"""Restore a snapshot — everything, or one piece of it (memory, a skill, a profile's config...).

Restore is planned first (``plan_restore``) and applied second (``apply_plan``), so every surface
can show the user exactly what will change before anything is touched. Within the selected scope
the disk is made to match the snapshot: changed files are rewritten, missing files recreated, and
files that did not exist at snapshot time are removed (the safety snapshot taken before applying
keeps them recoverable).
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

from .repo import Repository
from .scanner import classify, is_credential, scan, skill_roots

KINDS = ("memory", "soul", "skill", "config", "cron", "sessions", "plugin", "secrets", "media", "other")
_ALIASES = {"memories": "memory", "skills": "skill", "plugins": "plugin", "session": "sessions",
            "all": "", "everything": "", "*": ""}


class RestoreError(Exception):
    pass


@dataclass(frozen=True)
class Selector:
    """What to restore. Empty kind = everything; profile None = all profiles."""

    kind: str = ""
    item: str = ""
    profile: Optional[str] = None
    path: str = ""          # exact file or folder path (``file:...``)

    @classmethod
    def parse(cls, text: str, profile: Optional[str] = None) -> "Selector":
        """``all`` · ``memory`` · ``skills`` · ``skill:weather`` · ``plugin:x`` · ``file:skills/x/SKILL.md``
        · optionally prefixed by ``<profile>/`` (``work/memory``)."""
        text = (text or "all").strip()
        if text.startswith("file:"):
            p = PurePosixPath(text[5:].strip().strip("/"))
            if not p.parts or ".." in p.parts:
                raise RestoreError(f"invalid path {text[5:]!r}")
            return cls(path=p.as_posix(), profile=profile)
        if "/" in text and ":" not in text.split("/", 1)[0]:
            profile, text = text.split("/", 1)
        kind, _, item = text.partition(":")
        kind = _ALIASES.get(kind.lower(), kind.lower())
        if kind and kind not in KINDS:
            raise RestoreError(f"unknown item {kind!r}; use one of: all, {', '.join(KINDS)}, skill:<name>, file:<path>")
        return cls(kind=kind, item=item.strip(), profile=profile or None)

    def matches(self, rel: str, roots: Optional[frozenset] = None, item: Optional[str] = None) -> bool:
        if self.path:
            return rel == self.path or rel.startswith(self.path + "/")
        prof, kind, found_item = classify(rel, roots)
        item = item if item is not None else found_item
        if self.profile and prof != self.profile:
            return False
        if self.kind and kind != self.kind:
            return False
        if self.kind == "skill" and not item:
            # Files directly in skills/ (the curator's .curator_ledger.jsonl, .bundled_manifest) are Hermes'
            # own bookkeeping, not a skill: a skills restore must neither roll them back nor delete them.
            return False
        if self.item and item != self.item and not item.endswith("/" + self.item):
            return False   # "skill:maps" matches the nested "productivity/maps"
        return True

    def describe(self) -> str:
        if self.path:
            return f"file {self.path}"
        what = {"": "everything", "skill": "skills", "plugin": "plugins"}.get(self.kind, self.kind)
        if self.item:
            what = f"{self.kind} '{self.item}'"
        return what + (f" (profile {self.profile})" if self.profile else "")


@dataclass
class RestorePlan:
    snapshot_id: str
    selector: Selector
    write: list[dict] = field(default_factory=list)      # manifest entries to (re)write
    delete: list[str] = field(default_factory=list)      # rel paths to remove
    unchanged: int = 0

    @property
    def empty(self) -> bool:
        return not self.write and not self.delete

    @property
    def touches_sessions_db(self) -> bool:
        return any(PurePosixPath(e["p"]).name == "state.db" for e in self.write) or any(
            PurePosixPath(p).name == "state.db" for p in self.delete)


def _same_as_disk(repo: Repository, entry: dict, path: Path) -> bool:
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size != entry["s"]:
            return False
    except OSError:
        return False
    if path.name.endswith(".db"):
        return False  # live databases always differ byte-wise; restore explicitly
    from .snapshot import file_matches_entry
    return file_matches_entry(repo, path, entry)


def plan_restore(repo: Repository, snapshot_id: str, root: Path | str, selector: Selector,
                 extra_excludes=()) -> RestorePlan:
    root = Path(root)
    manifest = repo.load_manifest(snapshot_id)
    plan = RestorePlan(snapshot_id, selector)
    # Credential files are never written back, even from a snapshot taken by an older Ark that captured them.
    wanted = {e["p"]: e for e in manifest["files"]
              if not e.get("x") and not is_credential(e["p"]) and selector.matches(e["p"], item=e.get("i"))}
    current = list(scan(root, extra_excludes=extra_excludes))
    roots = skill_roots([e.rel for e in current] + [e["p"] for e in manifest["files"]])
    for rel, entry in sorted(wanted.items()):
        if _same_as_disk(repo, entry, root / rel):
            plan.unchanged += 1
        else:
            plan.write.append(entry)
    # Files in scope today that the snapshot did not have. Credential files are never scanned, so they are
    # never deleted either.
    scope = manifest.get("scope")
    unreadable = set(manifest.get("unreadable") or [])
    for cur in current:
        if cur.rel in unreadable:
            continue  # existed at snapshot time but could not be read: never "added since", never deleted
        if scope is not None and classify(cur.rel, roots)[1] not in scope:
            continue  # a scoped snapshot says nothing about kinds it did not capture
        if cur.rel not in wanted and selector.matches(cur.rel, roots):
            plan.delete.append(cur.rel)
    if not wanted and not plan.delete:
        raise RestoreError(f"snapshot {snapshot_id} has nothing matching {selector.describe()}")
    return plan


def _inside(path: Path, root: Path) -> bool:
    """True when *path* (resolved) is *root* or below it. Path-aware: ~/.hermes-evil is NOT inside ~/.hermes."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _safe_target(root: Path, rel: str) -> Path:
    p = PurePosixPath(rel)
    # Reject anything that is not a plain relative POSIX path: "..", absolute paths, and the
    # Windows forms that would escape the home there (drive letters "C:", backslashes, NUL).
    if (not p.parts or p.is_absolute() or ".." in p.parts
            or any(":" in part or "\\" in part or "\x00" in part for part in p.parts)):
        raise RestoreError(f"refusing unsafe path in snapshot: {rel!r}")
    target = root.joinpath(*p.parts)
    # Never follow a symlinked parent out of the home.
    parent = target.parent
    while parent != root and not parent.exists():
        parent = parent.parent
    if not _inside(parent, root):
        raise RestoreError(f"refusing to write outside the Hermes home: {rel!r}")
    if target.is_dir() and not target.is_symlink():
        raise RestoreError(f"cannot restore {rel!r}: a folder now exists at that path")
    return target


@dataclass
class RestoreResult:
    written: int
    deleted: int
    bytes: int


def _stage(repo: Repository, entry: dict, target: Path, created_dirs: list) -> tuple[str, int]:
    """Write *entry* to a private temp file next to *target*, verifying every blob. Returns (temp, size)."""
    missing = []
    d = target.parent
    while not d.exists():
        missing.append(d)
        d = d.parent
    target.parent.mkdir(parents=True, exist_ok=True)
    created_dirs.extend(reversed(missing))
    fd, tmp = tempfile.mkstemp(prefix=".ark-", dir=target.parent)
    size = 0
    try:
        with os.fdopen(fd, "wb") as f:
            for blob_id in entry["b"]:
                chunk = repo.get_blob(blob_id)          # raises on a damaged/missing blob
                f.write(chunk)
                size += len(chunk)
            f.flush()
            os.fsync(f.fileno())
        if size != entry["s"]:
            raise RestoreError(f"{entry['p']}: size mismatch in backup")
        # Permission bits only: never restore setuid/setgid/sticky, whatever a manifest says.
        os.chmod(tmp, (int(entry.get("m", 0o600)) & 0o777) or 0o600)
        if entry.get("t"):
            os.utime(tmp, ns=(int(entry["t"]), int(entry["t"])))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return tmp, size


def apply_plan(repo: Repository, plan: RestorePlan, root: Path | str) -> RestoreResult:
    """Apply *plan* in two phases so a failure never leaves a half-restored home:

    1. every file is streamed from the backup into a temp file beside its target, each blob decrypted
       and integrity-checked (memory stays bounded by one blob, whatever the file size);
    2. only when all of them are ready are they renamed into place, then removals happen.
    Any error in phase 1 deletes the temp files and leaves the home exactly as it was.
    """
    root = Path(root)
    targets = [(entry, _safe_target(root, entry["p"])) for entry in plan.write]   # pre-flight, no writes
    deletes = [_safe_target(root, rel) for rel in plan.delete]
    staged: list[tuple[dict, Path, str, int]] = []
    created_dirs: list[Path] = []
    try:
        for entry, target in targets:
            tmp, size = _stage(repo, entry, target, created_dirs)
            staged.append((entry, target, tmp, size))
    except BaseException:
        for _, _, tmp, _ in staged:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        for d in reversed(created_dirs):
            try:
                d.rmdir()
            except OSError:
                pass
        raise

    total = 0
    for _entry, target, tmp, size in staged:
        if target.name.endswith(".db"):
            for suffix in ("-wal", "-shm", "-journal"):   # stale sidecars would corrupt the restored DB
                side = target.with_name(target.name + suffix)
                if side.exists():
                    side.unlink()
        if target.is_symlink():
            target.unlink()
        os.replace(tmp, target)
        total += size
    for target in deletes:
        try:
            if not target.is_dir() or target.is_symlink():
                target.unlink()
        except FileNotFoundError:
            pass
        _prune_empty_dirs(target.parent, root)
    return RestoreResult(len(staged), len(deletes), total)


def _prune_empty_dirs(d: Path, root: Path) -> None:
    root = root.resolve()
    d = d.resolve()
    while d != root and _inside(d, root):
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent
