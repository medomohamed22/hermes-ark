"""Create, list and compare snapshots of a Hermes home."""

from __future__ import annotations

import os
import socket
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from typing import BinaryIO, Callable, Iterable, Iterator, Optional

from .repo import Repository, new_snapshot_id
from .scanner import FileEntry, classify, dirs_for_kinds, scan, skill_roots

CHUNK = 1 << 20          # 1 MiB chunks: SQLite pages stay aligned, so an edited DB re-uploads only touched chunks
SMALL = 4 << 20          # files up to 4 MiB are one blob


@dataclass
class SnapshotResult:
    snapshot_id: str
    files: int
    bytes_total: int
    bytes_new: int
    blobs_new: int
    seconds: float
    skipped: list[str] = field(default_factory=list)
    unchanged: bool = False


def _is_sqlite(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(16) == b"SQLite format 3\x00"


def stream_chunks(fh: BinaryIO) -> Iterator[bytes]:
    """Split a stream the way blobs are stored: one blob up to SMALL bytes, else CHUNK pieces.
    Never holds more than SMALL+1 bytes, whatever the file size (a multi-GB video included)."""
    head = fh.read(SMALL + 1)
    if len(head) <= SMALL:
        yield head
        return
    buf = head
    while True:
        while len(buf) >= CHUNK:
            yield buf[:CHUNK]
            buf = buf[CHUNK:]
        more = fh.read(CHUNK)
        if not more:
            break
        buf += more
    if buf:
        yield buf


@contextmanager
def open_consistent(entry: FileEntry):
    """Open a file for reading. SQLite databases are first copied through sqlite3's online backup
    API (into a private 0600 temp file) so a live WAL-mode database yields a consistent image."""
    if entry.is_sqlite and _is_sqlite(entry.path):
        fd, tmp = tempfile.mkstemp(suffix=".db", prefix="ark-")
        os.close(fd)
        try:
            # as_uri() percent-encodes the path, so "?" or "#" in a folder name cannot change the URI.
            src = sqlite3.connect(entry.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            with open(tmp, "rb") as fh:
                yield fh
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    else:
        with entry.path.open("rb") as fh:
            yield fh


def create_snapshot(
    repo: Repository,
    root: Path | str,
    *,
    label: str = "",
    trigger: str = "manual",
    extra_excludes: Iterable[str] = (),
    skip_if_unchanged: bool = False,
    scope: Optional[Iterable[str]] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> SnapshotResult:
    """Capture *root* into *repo*. Credential stores are never captured.

    *scope* limits the snapshot to some kinds (e.g. ``BRAIN``: memory, soul, skills, config, cron) —
    fast enough to run before every memory/skill write. A scoped snapshot records its scope, and a
    restore from it never touches anything outside that scope.
    """
    started = time.monotonic()
    root = Path(root)
    scope_set = frozenset(scope) if scope is not None else None
    entries, skipped, skip_reasons = [], [], []
    total = new_bytes = new_blobs = 0
    top = dirs_for_kinds(scope_set) if scope_set is not None else None
    found = list(scan(root, extra_excludes=extra_excludes, top_dirs=top))
    roots = skill_roots(e.rel for e in found)
    for entry in found:
        if scope_set is not None and classify(entry.rel, roots)[1] not in scope_set:
            continue
        ids, size = [], 0
        try:
            with open_consistent(entry) as fh:
                for chunk in stream_chunks(fh):
                    blob_id, fresh = repo.put_blob(chunk)
                    ids.append(blob_id)
                    size += len(chunk)
                    if fresh:
                        new_blobs += 1
                        new_bytes += len(chunk)
        except (OSError, sqlite3.Error) as exc:
            skipped.append(entry.rel)
            skip_reasons.append(f"{entry.rel}: {exc.__class__.__name__}")
            continue
        total += size
        profile, kind, item = classify(entry.rel, roots)
        entries.append({"p": entry.rel, "s": size, "m": entry.mode, "t": entry.mtime_ns, "b": ids,
                        "k": kind, "i": item, "pr": profile})
        if progress:
            progress(entry.rel)

    if skip_if_unchanged:
        prev = _latest_covering(repo, scope_set)
        if prev is not None:
            prev_files = [f for f in prev["files"] if scope_set is None or f.get("k") in scope_set]
            if _content_key(prev_files) == _content_key(entries):
                return SnapshotResult(prev["id"], len(entries), total, 0, 0, time.monotonic() - started, skip_reasons, unchanged=True)

    sid = new_snapshot_id()
    manifest = {
        "id": sid,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "label": label,
        "trigger": trigger,
        "host": socket.gethostname(),
        "root": str(root),
        "secrets": False,
        "scope": sorted(scope_set) if scope_set is not None else None,
        "files": entries,
        # Files that existed but could not be read: a restore must never treat them as "added later".
        "unreadable": skipped,
        "stats": {"files": len(entries), "bytes": total, "new_bytes": new_bytes, "new_blobs": new_blobs},
    }
    repo.save_manifest(manifest)
    return SnapshotResult(sid, len(entries), total, new_bytes, new_blobs, time.monotonic() - started, skip_reasons)


BRAIN = ("memory", "soul", "skill", "config", "cron")


def _latest_covering(repo: Repository, scope_set) -> Optional[dict]:
    """Newest snapshot whose scope includes *scope_set* (a full snapshot covers everything)."""
    for sid in reversed(repo.snapshot_ids()):
        m = repo.load_manifest(sid)
        sc = m.get("scope")
        if sc is None or (scope_set is not None and scope_set <= set(sc)):
            return m
    return None


def _content_key(files: list[dict]) -> list[tuple]:
    return sorted((f["p"], tuple(f["b"])) for f in files)


@dataclass
class SnapshotInfo:
    id: str
    created: str
    label: str
    trigger: str
    files: int
    bytes: int
    new_bytes: int
    host: str
    scope: Optional[list] = None


def list_snapshots(repo: Repository) -> list[SnapshotInfo]:
    out = []
    for sid in repo.snapshot_ids():
        m = repo.load_manifest(sid)
        st = m.get("stats", {})
        out.append(SnapshotInfo(sid, m.get("created", ""), m.get("label", ""), m.get("trigger", ""),
                                st.get("files", len(m.get("files", []))), st.get("bytes", 0), st.get("new_bytes", 0), m.get("host", ""), m.get("scope")))
    return out


@dataclass
class Change:
    path: str
    status: str   # added | removed | modified
    kind: str
    item: str
    profile: str
    size_before: int
    size_after: int


def diff_manifests(old: dict, new: dict) -> list[Change]:
    """Changes between two snapshots. When either is scoped (a brain snapshot), only the kinds both
    captured are compared: a brain snapshot is silent about sessions, not "they were deleted"."""
    scopes = [set(m["scope"]) for m in (old, new) if m.get("scope") is not None]
    common = set.intersection(*scopes) if scopes else None
    keep = (lambda f: f.get("k") in common) if common is not None else (lambda f: True)
    a = {f["p"]: f for f in old["files"] if keep(f)}
    b = {f["p"]: f for f in new["files"] if keep(f)}
    changes = []
    for path in sorted(a.keys() | b.keys()):
        fa, fb = a.get(path), b.get(path)
        if fa and fb and fa["b"] == fb["b"]:
            continue
        status = "added" if not fa else "removed" if not fb else "modified"
        ref = fb or fa
        changes.append(Change(path, status, ref.get("k", "other"), ref.get("i", ""), ref.get("pr", "default"),
                              fa["s"] if fa else 0, fb["s"] if fb else 0))
    return changes


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def file_matches_entry(repo: Repository, path: Path, entry: dict) -> bool:
    """Does the file on disk hold exactly the snapshot entry's content? Streams; never loads it whole."""
    try:
        with path.open("rb") as fh:
            ids = [repo.blob_id(c) for c in stream_chunks(fh)]
    except OSError:
        return False
    return ids == entry["b"]


def read_file_from_snapshot(repo: Repository, entry: dict) -> bytes:
    return b"".join(repo.get_blob(b) for b in entry["b"])

