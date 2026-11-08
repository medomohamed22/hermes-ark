"""Bridge between Hermes (settings, secrets, home dir) and the pure ``arkcore`` engine."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .arkcore.backends import Backend, BackendError, LocalBackend
from .arkcore.repo import Repository

PLUGIN_NAME = "hermes-ark"

def _config_fallback(key: str, default: Any = None) -> Any:
    """Read ``plugins.entries.hermes-ark.settings.<key>`` straight from config.yaml. Used where the
    plugin context is not available (the Desktop backend runs ``plugin_api.py`` as its own module)."""
    try:
        from hermes_cli.config import load_config_readonly
        entry = ((load_config_readonly() or {}).get("plugins") or {}).get("entries", {}).get(PLUGIN_NAME) or {}
        settings = entry.get("settings") or entry.get("config") or {}
        return settings.get(key, default)
    except Exception:
        return default


_get_config: Callable[[str, Any], Any] = _config_fallback  # replaced in register()
def _set_fallback(key: str, value: Any) -> None:
    """Write ``plugins.entries.hermes-ark.settings.<key>`` without a plugin context (Desktop backend, tests)."""
    from hermes_cli.config import save_config
    path = ("plugins", "entries", PLUGIN_NAME, "settings", key)
    save_config({"plugins": {"entries": {PLUGIN_NAME: {"settings": {key: value}}}}},
                preserve_keys={path}, merge_existing=True)


_set_config: Callable[[str, Any], None] = _set_fallback


def bind(ctx) -> None:
    global _get_config, _set_config
    _get_config = ctx.get_config
    _set_config = ctx.set_config


def set_value(key: str, value: Any) -> None:
    _set_config(key, value)


def hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir(PLUGIN_NAME)


def passphrase() -> Optional[str]:
    from agent.secret_scope import get_secret
    try:
        value = get_secret("ARK_PASSPHRASE")
    except Exception:  # multiplexed gateway with no scope: fail closed, behave as unset
        value = None
    return value or None


def get(key: str, default: Any = None) -> Any:
    try:
        value = _get_config(key, default)
    except Exception:
        return default
    return default if value is None else value


@dataclass
class Settings:
    repository: Path
    exclude: list[str]
    auto: bool = True
    interval_minutes: int = 60
    health_guard: bool = True
    remote: str = ""
    s3_endpoint: str = ""
    s3_region: str = "auto"
    auto_push: bool = True
    allow_agent_admin: bool = False


def _int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def load() -> Settings:
    repo = str(get("repository", "") or "").strip()
    excl = get("exclude", []) or []
    return Settings(
        repository=Path(repo).expanduser() if repo else data_dir() / "repo",
        exclude=[str(e) for e in excl if str(e).strip()],
        auto=bool(get("auto_snapshots", True)),
        interval_minutes=_int(get("interval_minutes", 60), 60),
        health_guard=bool(get("health_guard", True)),
        remote=str(get("remote", "") or "").strip(),
        s3_endpoint=str(get("s3_endpoint", "") or "").strip(),
        s3_region=str(get("s3_region", "auto") or "auto").strip(),
        auto_push=bool(get("auto_push", True)),
        allow_agent_admin=bool(get("allow_agent_admin", False)),
    )


def lock_path(settings: Optional[Settings] = None) -> Path:
    return data_dir() / "repo.lock"


def backend(settings: Optional[Settings] = None) -> LocalBackend:
    return LocalBackend((settings or load()).repository)


_cache: dict[tuple[str, str], Repository] = {}
_cache_lock = threading.Lock()


def open_repo(settings: Optional[Settings] = None) -> Repository:
    """Open (and cache) the repository. Unlocking runs scrypt (~0.3 s), so automatic snapshots
    reuse the unlocked repository instead of paying that on every memory write."""
    st = settings or load()
    pw = passphrase()
    key = (str(st.repository), hashlib.sha256((pw or "").encode()).hexdigest())
    with _cache_lock:
        repo = _cache.get(key)
        if repo is None or not Repository.exists(repo.backend):
            repo = Repository.open(backend(st), pw)
            _cache.clear()
            _cache[key] = repo
        return repo


def _secret(name: str) -> str:
    from agent.secret_scope import get_secret
    try:
        return get_secret(name) or ""
    except Exception:
        return ""


def remote_backend(st: Optional[Settings] = None, url: str = "", endpoint: str = "", region: str = "") -> Backend:
    """The off-site copy: ``s3://bucket/prefix`` (S3-compatible) or a folder path (USB drive, NAS,
    Syncthing/Dropbox folder...). Raises BackendError when none is configured."""
    st = st or load()
    url = (url or st.remote).strip()
    if not url:
        raise BackendError("no remote configured: set `remote` (s3://bucket/prefix or a folder) in the plugin settings, "
                           "or pass --remote")
    if url.startswith("s3://"):
        from .arkcore.s3 import S3Backend, S3Config
        return S3Backend(S3Config.from_url(
            url, endpoint=endpoint or st.s3_endpoint, region=region or st.s3_region,
            access_key=_secret("ARK_S3_ACCESS_KEY_ID"), secret_key=_secret("ARK_S3_SECRET_ACCESS_KEY")))
    if "://" in url:
        raise BackendError(f"unsupported remote {url!r} (use s3://bucket/prefix or a folder path)")
    target = Path(url).expanduser()
    if target.resolve() == load().repository.resolve():
        raise BackendError("the remote folder is the same as the local repository")
    return LocalBackend(target)


def excludes(st: Optional[Settings] = None) -> list[str]:
    """User exclusions plus Ark's own storage when it lives inside the Hermes home (a repository or a
    folder remote under ~/.hermes): never back it up into itself, and never let a restore delete it."""
    st = st or load()
    out = list(st.exclude)
    try:
        home = hermes_home().resolve()
    except Exception:
        return out
    candidates = [st.repository]
    if st.remote and "://" not in st.remote:
        candidates.append(Path(st.remote).expanduser())
    for c in candidates:
        try:
            rel = c.expanduser().resolve().relative_to(home)
        except (ValueError, OSError, RuntimeError):
            continue
        if rel.parts:
            out.append(rel.as_posix())
    return out
