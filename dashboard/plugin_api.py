"""Hermes Ark backend for the Desktop page: routes under ``/api/plugins/hermes-ark/``.

The gateway imports this file as a standalone module, so it loads the plugin package explicitly
(reusing the copy the agent already loaded when there is one) and calls the same operations as
``hermes ark`` and ``/ark``. Routes sit behind the dashboard's auth gate. Handlers are plain ``def``
so FastAPI runs them in its thread pool and slow disk/S3 work never blocks the event loop.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

_ROOT = Path(__file__).resolve().parents[1]
router = APIRouter()


def _pkg():
    for name, mod in list(sys.modules.items()):
        if name.startswith("hermes_plugins.") and name.count(".") == 1:
            try:
                if Path(mod.__file__).resolve().parent == _ROOT:
                    return mod
            except (AttributeError, TypeError):
                continue
    name = "hermes_ark_desktop_backend"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


def _ark():
    p = _pkg()
    return p.settings, p.cli, p.auto


def _repo_or_none():
    S, _, _ = _ark()
    repo_mod = sys.modules[S.__name__.rsplit(".", 1)[0] + ".arkcore.repo"]
    st = S.load()
    if not repo_mod.Repository.exists(S.backend(st)):
        return st, None, "not_initialized"
    try:
        return st, S.open_repo(st), None
    except repo_mod.LockedError:
        return st, None, "locked"


def _core(name: str):
    S, _, _ = _ark()
    return sys.modules[S.__name__.rsplit(".", 1)[0] + f".arkcore.{name}"]


def _fail(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=f"{exc.__class__.__name__}: {exc}")


# ---- reads -------------------------------------------------------------------------------------

@router.get("/status")
def status() -> dict[str, Any]:
    S, _, auto = _ark()
    st, repo, problem = _repo_or_none()
    ap = auto.load_state()
    out: dict[str, Any] = {
        "state": problem or "ready",
        "repository": str(st.repository),
        "encrypted": bool(repo and repo.encrypted),
        "autopilot": {"on": st.auto, "interval_minutes": st.interval_minutes, "health_guard": st.health_guard},
        "remote": {"url": st.remote, "auto_push": st.auto_push, "last_push": ap.get("last_push")},
        "last_verify": ap.get("last_verify"),
        "events": list(reversed(ap.get("events", [])))[:20],
        "snapshots": 0,
        "latest": None,
    }
    if repo is not None:
        snaps = _core("snapshot").list_snapshots(repo)
        out["snapshots"] = len(snaps)
        if snaps:
            out["latest"] = snaps[-1].__dict__
            out["total_bytes_latest"] = snaps[-1].bytes
    return out


@router.get("/snapshots")
def snapshots() -> dict[str, Any]:
    _, repo, problem = _repo_or_none()
    if repo is None:
        return {"state": problem, "snapshots": []}
    return {"state": "ready", "snapshots": [s.__dict__ for s in reversed(_core("snapshot").list_snapshots(repo))]}


@router.get("/snapshots/{snapshot_id}")
def snapshot_detail(snapshot_id: str) -> dict[str, Any]:
    _, repo, problem = _repo_or_none()
    if repo is None:
        raise HTTPException(status_code=409, detail=problem)
    try:
        sid = repo.resolve(snapshot_id)
        m = repo.load_manifest(sid)
    except Exception as exc:
        raise _fail(exc)
    groups: dict[tuple, dict] = {}
    for f in m["files"]:
        key = (f.get("pr", "default"), f.get("k", "other"))
        g = groups.setdefault(key, {"profile": key[0], "kind": key[1], "files": 0, "bytes": 0, "items": set()})
        g["files"] += 1
        g["bytes"] += f["s"]
        if f.get("i"):
            g["items"].add(f["i"])
    ids = repo.snapshot_ids()
    prev = ids[ids.index(sid) - 1] if ids.index(sid) > 0 else None
    return {
        "id": sid, "created": m.get("created"), "label": m.get("label"), "trigger": m.get("trigger"),
        "host": m.get("host"), "scope": m.get("scope"), "secrets": m.get("secrets", False), "previous": prev,
        "groups": [{**g, "items": sorted(g["items"])} for g in sorted(groups.values(), key=lambda g: (g["profile"], g["kind"]))],
    }


@router.get("/diff")
def diff(new: str = "latest", old: Optional[str] = None) -> dict[str, Any]:
    _, repo, problem = _repo_or_none()
    if repo is None:
        raise HTTPException(status_code=409, detail=problem)
    try:
        new_id = repo.resolve(new)
        ids = repo.snapshot_ids()
        old_id = repo.resolve(old) if old else (ids[ids.index(new_id) - 1] if ids.index(new_id) > 0 else None)
        if old_id is None:
            return {"old": None, "new": new_id, "changes": []}
        changes = _core("snapshot").diff_manifests(repo.load_manifest(old_id), repo.load_manifest(new_id))
    except Exception as exc:
        raise _fail(exc)
    return {"old": old_id, "new": new_id, "changes": [c.__dict__ for c in changes]}


class RestoreBody(BaseModel):
    what: str = "all"
    snapshot: str = "latest"
    confirm: bool = False


@router.post("/restore/preview")
def restore_preview(body: RestoreBody) -> dict[str, Any]:
    S, cli, _ = _ark()
    st, repo, problem = _repo_or_none()
    if repo is None:
        raise HTTPException(status_code=409, detail=problem)
    restore = _core("restore")
    try:
        sid, note = cli._restore_target(repo, body.snapshot)
        selector = restore.Selector.parse(body.what)
        plan = restore.plan_restore(repo, sid, S.hermes_home(), selector, extra_excludes=S.excludes(st))
    except Exception as exc:
        raise _fail(exc)
    home = S.hermes_home()
    return {
        "snapshot": sid, "note": note, "what": selector.describe(), "unchanged": plan.unchanged,
        "blocked": "sessions_need_stopped_gateway" if plan.touches_sessions_db else None,
        "write": [{"path": e["p"], "kind": e.get("k"), "bytes": e["s"], "action": "revert" if (home / e["p"]).exists() else "recreate"}
                  for e in plan.write],
        "delete": plan.delete,
    }


# ---- actions -----------------------------------------------------------------------------------

def _result(text: str) -> dict[str, Any]:
    ok = not text.startswith(("✗", "🔒"))
    return {"ok": ok, "message": text}


class SnapBody(BaseModel):
    label: str = ""


@router.post("/snapshot")
def take_snapshot(body: SnapBody) -> dict[str, Any]:
    _, cli, _ = _ark()
    return _result(cli._guard(cli.op_snapshot, label=body.label.strip()[:120], trigger="desktop"))


@router.post("/restore")
def restore(body: RestoreBody) -> dict[str, Any]:
    if not body.confirm:
        raise HTTPException(status_code=400, detail="restore needs confirm=true (preview first)")
    _, cli, _ = _ark()
    # from_chat=True: the app talks to a running gateway, so the sessions DB is never swapped live.
    return _result(cli._guard(cli.op_restore, body.what, body.snapshot, yes=True, from_chat=True))


class VerifyBody(BaseModel):
    remote: bool = False
    quick: bool = False


@router.post("/verify")
def verify(body: VerifyBody) -> dict[str, Any]:
    _, cli, _ = _ark()
    return _result(cli._guard(cli.op_verify, remote=body.remote, quick=body.quick))


@router.post("/push")
def push() -> dict[str, Any]:
    _, cli, _ = _ark()
    return _result(cli._guard(cli.op_push))
