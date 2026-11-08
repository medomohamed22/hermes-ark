"""Autopilot: automatic snapshots, the health guard, and the agent's ``ark_checkpoint`` tool.

* Before every ``memory`` / ``skill_manage`` tool call, a *brain* snapshot (memory, SOUL, skills,
  config, cron) is taken — skipped when nothing changed, so it costs a directory walk.
* After that call, the health guard compares memory/skills before vs after. A sudden large loss is
  reported inside the tool result, so the agent sees it immediately and tells the user, together
  with the exact ``/ark restore`` command that undoes it.
* At the end of a session, if ``interval_minutes`` have passed, a full snapshot runs in a background
  thread; retention pruning runs at most once a day after it.

Nothing here may break the agent: every failure is logged and swallowed.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import selfdefense as D
from . import settings as S
from .arkcore.guard import compare, format_alarms, measure
from .arkcore.lock import LockBusy, _lock as _flock_raw, _unlock as _funlock_raw, repo_lock
from .arkcore.repo import Repository
from .arkcore.retention import Policy, prune
from .arkcore.snapshot import BRAIN, create_snapshot

logger = logging.getLogger(__name__)

WATCHED_TOOLS = frozenset({"memory", "skill_manage"})
# Generic tools that can also rewrite Hermes' own files (``echo > ~/.hermes/memories/MEMORY.md``,
# a write_file on a SKILL.md). Watched only when the call actually points at those files.
FILE_TOOLS = frozenset({"write_file", "patch"})
COMMAND_TOOLS = frozenset({"terminal", "execute_code"})
_BRAIN_MARKERS = ("MEMORY.md", "USER.md", "SOUL.md", "memories", "/skills", "skills/", ".hermes", "config.yaml",
                  "cron/", "jobs.json")
PRUNE_EVERY_S = 24 * 3600
_EVENTS_MAX = 200
_state_lock = threading.Lock()
_full_running = threading.Event()
_start_lock = threading.Lock()
_warned_unready = False


# ---- small persistent state (data dir) ---------------------------------------------------------

def _flock(fh) -> None:
    deadline = time.monotonic() + 10
    while True:
        try:
            _flock_raw(fh)
            return
        except OSError:
            if time.monotonic() > deadline:
                raise LockBusy("autopilot state is locked")
            time.sleep(0.05)


def _funlock(fh) -> None:
    try:
        _funlock_raw(fh)
    except OSError:
        pass


def _state_file() -> Path:
    return S.data_dir() / "autopilot.json"


def load_state() -> dict:
    try:
        return json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return {}


@contextmanager
def _state_guard():
    """Thread lock + inter-process file lock: the gateway and a terminal `hermes ark` may both write."""
    with _state_lock:
        lock = S.data_dir() / "autopilot.lock"
        with open(lock, "a+b") as fh:
            _flock(fh)
            try:
                yield
            finally:
                _funlock(fh)


def _write_state(st: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".autopilot-", dir=S.data_dir())
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(st, f, indent=1)
        os.replace(tmp, _state_file())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _update_state(**changes: Any) -> None:
    with _state_guard():
        st = load_state()
        st.update(changes)
        _write_state(st)


def record_event(kind: str, message: str, snapshot_id: Optional[str] = None) -> None:
    with _state_guard():
        st = load_state()
        events = st.get("events", [])
        events.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind,
                       "message": message[:500], "snapshot": snapshot_id})
        st["events"] = events[-_EVENTS_MAX:]
        _write_state(st)


def protected_snapshots(days: int = 30) -> set[str]:
    """Snapshots named by a health-guard alert in the last *days*: their undo command must keep
    working, so retention never prunes them."""
    cutoff = time.time() - days * 86400
    out = set()
    for e in load_state().get("events", []):
        try:
            when = datetime.fromisoformat(e["at"]).timestamp()
        except (KeyError, ValueError):
            continue
        if e.get("snapshot") and when >= cutoff:
            out.add(e["snapshot"])
    return out


def record_push(objects: int, nbytes: int, where: str) -> None:
    _update_state(last_push={"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "objects": objects,
                             "bytes": nbytes, "where": where})


def record_verify(where: str, ok: bool, snapshots: int, damaged: int) -> None:
    _update_state(last_verify={"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "where": where,
                               "ok": ok, "snapshots": snapshots, "damaged": damaged})


def auto_push() -> None:
    """Push after an automatic snapshot when a remote is configured. Failures are recorded, not raised."""
    st = S.load()
    if not (st.remote and st.auto_push):
        return
    from .arkcore.sync import sync
    try:
        remote = S.remote_backend(st)
        res = sync(S.backend(st), remote)
        record_push(res.copied, res.bytes, remote.label)
    except Exception as exc:
        logger.warning("Hermes Ark: off-site push failed: %s", exc)
        record_event("error", f"off-site push failed: {exc}")


# ---- repository access ------------------------------------------------------------------------

def _ready_repo(st: S.Settings) -> Optional[Repository]:
    """The repo if Ark is set up and unlocked; None (logged once) otherwise."""
    global _warned_unready
    try:
        if not Repository.exists(S.backend(st)):
            return None
        return S.open_repo(st)
    except Exception as exc:
        if not _warned_unready:
            logger.warning("Hermes Ark: automatic snapshots paused (%s)", exc)
            _warned_unready = True
        return None


def brain_checkpoint(reason: str) -> Optional[str]:
    st = S.load()
    if not st.auto:
        return None
    repo = _ready_repo(st)
    if repo is None:
        return None
    # A full snapshot may hold the lock for a while; the agent's memory write must not wait on it.
    with repo_lock(S.lock_path(st), timeout=2):
        res = create_snapshot(repo, S.hermes_home(), label=reason, trigger="auto:pre-write", scope=BRAIN,
                              extra_excludes=S.excludes(st), skip_if_unchanged=True)
    return res.snapshot_id


def full_snapshot(trigger: str = "auto", label: str = "", force_prune: bool = False) -> Optional[str]:
    st = S.load()
    repo = _ready_repo(st)
    if repo is None:
        return None
    with repo_lock(S.lock_path(st), timeout=60):
        res = create_snapshot(repo, S.hermes_home(), label=label, trigger=trigger,
                              extra_excludes=S.excludes(st),
                              skip_if_unchanged=True)
        now = time.time()
        _update_state(last_full=now, last_full_id=res.snapshot_id)
        if force_prune or now - load_state().get("last_prune", 0) >= PRUNE_EVERY_S:
            pr = prune(repo, Policy.from_settings(S.get), protect=protected_snapshots())
            _update_state(last_prune=now)
            if pr.removed_snapshots or pr.removed_blobs:
                record_deletions(pr.removed_snapshots)
            if pr.removed_snapshots:
                logger.info("Hermes Ark: pruned %d snapshot(s), %d blob(s)", len(pr.removed_snapshots), pr.removed_blobs)
    return res.snapshot_id


# ---- hooks / middleware -----------------------------------------------------------------------

def on_session_end(**_: Any) -> None:
    """Full snapshot in the background when the interval has elapsed. Never blocks the agent."""
    try:
        st = S.load()
        if not st.auto:
            return
        if time.time() - load_state().get("last_full", 0) < st.interval_minutes * 60:
            return
    except Exception:
        return
    with _start_lock:                       # two sessions ending together start one snapshot, not two
        if _full_running.is_set():
            return
        _full_running.set()

    def run():
        try:
            if full_snapshot() is not None:
                auto_push()      # outside the lock: blobs are immutable, uploads can be slow
        except LockBusy:
            pass
        except Exception as exc:
            logger.warning("Hermes Ark: automatic snapshot failed: %s", exc)
            try:
                record_event("error", f"automatic snapshot failed: {exc}")
            except Exception:
                pass
        finally:
            _full_running.clear()

    threading.Thread(target=run, name="hermes-ark-snapshot", daemon=True).start()


def _attach_note(result: Any, note: str, key: str = "ark_health_guard") -> Any:
    """Add the guard's note to a tool result without breaking its JSON shape."""
    if isinstance(result, str):
        try:
            data = json.loads(result)
        except ValueError:
            return result + "\n\n" + note
        if isinstance(data, dict):
            data[key] = note
            return json.dumps(data, ensure_ascii=False)
        return result + "\n\n" + note
    if isinstance(result, dict):
        return {**result, key: note}
    return result


def _touches_brain(tool_name: str, args: Any) -> bool:
    """Does this call write (or might it write) Hermes' memory, SOUL, skills, config or cron?"""
    if tool_name in WATCHED_TOOLS:
        return True
    if not isinstance(args, dict):
        return False
    if tool_name in FILE_TOOLS:
        raw = str(args.get("path") or args.get("file_path") or "")
        if not raw:
            return False
        try:
            from pathlib import Path
            home = S.hermes_home().resolve()
            target = Path(raw).expanduser()
            target = (target if target.is_absolute() else Path.cwd() / target).resolve()
            rel = target.relative_to(home)
        except (ValueError, OSError, RuntimeError):
            return False
        from .arkcore.scanner import classify
        from .arkcore.snapshot import BRAIN
        return classify(rel.as_posix())[1] in BRAIN
    if tool_name in COMMAND_TOOLS:
        text = str(args.get("command") or args.get("code") or "")
        if not text:
            return False
        try:
            home = str(S.hermes_home())
        except Exception:
            home = ""
        return (bool(home) and home in text) or any(m in text for m in _BRAIN_MARKERS)
    return False


def _agent_admin() -> bool:
    try:
        return S.load().allow_agent_admin
    except Exception:
        return False


_DELETIONS_MAX = 50
# Deletions made by *this* process (autopilot retention in the gateway, Desktop actions). During an
# agent tool call only these are trusted: a deletion from another process in that window — even through
# Ark's own CLI, run from a disguised script — is the agent's doing.
_local_deletions: list[tuple[float, list[str]]] = []


def record_deletions(snapshot_ids: list[str]) -> None:
    """Ark's own deletions (retention, `forget`), so the tamper check does not mistake them for an attack."""
    with _state_lock:
        _local_deletions.append((time.time(), list(snapshot_ids)))
        del _local_deletions[:-_DELETIONS_MAX]
    with _state_guard():
        st = load_state()
        dels = st.get("deletions", [])
        dels.append({"at": time.time(), "ids": list(snapshot_ids)})
        st["deletions"] = dels[-_DELETIONS_MAX:]
        _write_state(st)


def _deleted_by_ark(since: float, *, between_calls: bool) -> frozenset:
    if between_calls:          # the user's own `hermes ark prune/forget` in their terminal is recorded on disk
        records = [(d.get("at", 0), d.get("ids", [])) for d in load_state().get("deletions", [])]
    else:
        with _state_lock:
            records = list(_local_deletions)
    out: set[str] = set()
    for at, ids in records:
        if at >= since - 5:
            out.update(ids)
            out.add("*")                                   # some deletion happened: blob counts may drop
    return frozenset(out)


def _resnapshot() -> None:
    """After history was destroyed: take a fresh full snapshot (and push it) right away."""
    def run():
        try:
            full_snapshot(trigger="tamper")
            auto_push()
        except Exception as exc:
            logger.warning("Hermes Ark: snapshot after tamper alarm failed: %s", exc)
    threading.Thread(target=run, name="hermes-ark-tamper-snapshot", daemon=True).start()


# ---- who is acting: agent activity window and the processes the agent started -------------------

AGENT_WINDOW_S = 30 * 60
_activity = {"last": 0.0}
_agent_pids: dict[int, int] = {}            # pid -> session id, of background processes the agent started
_pending_notes: list[str] = []
_watch_lock = threading.Lock()
# Calls proven to damage Ark (during the call, or as a background job running when damage appeared).
# A model that ignores the alarm gets the identical call refused instead of another try.
_quarantine: set[str] = set()
_bg_calls: list[tuple[float, str]] = []


def _call_key(tool_name: str, args: Any) -> str:
    return D.call_key(D._unwrap(tool_name, args)[1])


# All of this also lives on disk: re-enabling Ark after an attack makes Hermes reload the plugin, and a
# fresh module must not forget who was active or which calls are quarantined.
QUARANTINE_TTL_S = 7 * 86400
_loaded = threading.Event()


def _digest(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()


_persist_seen = {"mtime": -1}


def _load_persisted() -> None:
    """Merge what other instances of Ark in this process (a reloaded plugin, or the watchdog that survived
    an unload) or earlier runs wrote: activity time, background processes, quarantine."""
    try:
        mtime = _state_file().stat().st_mtime_ns
    except OSError:
        mtime = 0
    if _loaded.is_set() and mtime == _persist_seen["mtime"]:
        return
    _loaded.set()
    _persist_seen["mtime"] = mtime
    try:
        st = load_state()
    except Exception:
        return
    now = time.time()
    _activity["last"] = max(_activity["last"], float(st.get("agent_activity", 0) or 0))
    with _watch_lock:
        for pid, sid in (st.get("agent_pids") or {}).items():
            try:
                _agent_pids[int(pid)] = int(sid)
            except (TypeError, ValueError):
                continue
        for q in st.get("quarantine") or []:
            if isinstance(q, dict) and now - q.get("at", 0) < QUARANTINE_TTL_S:
                _quarantine.add(q.get("h", ""))


def quarantined(tool_name: str, args: Any) -> bool:
    _load_persisted()
    key = _call_key(tool_name, args)
    if not key:
        return False
    with _watch_lock:
        return _digest(key) in _quarantine


def _quarantine_call(key: str) -> None:
    if not key:
        return
    h = _digest(key)
    with _watch_lock:
        _quarantine.add(h)
    try:
        with _state_guard():
            st = load_state()
            now = time.time()
            q = [x for x in st.get("quarantine") or [] if isinstance(x, dict) and now - x.get("at", 0) < QUARANTINE_TTL_S
                 and x.get("h") != h]
            q.append({"at": now, "h": h})
            st["quarantine"] = q[-200:]
            _write_state(st)
    except Exception as exc:
        logger.debug("Hermes Ark could not persist quarantine: %s", exc)


_persisted_activity = {"at": 0.0}


def _note_agent_activity(result: Any = None, call_key: str = "") -> None:
    _load_persisted()
    now = time.time()
    _activity["last"] = now
    if now - _persisted_activity["at"] > 30:
        _persisted_activity["at"] = now
        try:
            _update_state(agent_activity=now)
        except Exception:
            pass
    if result is None:
        return
    try:
        data = json.loads(result) if isinstance(result, str) else result
        pid = int(data.get("pid")) if isinstance(data, dict) and data.get("pid") else 0
    except (ValueError, TypeError, AttributeError):
        pid = 0
    if pid > 1:
        try:
            sid = os.getsid(pid)
        except (OSError, AttributeError):
            sid = 0
        with _watch_lock:
            _agent_pids[pid] = sid
            if call_key:
                _bg_calls.append((time.time(), call_key))
                del _bg_calls[:-50]
            pids = {str(k): v for k, v in list(_agent_pids.items())[-50:]}
        try:
            _update_state(agent_pids=pids)
        except Exception:
            pass


def _agent_processes_alive() -> bool:
    with _watch_lock:
        tracked = dict(_agent_pids)
    alive = False
    for pid in tracked:
        try:
            os.kill(pid, 0)
            alive = True
            continue
        except OSError:
            pass
        with _watch_lock:
            _agent_pids.pop(pid, None)
    if alive:
        return True
    sids = {sid for sid in tracked.values() if sid and sid != os.getsid(0)}
    if sids and os.path.isdir("/proc"):              # children that outlived their parent keep its session
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                try:
                    if os.getsid(int(entry)) in sids:
                        return True
                except OSError:
                    continue
    return False


def agent_active() -> bool:
    _load_persisted()
    return time.time() - _activity["last"] < AGENT_WINDOW_S or _agent_processes_alive()


def _queue_note(note: str) -> None:
    with _watch_lock:
        _pending_notes.append(note)
        del _pending_notes[:-5]


def _take_notes() -> list[str]:
    with _watch_lock:
        out = list(_pending_notes)
        _pending_notes.clear()
    return out


def _respond(before: dict, after: dict, *, when: str, deleted: frozenset, may_repair: bool,
             alarm_scopes: tuple = D.SCOPES) -> Optional[str]:
    """Raise the tamper alarm. The only repair is Ark's own repository key file, and only when the agent
    caused the damage (*may_repair*); settings, enabled state, secrets and plugin files are never written."""
    changes = D.tamper_changes(before, after, ark_busy=_full_running.is_set(), deleted_by_ark=deleted,
                               scopes=alarm_scopes)
    if not changes:
        return None
    flat = [c for scope in D.SCOPES for c in changes.get(scope, [])]
    restored = D.repair(before, after) if may_repair and "repo" in changes else []
    fresh = may_repair and D.needs_fresh_snapshot(changes)
    logger.warning("Hermes Ark tamper alarm (%s): %s (repaired: %s)", when, "; ".join(flat), restored)
    record_event("tamper", "; ".join(flat) + (f" — repaired {', '.join(restored)}" if restored else ""))
    if fresh:
        _resnapshot()
    return D.format_tamper(flat, restored, when=when, resnapshot=fresh)


def watchdog_tick() -> Optional[str]:
    """Compare Ark with how the agent last left it and raise the tamper alarm on a change. While the agent
    is active every change is reported; otherwise only damage to the backups is (other changes are the
    user's own and are logged as info). Nothing the user can change is ever written back."""
    last = D.last_seen()
    now = D.fingerprint()
    if last is None:
        D.remember(now)
        return None
    if isinstance(last.get("settings"), dict) and last["settings"].get("allow_agent_admin"):
        D.remember(now)
        return None
    deleted = _deleted_by_ark(last.get("at", 0), between_calls=True)
    if last.get("_repo_only"):                            # first look after a restart
        note = _respond(last, now, when="while Hermes was not running", deleted=deleted, may_repair=False,
                        alarm_scopes=("repo",))
    elif agent_active():
        known = tuple(s for s in D.SCOPES if s == "repo" or s in last)   # after a reload: no env/code on record
        note = _respond(last, now, when="while the agent was active (a background job or delayed script?)",
                        deleted=deleted, may_repair=True, alarm_scopes=known)
        if note:
            cutoff = time.time() - AGENT_WINDOW_S
            with _watch_lock:
                suspects = [k for t, k in _bg_calls if t >= cutoff]
            for k in suspects:
                _quarantine_call(k)
    else:
        note = _respond(last, now, when="while the agent was idle", deleted=deleted, may_repair=False,
                        alarm_scopes=("repo",))
        other = D.tamper_changes(last, now, ark_busy=True, scopes=("config", "env", "code"))
        if other:
            record_event("info", "changed while the agent was idle (your own change): "
                         + "; ".join(c for v in other.values() for c in v))
    D.remember(D.fingerprint() if note else now)
    if note:
        _queue_note(note)
    return note


_watchdog_started = threading.Event()
_watchdog_stop = threading.Event()
WATCHDOG_INTERVAL_S = 10.0


def stop_watchdog() -> None:
    """Called when Hermes unloads the plugin (``ctx.on_unload``)."""
    _watchdog_stop.set()


def _ensure_watchdog() -> None:
    """Started by the first agent tool call, so only processes that run the agent are watched. It stops
    as soon as the plugin is unloaded or disabled."""
    if _watchdog_started.is_set() or _watchdog_stop.is_set():
        return
    with _start_lock:
        if _watchdog_started.is_set():
            return
        _watchdog_started.set()

    def loop():
        while not _watchdog_stop.wait(WATCHDOG_INTERVAL_S):
            if not D.is_enabled():
                logger.info("Hermes Ark watchdog: plugin disabled, stopping")
                break
            try:
                watchdog_tick()
            except Exception as exc:
                logger.debug("Hermes Ark watchdog: %s", exc)
        _watchdog_started.clear()

    threading.Thread(target=loop, name="hermes-ark-watchdog", daemon=True).start()


def _tamper_checked(call, tool_name: str = ""):
    """Wrap a tool call so a change to Ark itself — during the call or since the previous one — is
    reported and repaired, however it was disguised."""
    def wrapped(a):
        key = _call_key(tool_name, a)
        notes = []
        try:
            watchdog_tick()                               # what happened since the agent's last action
            notes.extend(_take_notes())
            before = D.fingerprint()
        except Exception as exc:
            logger.debug("Hermes Ark tamper check failed: %s", exc)
            return call(a)
        result = call(a)
        _note_agent_activity(result, key)
        try:
            after = D.fingerprint()
            note = _respond(before, after, when="during this tool call",
                            deleted=_deleted_by_ark(before.get("at", 0), between_calls=False),
                            may_repair=True)
            if note:
                notes.append(note)
                _quarantine_call(key)
                after = D.fingerprint()
            D.remember(after)
        except Exception as exc:
            logger.debug("Hermes Ark tamper check failed: %s", exc)
        if notes:
            return _attach_note(result, "\n".join(notes), key="ark_tamper_alarm")
        return result
    return wrapped


# Per-conversation baseline, so losses spread over several tool calls (deleting skills one by one)
# are caught too. Expires after a quiet hour; each alarm is reported once per conversation.
_BASELINE_TTL_S = 3600
_baselines: dict[str, dict] = {}
_baseline_lock = threading.Lock()


def _conversation_key(ctx: dict) -> str:
    return str(ctx.get("session_id") or ctx.get("task_id") or "default")


def tool_execution(tool_name: str = "", args: Any = None, next_call=None, **ctx: Any) -> Any:
    """Middleware: Ark's self-defense first, then checkpoint before / guard after brain writes."""
    _note_agent_activity()
    try:
        blocked = D.check(tool_name, args)
    except Exception as exc:
        logger.debug("Hermes Ark self-defense check failed: %s", exc)
        blocked = None
    if not blocked and quarantined(tool_name, args) and not _agent_admin():
        blocked = D.QUARANTINE_MESSAGE
    if blocked:
        shown = json.dumps(args, ensure_ascii=False, default=str)[:200] if args is not None else ""
        logger.warning("Hermes Ark blocked an agent %s call: %s", tool_name, shown)
        try:
            record_event("blocked", f"blocked agent {tool_name}: {shown}")
        except Exception:
            pass
        return D.blocked_result(blocked)
    if D.watched(tool_name) and not _agent_admin():
        _ensure_watchdog()
        next_call = _tamper_checked(next_call, tool_name)
    try:
        watched = _touches_brain(tool_name, args)
    except Exception:
        watched = False
    if not watched:
        return next_call(args)
    snapshot_id = before = None
    key = _conversation_key(ctx)
    try:
        st = S.load()
        action = (args or {}).get("action", "") if isinstance(args, dict) else ""
        if st.health_guard:
            before = measure(S.hermes_home())
        snapshot_id = brain_checkpoint(f"before {tool_name} {action}".strip())
        if before is not None:
            now = time.time()
            with _baseline_lock:
                for k in [k for k, b in _baselines.items() if now - b["seen"] > _BASELINE_TTL_S]:
                    del _baselines[k]
                b = _baselines.setdefault(key, {"state": before, "snapshot": snapshot_id, "reported": set(), "seen": now})
                b["seen"] = now
    except Exception as exc:  # never block the tool
        logger.debug("Hermes Ark pre-write checkpoint skipped: %s", exc)
    result = next_call(args)
    if before is None:
        return result
    try:
        after = measure(S.hermes_home())
        alarms = compare(before, after)
        restore_from = snapshot_id
        with _baseline_lock:
            b = _baselines.get(key)
        if b is not None:
            # Cumulative loss since this conversation's first write wins when it is bigger.
            cumulative = compare(b["state"], after)
            if cumulative and len(cumulative) >= len(alarms):
                alarms, restore_from = cumulative, b["snapshot"] or snapshot_id
            fresh = [a for a in alarms if a.message not in b["reported"]]
            b["reported"].update(a.message for a in fresh)
            alarms = fresh
        if alarms:
            note = format_alarms(alarms, restore_from)
            for a in alarms:
                record_event("guard", a.message, restore_from)
            logger.warning("Hermes Ark health guard: %s", "; ".join(a.message for a in alarms))
            return _attach_note(result, note)
    except Exception as exc:
        logger.debug("Hermes Ark guard check failed: %s", exc)
    return result


# ---- agent tool ------------------------------------------------------------------------------

CHECKPOINT_SCHEMA = {
    "name": "ark_checkpoint",
    "description": (
        "Save a restorable snapshot of Hermes itself (memory, SOUL, skills, config, cron, sessions, all "
        "profiles) before you do something risky to them: bulk-editing or deleting memory or skills, "
        "changing config, reorganising cron jobs, or when the user asks for a backup. Returns the snapshot "
        "id; the user can restore with /ark restore <what> <id>. Does not back up project files."),
    "parameters": {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "Short reason, e.g. 'before merging duplicate skills'"},
        },
        "required": ["label"],
    },
}


def checkpoint_available() -> bool:
    try:
        st = S.load()
        return Repository.exists(S.backend(st))
    except Exception:
        return False


def checkpoint_tool(args: dict, **_: Any) -> str:
    import re
    # The label is shown in chats and the Desktop timeline: one plain line, no control characters.
    label = re.sub(r"[\x00-\x1f\x7f]+", " ", str((args or {}).get("label", ""))).strip()[:120] or "agent checkpoint"
    try:
        sid = full_snapshot(trigger="agent", label=label)
        if sid is None:
            return json.dumps({"ok": False, "error": "Hermes Ark is not set up or the repository is locked "
                                                     "(the user must run `hermes ark init` and set ARK_PASSPHRASE)."})
        return json.dumps({"ok": True, "snapshot_id": sid, "label": label,
                           "restore_hint": f"/ark restore <all|memory|skills|skill:NAME|config|cron> {sid}"})
    except LockBusy:
        return json.dumps({"ok": False, "error": "another Ark backup is running; try again in a minute"})
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"})
