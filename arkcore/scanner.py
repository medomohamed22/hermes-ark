"""Decide which files under a Hermes home belong in a snapshot.

The rules mirror Hermes' own ``hermes backup`` exclusions (regenerable runtimes, caches, browser
profiles, nested backups) and add one policy of Ark's own: **credentials are never captured unless
the user opts in and the repository is encrypted** (``.env``, ``auth.json``, vault keys, secret
caches, pairing tokens, browser login stores).
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator, Optional

# Anywhere in the tree: never user data.
EXCLUDED_DIRS_ANYWHERE = frozenset({
    "__pycache__", ".git", "node_modules", ".venv", "venv", "site-packages",
    ".cache", ".tox", ".nox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "browser-profiles", "browser-profile",  # live CDP profiles / copied Cookies + Login Data
})

# Only at a home root (``~/.hermes`` or ``~/.hermes/profiles/<name>``): regenerable machine state.
EXCLUDED_ROOT_DIRS = frozenset({
    "hermes-agent",          # the code checkout - reinstall instead
    "models", "runtimes", "node", "installs", "tools",  # downloaded runtimes, often tens of GB
    "backups", "state-snapshots", "checkpoints",          # Hermes' own backups / per-session caches
    "browser_profiles",      # Browser Use CLI user-data (Login Data / Cookies)
    "logs", "audio_cache", "image_cache",                # regenerable
    "plugin-data/hermes-ark",  # never back up our own repository into itself
    # Gateway runtime state: heartbeats, lifecycle, delivery queues' locks, update-check caches.
    "state", "runtime", "plugin-update-checks", "source-checks",
    # The Webapp's signed-in session token: a credential, and stale on any other machine.
    "webapp",
    ".curator_backups",      # the skill curator's own backups: a backup of a backup
})

# Root-level files that are machine state or regenerable caches, never user data.
EXCLUDED_ROOT_FILES = frozenset({
    "install_id",                          # this machine's identity; must not be cloned onto another
    "gateway_state.json", "gateway.sock.path", "gateway-starts.log",
    "models_dev_cache.json", "models_dev_cache.etag", "provider_models_cache.json",
    ".clean_shutdown", ".skills_prompt_snapshot.json",   # runtime marker / regenerated prompt cache
})
# Inside cron/: the scheduler's own liveness markers.
EXCLUDED_CRON_FILES = frozenset({"ticker_heartbeat", "ticker_last_success"})

# ``cache/`` at a home root: only durable media / evidence the user cannot regenerate.
KEPT_CACHE_SUBDIRS = frozenset({"images", "audio", "videos", "documents", "screenshots", "citations", "generated"})

# Credential stores (relative to a home root). Never captured, never restored: Hermes owns them, and
# users back credentials up with `hermes auth` or their own secret manager.
SECRET_ROOT_FILES = frozenset({
    ".env", "auth.json", "vault.key", "vault.json.enc",
    "bws_cache.json", "bws_cache.enc.json", "op_cache.json", "photon-sidecar.json",
})
SECRET_ROOT_DIRS = frozenset({"pairing"})

SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
# Runtime files that must never be captured — and therefore never deleted by a restore while
# another process may hold them (e.g. the memory tool's MEMORY.md.lock).
RUNTIME_SUFFIXES = (".lock", ".pid", ".sock")
RUNTIME_PREFIXES = (".ark-", ".tmp-")


@dataclass(frozen=True)
class FileEntry:
    rel: str          # POSIX path relative to the backed-up root, e.g. "profiles/work/skills/x/SKILL.md"
    path: Path        # absolute path on disk
    size: int
    mtime_ns: int
    mode: int
    secret: bool = False

    @property
    def is_sqlite(self) -> bool:
        return self.rel.endswith(".db")


def _home_roots(root: Path) -> list[tuple[str, Path]]:
    """The main home plus every profile home under ``profiles/``: (rel prefix, path)."""
    roots = [("", root)]
    prof = root / "profiles"
    if prof.is_dir():
        for child in sorted(prof.iterdir()):
            if child.is_dir() and not child.is_symlink():
                roots.append((f"profiles/{child.name}/", child))
    return roots


def _excluded_under_home(rel_in_home: PurePosixPath, is_dir: bool) -> Optional[str]:
    """Reason a path relative to a home root is excluded, or None to keep it."""
    parts = rel_in_home.parts
    if not parts:
        return None
    if any(p in EXCLUDED_DIRS_ANYWHERE for p in (parts if is_dir else parts[:-1])):
        return "regenerable"
    top = parts[0]
    joined2 = "/".join(parts[:2])
    if top in EXCLUDED_ROOT_DIRS or joined2 in EXCLUDED_ROOT_DIRS:
        return "regenerable"
    if top == "cache" and (len(parts) == 1 and not is_dir or len(parts) >= 2 and parts[1] not in KEPT_CACHE_SUBDIRS):
        return "regenerable"
    if top == "profiles":
        return "profile-root"  # walked separately as its own home
    name = parts[-1]
    if not is_dir and len(parts) == 1 and name in EXCLUDED_ROOT_FILES:
        return "regenerable"
    if not is_dir and len(parts) == 2 and top == "cron" and name in EXCLUDED_CRON_FILES:
        return "runtime"
    if not is_dir and any(name.endswith(s) for s in SQLITE_SIDECARS):
        return "sqlite-sidecar"
    if not is_dir and (name.endswith(RUNTIME_SUFFIXES) or name.startswith(RUNTIME_PREFIXES)):
        return "runtime"
    return None


def _is_secret(rel_in_home: PurePosixPath) -> bool:
    parts = rel_in_home.parts
    return (len(parts) == 1 and parts[0] in SECRET_ROOT_FILES) or (parts and parts[0] in SECRET_ROOT_DIRS)


def is_credential(rel: str) -> bool:
    """Is this backed-up path (``.env``, ``profiles/work/auth.json``, ``pairing/…``) a credential store?"""
    parts = PurePosixPath(rel).parts
    if len(parts) >= 3 and parts[0] == "profiles":
        parts = parts[2:]
    return bool(parts) and _is_secret(PurePosixPath(*parts))


def scan(root: Path | str, extra_excludes: Iterable[str] = (),
         top_dirs: Optional[frozenset] = None) -> Iterator[FileEntry]:
    """Yield every file to capture under *root* (a Hermes home), in a stable order.

    Symlinks, sockets and devices are skipped (a zip/restore cannot hold them faithfully, and a
    symlink could pull files from outside the home into a backup).

    *top_dirs* limits the walk to those folders at each home root (files at the root are always
    listed): a brain snapshot then never walks thousands of session files.
    """
    root = Path(root)
    extra = tuple(e.strip("/") for e in extra_excludes if e.strip("/"))
    for prefix, home in _home_roots(root):
        for dirpath, dirnames, filenames in os.walk(home, followlinks=False):
            d = Path(dirpath)
            rel_dir = PurePosixPath(d.relative_to(home).as_posix()) if d != home else PurePosixPath()
            keep_dirs = []
            for name in sorted(dirnames):
                child = d / name
                rel = rel_dir / name
                if child.is_symlink():
                    continue
                if top_dirs is not None and len(rel.parts) == 1 and name not in top_dirs:
                    continue
                if _excluded_under_home(rel, True):
                    continue
                if rel.parts[:1] == ("desktop-plugins",) and len(rel.parts) == 2 and (child / ".hermes-package.json").exists():
                    continue  # Hermes regenerates package desktop halves; hand-written desktop plugins are kept
                if _is_secret(rel):
                    continue
                full_dir = prefix + rel.as_posix()
                if any(full_dir == e or full_dir.startswith(e + "/") for e in extra):
                    continue
                keep_dirs.append(name)
            dirnames[:] = keep_dirs
            for name in sorted(filenames):
                rel = rel_dir / name
                full_rel = prefix + rel.as_posix()
                if _excluded_under_home(rel, False):
                    continue
                if _is_secret(rel):
                    continue
                if any(full_rel == e or full_rel.startswith(e + "/") for e in extra):
                    continue
                p = d / name
                try:
                    st = p.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                yield FileEntry(full_rel, p, st.st_size, st.st_mtime_ns, stat.S_IMODE(st.st_mode), False)


# ---- Classification: what a path *is*, so users can restore "my memory" instead of file paths ----

def skill_roots(rels: Iterable[str]) -> frozenset[str]:
    """Folders that hold a ``SKILL.md`` under a ``skills/`` dir. Hermes nests skills in categories
    (``skills/productivity/maps/SKILL.md``), so a skill is identified by that folder, not by the
    first path segment."""
    roots = set()
    for rel in rels:
        if rel.endswith("/SKILL.md"):
            d = rel.rsplit("/", 1)[0]
            parts = PurePosixPath(d).parts
            if "skills" in parts[:3]:
                roots.add(d)
    return frozenset(roots)


def classify(rel: str, roots: Optional[frozenset] = None) -> tuple[str, str, str]:
    """Return (profile, kind, item) for a snapshot path.

    kind is one of memory, soul, skill, config, cron, sessions, plugin, secrets, media, other.
    item names the restorable unit (a skill's path below skills/, e.g. "productivity/maps", a plugin
    name...), or "" for the kind. Pass *roots* (from :func:`skill_roots`) to resolve nested skills.
    """
    parts = PurePosixPath(rel).parts
    profile = "default"
    prefix = ""
    if len(parts) >= 3 and parts[0] == "profiles":
        profile, parts, prefix = parts[1], parts[2:], f"profiles/{parts[1]}/"
    if not parts:
        return profile, "other", ""
    top, name = parts[0], parts[-1]
    if len(parts) == 1:
        if name in ("MEMORY.md", "USER.md"):
            return profile, "memory", name
        if name == "SOUL.md":
            return profile, "soul", ""
        if name == "config.yaml":
            return profile, "config", ""
        if name == "state.db":
            return profile, "sessions", ""
        if name in SECRET_ROOT_FILES:
            return profile, "secrets", name
        return profile, "other", name
    if top == "memories":
        return profile, "memory", parts[1] if len(parts) > 1 else ""
    if top == "skills":
        item = parts[1] if len(parts) > 2 else ""      # files directly in skills/ are metadata
        if roots:
            d = rel
            base = prefix + "skills/"
            while "/" in d:
                d = d.rsplit("/", 1)[0]
                if d in roots:
                    item = d[len(base):]
                    break
                if len(d) <= len(base):
                    break
        return profile, "skill", item
    if top == "cron":
        return profile, "cron", ""
    if top == "sessions":
        return profile, "sessions", ""
    if top == "plugins":
        return profile, "plugin", parts[1]
    if top in SECRET_ROOT_DIRS:
        return profile, "secrets", top
    if top == "cache":
        return profile, "media", parts[1] if len(parts) > 1 else ""
    return profile, "other", top


# Folders that hold each kind, for walks that only need some kinds.
KIND_DIRS = {"memory": {"memories"}, "skill": {"skills"}, "cron": {"cron"}, "soul": set(), "config": set()}


def dirs_for_kinds(kinds: Iterable[str]) -> Optional[frozenset]:
    """Top-level folders needed to see *kinds*, or None when some kind needs a full walk."""
    out: set[str] = set()
    for k in kinds:
        if k not in KIND_DIRS:
            return None
        out |= KIND_DIRS[k]
    return frozenset(out)
