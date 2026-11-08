"""``hermes ark ...`` (terminal) and ``/ark ...`` (any chat surface) commands."""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import Optional

from . import settings as S
from .arkcore.backends import BackendError
from .arkcore.lock import LockBusy, repo_lock
from .arkcore.repo import LockedError, Repository, RepoError
from .arkcore.retention import Policy, prune
from .arkcore.sync import SyncError, sync, verify
from .arkcore.restore import RestoreError, Selector, apply_plan, plan_restore
from .arkcore.snapshot import create_snapshot, diff_manifests, human_size, list_snapshots

KIND_ICONS = {"memory": "🧠", "soul": "✨", "skill": "🛠", "config": "⚙", "cron": "⏰", "sessions": "💬",
              "plugin": "🔌", "secrets": "🔑", "media": "🖼", "other": "📄"}


# ---------------------------------------------------------------------------------------------
# Shared operations (return text; never raise for expected user errors)
# ---------------------------------------------------------------------------------------------

def op_status() -> str:
    st = S.load()
    be = S.backend(st)
    if not Repository.exists(be):
        return ("Ark is not set up yet.\n"
                "  Run `hermes ark init` in a terminal to create an encrypted repository.\n"
                f"  Repository location: {st.repository}")
    try:
        repo = S.open_repo(st)
    except LockedError as exc:
        return (f"🔒 {exc}. Add ARK_PASSPHRASE=… to your Hermes .env, then restart the gateway.\n"
                "   Automatic snapshots are paused until then; chats keep working normally.")
    snaps = list_snapshots(repo)
    lines = [f"🛡  Ark repository: {st.repository}",
             f"   encryption: {'AES-256-GCM 🔒' if repo.encrypted else 'off ⚠'} · credentials: never captured",
             f"   snapshots: {len(snaps)}"]
    if snaps:
        last = snaps[-1]
        lines.append(f"   latest: {last.id}  {last.created}  {last.files} files, {human_size(last.bytes)}"
                     + (f"  [{last.label}]" if last.label else ""))
    from . import auto
    ap = auto.load_state()
    lines.append(f"   autopilot: {'on' if st.auto else 'off'} (full snapshot every {st.interval_minutes} min of activity, "
                 f"checkpoint before every memory/skill write) · health guard: {'on' if st.health_guard else 'off'}")
    if st.remote:
        push = ap.get("last_push")
        lines.append(f"   off-site: {st.remote}" + (f" · last push {push['at'][:16].replace('T', ' ')} ({push['objects']} objects)" if push else " · never pushed")
                     + (" · auto-push on" if st.auto_push else ""))
    else:
        lines.append("   off-site: none — set `remote` to an s3:// bucket or a folder, then `hermes ark push`")
    ver = ap.get("last_verify")
    if ver:
        lines.append(f"   last verify: {ver['at'][:16].replace('T', ' ')} {'✓ ok' if ver['ok'] else '✗ problems'} ({ver['where']})")
    guard = [e for e in ap.get("events", []) if e.get("kind") in ("guard", "error", "blocked", "tamper", "info")][-4:]
    for e in guard:
        icon = {"guard": "⚠", "blocked": "⛔", "tamper": "🚨", "info": "ℹ"}.get(e["kind"], "✗")
        lines.append(f"   {icon} {e['at'][:16].replace('T', ' ')}  {e['message']}"
                     + (f"  (saved in {e['snapshot']})" if e.get("snapshot") else ""))
    return "\n".join(lines)


def op_snapshot(label: str = "", trigger: str = "manual", skip_if_unchanged: bool = False) -> str:
    st = S.load()
    repo = S.open_repo(st)
    with repo_lock(S.lock_path(st)):
        res = create_snapshot(repo, S.hermes_home(), label=label, trigger=trigger,
                              extra_excludes=S.excludes(st), skip_if_unchanged=skip_if_unchanged)
    if res.unchanged:
        return f"✓ Nothing changed since {res.snapshot_id}; no new snapshot needed."
    msg = (f"✓ Snapshot {res.snapshot_id}: {res.files} files, {human_size(res.bytes_total)} "
           f"(new data {human_size(res.bytes_new)}) in {res.seconds:.1f}s")
    if res.skipped:
        msg += f"\n⚠ {len(res.skipped)} file(s) could not be read: " + "; ".join(res.skipped[:5])
    return msg


def op_list(limit: int = 20) -> str:
    repo = S.open_repo()
    snaps = list_snapshots(repo)
    if not snaps:
        return "No snapshots yet. Create one with `/ark snap` or `hermes ark snap`."
    lines = [f"{'#':>3}  {'snapshot':<26} {'when (UTC)':<20} {'files':>6} {'size':>9}  trigger / label"]
    for n, s in enumerate(reversed(snaps[-limit:])):
        lines.append(f"{n:>3}  {s.id:<26} {s.created[:19].replace('T', ' '):<20} {s.files:>6} {human_size(s.bytes):>9}  "
                     f"{s.trigger}{' · ' + s.label if s.label else ''}")
    if len(snaps) > limit:
        lines.append(f"… {len(snaps) - limit} older snapshot(s) not shown")
    lines.append("(#0 = latest; refer to one as latest~N or by id prefix)")
    return "\n".join(lines)


def op_diff(old_ref: str = "latest~1", new_ref: str = "latest") -> str:
    repo = S.open_repo()
    old_id, new_id = repo.resolve(old_ref), repo.resolve(new_ref)
    changes = diff_manifests(repo.load_manifest(old_id), repo.load_manifest(new_id))
    if not changes:
        return f"No differences between {old_id} and {new_id}."
    lines = [f"Changes {old_id} → {new_id}:"]
    sign = {"added": "+", "removed": "-", "modified": "~"}
    for c in changes[:60]:
        delta = f"{human_size(c.size_before)} → {human_size(c.size_after)}" if c.status == "modified" else human_size(c.size_after or c.size_before)
        lines.append(f"  {sign[c.status]} {KIND_ICONS.get(c.kind, '📄')} {c.path}  ({delta})")
    if len(changes) > 60:
        lines.append(f"  … and {len(changes) - 60} more")
    return "\n".join(lines)


def op_show(ref: str = "latest") -> str:
    """What a snapshot contains, grouped the way people think about Hermes."""
    repo = S.open_repo()
    sid = repo.resolve(ref)
    m = repo.load_manifest(sid)
    groups: dict[tuple[str, str], list[dict]] = {}
    for f in m["files"]:
        groups.setdefault((f.get("pr", "default"), f.get("k", "other")), []).append(f)
    lines = [f"Snapshot {sid} · {m.get('created', '')} · {m.get('trigger', '')}"
             + (f" · {m['label']}" if m.get("label") else ""),
             f"  {len(m['files'])} files, {human_size(sum(f['s'] for f in m['files']))}"
             + (" · includes credentials 🔑" if m.get("secrets") else "")]
    for (profile, kind) in sorted(groups):
        files = groups[(profile, kind)]
        items = sorted({f.get("i") for f in files if f.get("i")})
        detail = ", ".join(items[:8]) + (f" +{len(items) - 8}" if len(items) > 8 else "") if kind in ("skill", "plugin", "memory") else ""
        lines.append(f"  {KIND_ICONS.get(kind, '📄')} {profile}/{kind}: {len(files)} file(s), {human_size(sum(f['s'] for f in files))}"
                     + (f"  [{detail}]" if detail else ""))
    return "\n".join(lines)


def _gateway_running() -> bool:
    try:
        from gateway.status import live_gateway_pid_for_home
        return live_gateway_pid_for_home(S.hermes_home()) is not None
    except Exception:
        return False


def _restore_target(repo: Repository, ref: str) -> tuple[str, str]:
    """``latest`` means the newest *real* snapshot: safety snapshots taken before a previous restore
    capture the state being replaced, which is rarely what someone recovering a machine wants."""
    if ref not in ("latest", "last", "HEAD"):
        return repo.resolve(ref), ""
    ids = repo.snapshot_ids()
    if not ids:
        repo.resolve(ref)  # raises "no snapshots"
    for sid in reversed(ids):
        if repo.load_manifest(sid).get("trigger") != "pre-restore":
            if sid != ids[-1]:
                return sid, (f"(latest = {sid}, the newest snapshot that is not a pre-restore safety copy; "
                             f"use `{ids[-1]}` to restore that safety copy)")
            return sid, ""
    return ids[-1], ""


def _keep_offsite_settings(before: "S.Settings") -> bool:
    """A restored config.yaml may predate the off-site setup on this machine; keep it working."""
    if not before.remote:
        return False
    after = S.load()
    changed = False
    for key, value in (("remote", before.remote), ("s3_endpoint", before.s3_endpoint), ("s3_region", before.s3_region)):
        if value and getattr(after, key) != value:
            S.set_value(key, value)
            changed = True
    return changed


def op_restore(what: str = "all", ref: str = "latest", *, yes: bool = False, profile: Optional[str] = None,
               from_chat: bool = False) -> str:
    """Preview (default) or apply a restore. Applying always takes a safety snapshot first."""
    st = S.load()
    repo = S.open_repo(st)
    sid, note = _restore_target(repo, ref)
    selector = Selector.parse(what, profile)
    home = S.hermes_home()
    plan = plan_restore(repo, sid, home, selector, extra_excludes=S.excludes(st))
    if plan.empty:
        return f"✓ {selector.describe().capitalize()} already matches snapshot {sid} ({plan.unchanged} file(s)). Nothing to do."
    if plan.touches_sessions_db and (from_chat or _gateway_running()):
        return ("✗ Restoring the sessions database (state.db) needs Hermes to be stopped.\n"
                "  Stop the gateway (`hermes gateway stop`) and run in a terminal:\n"
                f"    hermes ark restore {what} {ref} --yes\n"
                "  Or restore everything except sessions, e.g. `memory`, `skills`, `config`.")
    lines = ([note] if note else []) + [f"Restore {selector.describe()} from {sid}:"]
    for e in plan.write[:25]:
        state = "recreate" if not (home / e["p"]).exists() else "revert"
        lines.append(f"  ↺ {KIND_ICONS.get(e.get('k', 'other'), '📄')} {e['p']}  ({state}, {human_size(e['s'])})")
    for rel in plan.delete[:25]:
        lines.append(f"  ✗ {rel}  (did not exist in that snapshot)")
    more = len(plan.write) + len(plan.delete) - min(len(plan.write), 25) - min(len(plan.delete), 25)
    if more > 0:
        lines.append(f"  … and {more} more")
    if not yes:
        cmd = f"/ark restore {what} {ref} yes" if from_chat else f"hermes ark restore {what} {ref} --yes"
        lines.append(f"\nNothing changed yet. To apply, run: {cmd}\n"
                     "(A safety snapshot is taken first, so this restore can itself be undone.)")
        return "\n".join(lines)
    with repo_lock(S.lock_path(st)):
        safety = create_snapshot(repo, home, label=f"before restoring {selector.describe()} ({sid[:15]})",
                                 trigger="pre-restore", extra_excludes=S.excludes(st))
        res = apply_plan(repo, plan, home)
    kept = _keep_offsite_settings(st)
    lines.append(f"\n✓ Restored {res.written} file(s), removed {res.deleted}. "
                 f"Safety snapshot: {safety.snapshot_id} (undo with `restore {what} {safety.snapshot_id}`).")
    if kept:
        lines.append("  Kept this machine's Ark off-site settings (remote, endpoint) in the restored config.")
    if any(e.get("k") in ("memory", "soul", "skill", "config") for e in plan.write):
        lines.append("  New sessions pick up the restored memory/skills/config; start a new chat (/new) to use them now.")
    return "\n".join(lines)


def op_prune(dry_run: bool = False, forget: Optional[list] = None) -> str:
    from . import auto
    st = S.load()
    repo = S.open_repo(st)
    with repo_lock(S.lock_path(st)):
        res = prune(repo, Policy.from_settings(S.get), dry_run=dry_run, only=forget or None,
                    protect=() if forget else auto.protected_snapshots())
    if not dry_run and (res.removed_snapshots or res.removed_blobs):
        auto.record_deletions(res.removed_snapshots)
    verb = "Would remove" if dry_run else "Removed"
    lines = [f"{verb} {len(res.removed_snapshots)} snapshot(s) and {res.removed_blobs} unused blob(s), "
             f"{'freeing' if not dry_run else 'which would free'} {human_size(res.freed_bytes)}. Kept {res.kept}."]
    lines += [f"  - {sid}" for sid in res.removed_snapshots[:20]]
    if not forget:
        p = Policy.from_settings(S.get)
        lines.append(f"Policy: last {p.keep_last} · hourly {p.hourly} · daily {p.daily} · weekly {p.weekly} · "
                     f"monthly {p.monthly} · labelled manual snapshots kept")
    return "\n".join(lines)


def op_push(mirror: bool = False) -> str:
    st = S.load()
    remote = S.remote_backend(st)
    S.open_repo(st)  # fail early on a locked/wrong passphrase
    res = sync(S.backend(st), remote, mirror=mirror)
    from . import auto
    auto.record_push(res.copied, res.bytes, remote.label)
    warn = "" if S.open_repo(st).encrypted else ("\n⚠ This repository is NOT encrypted: the off-site copy is readable by anyone with "
                                                 "access to it and cannot detect tampering. Re-create it with `hermes ark init` (encrypted).")
    if not res.copied and not res.deleted:
        return f"✓ {remote.label} is up to date ({res.skipped} objects)." + warn
    return (f"✓ Pushed to {remote.label}: {res.copied} object(s), {human_size(res.bytes)}"
            f" · {len(res.snapshots_copied)} new snapshot(s)" + (f" · removed {res.deleted} stale object(s)" if res.deleted else "") + warn)


def op_pull(remote_url: str = "", endpoint: str = "", region: str = "") -> str:
    """Bring the off-site copy here (new machine, or after losing the disk)."""
    st = S.load()
    remote = S.remote_backend(st, remote_url, endpoint, region)
    if not Repository.exists(remote):
        return f"✗ No Ark repository at {remote.label}."
    Repository.open(remote, S.passphrase())          # verify the passphrase before copying anything
    res = sync(remote, S.backend(st))
    if remote_url:
        S.set_value("remote", remote_url)
        if endpoint:
            S.set_value("s3_endpoint", endpoint)
        if region:
            S.set_value("s3_region", region)
    S._cache.clear()
    repo = S.open_repo(st)
    snaps = repo.snapshot_ids()
    latest = _restore_target(repo, "latest")[0] if snaps else "-"
    return (f"✓ Pulled {res.copied} object(s), {human_size(res.bytes)} from {remote.label} into {st.repository}.\n"
            f"  {len(snaps)} snapshot(s) available, latest {latest}.\n"
            "  Next: stop the gateway, then `hermes ark restore all latest` to preview and `--yes` to bring this Hermes back.")


def op_verify(remote: bool = False, quick: bool = False) -> str:
    st = S.load()
    if remote:
        be = S.remote_backend(st)
        repo = Repository.open(be, S.passphrase())
        where = be.label
    else:
        repo = S.open_repo(st)
        where = str(st.repository)
    rep = verify(repo, read_data=not quick)
    from . import auto
    auto.record_verify(where, rep.ok, rep.snapshots, len(rep.damaged_snapshots()))
    how = "existence of every blob" if quick else "decrypted and re-hashed every blob"
    if rep.ok:
        return (f"✓ Verified {where}: {rep.snapshots} snapshot(s) restorable — {how} "
                f"({rep.blobs_checked} blobs, {human_size(rep.bytes_checked)})."
                + (f" {rep.unreferenced} unused blob(s) can be freed with `prune`." if rep.unreferenced else ""))
    lines = [f"✗ Problems in {where}: {len(rep.missing)} missing and {len(rep.corrupt)} damaged blob(s), "
             f"{len(rep.unreadable_snapshots)} unreadable snapshot(s)."]
    damaged = rep.damaged_snapshots()
    lines.append(f"  Affected snapshots ({len(damaged)}): " + ", ".join(damaged[:10]) + (" …" if len(damaged) > 10 else ""))
    good = [s for s in repo.snapshot_ids() if s not in damaged]
    if good:
        lines.append(f"  Newest intact snapshot: {good[-1]}")
    if not remote:
        lines.append("  Fix it with `hermes ark repair` (re-fetches damaged and missing data from your off-site copy).")
    return "\n".join(lines)


def op_repair() -> str:
    """Replace damaged or missing local data with clean copies from the off-site remote."""
    st = S.load()
    remote = S.remote_backend(st)
    with repo_lock(S.lock_path(st)):
        repo = S.open_repo(st)
        before = verify(repo)
        if before.ok:
            return f"✓ Nothing to repair: {before.snapshots} snapshot(s) verified."
        for blob_id in before.corrupt:
            repo.delete_blob(blob_id)                     # so the sync below fetches a clean copy
        Repository.open(remote, S.passphrase())           # same passphrase, same repository
        res = sync(remote, S.backend(st))
        after = verify(repo)
    if after.ok:
        return (f"✓ Repaired: replaced {len(before.corrupt)} damaged and {len(before.missing)} missing blob(s) "
                f"({res.copied} object(s) from {remote.label}). All {after.snapshots} snapshot(s) restorable again.")
    return (f"✗ Still damaged after repair: {len(after.corrupt)} damaged, {len(after.missing)} missing blob(s) — the off-site "
            f"copy lacks them too. Intact snapshots: {after.snapshots - len(after.damaged_snapshots())} of {after.snapshots}.")


def _guard(fn, *a, **kw) -> str:
    try:
        return fn(*a, **kw)
    except LockedError as exc:
        return f"🔒 {exc}."
    except LockBusy as exc:
        return f"✗ {exc}; try again in a moment."
    except (RepoError, RestoreError, SyncError, BackendError, ValueError, OSError) as exc:
        return f"✗ {exc}"


# ---------------------------------------------------------------------------------------------
# /ark slash command
# ---------------------------------------------------------------------------------------------

SLASH_HELP = """/ark — encrypted, versioned backups of your Hermes

  /ark                 status of your backups
  /ark snap [label]    take a snapshot now
  /ark list            list snapshots
  /ark diff [A] [B]    what changed between two snapshots (default: previous → latest)
  /ark show [snap]     what a snapshot contains
  /ark prune [dry]     apply the retention policy and free space
  /ark push            copy new snapshots to the off-site remote
  /ark verify [quick] [remote]  prove the backups can be restored
  /ark repair          re-fetch damaged data from the off-site copy
  /ark restore <what> [snap] [yes]
                       preview, then restore: all · memory · soul · skills · skill:<name>
                       · config · cron · plugin:<name> · file:<path> · <profile>/memory

Set up once in a terminal: hermes ark init"""


def handle_slash(raw_args: str = "", **_: object) -> str:
    argv = (raw_args or "").strip().split()
    if not argv or argv[0] == "status":
        return _guard(op_status)
    cmd, rest = argv[0], argv[1:]
    if cmd in ("help", "-h", "--help"):
        return SLASH_HELP
    if cmd in ("snap", "snapshot", "backup"):
        return _guard(op_snapshot, label=" ".join(rest), trigger="chat")
    if cmd in ("list", "ls", "log"):
        return _guard(op_list)
    if cmd == "diff":
        return _guard(op_diff, *(rest[:2] or []))
    if cmd == "show":
        return _guard(op_show, *(rest[:1] or []))
    if cmd == "push":
        return _guard(op_push)
    if cmd == "repair":
        return _guard(op_repair)
    if cmd == "verify":
        return _guard(op_verify, remote="remote" in rest, quick="quick" in rest)
    if cmd == "prune":
        return _guard(op_prune, dry_run=bool(rest) and rest[0] in ("dry", "dry-run", "--dry-run"))
    if cmd in ("restore", "rewind"):
        yes = bool(rest) and rest[-1].lower() in ("yes", "--yes", "-y", "confirm")
        args = rest[:-1] if yes else rest
        if not args:
            return "Usage: /ark restore <what> [snapshot] [yes]   e.g. /ark restore memory latest~1"
        return _guard(op_restore, args[0], args[1] if len(args) > 1 else "latest", yes=yes, from_chat=True)
    return f"Unknown subcommand {cmd!r}.\n\n{SLASH_HELP}"


# ---------------------------------------------------------------------------------------------
# hermes ark (argparse)
# ---------------------------------------------------------------------------------------------

def setup_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="ark_command")
    # No abbreviated flags: ``--ye`` must not silently mean ``--yes`` (it also defeats command screening).
    _add_parser = sub.add_parser
    sub.add_parser = lambda *a, **kw: _add_parser(*a, **{"allow_abbrev": False, **kw})  # type: ignore[method-assign]
    p = sub.add_parser("init", help="Create the backup repository")
    p.add_argument("--no-encryption", action="store_true", help="Create an unencrypted repository (not recommended)")
    sub.add_parser("status", help="Show repository status")
    p = sub.add_parser("snap", help="Take a snapshot now")
    p.add_argument("label", nargs="*", help="Optional label")
    p.add_argument("--if-changed", action="store_true", help="Skip when nothing changed since the latest snapshot")
    p = sub.add_parser("list", help="List snapshots")
    p.add_argument("-n", type=int, default=20)
    p = sub.add_parser("diff", help="Show what changed between two snapshots")
    p.add_argument("old", nargs="?", default="latest~1")
    p.add_argument("new", nargs="?", default="latest")
    p = sub.add_parser("show", help="Show what a snapshot contains")
    p.add_argument("snapshot", nargs="?", default="latest")
    p = sub.add_parser("prune", help="Apply the retention policy and delete unused data")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("push", help="Copy new snapshots to the off-site remote")
    p.add_argument("--mirror", action="store_true", help="Also delete remote objects that were pruned locally")
    p = sub.add_parser("pull", help="Fetch the off-site repository to this machine (new machine / disaster recovery)")
    p.add_argument("--remote", default="", help="s3://bucket/prefix or a folder (saved to settings)")
    p.add_argument("--endpoint", default="", help="S3 endpoint, e.g. https://<account>.r2.cloudflarestorage.com")
    p.add_argument("--region", default="", help="S3 region (R2: auto)")
    p = sub.add_parser("verify", help="Prove every snapshot can be restored")
    p.add_argument("--remote", action="store_true", help="Verify the off-site copy instead of the local one")
    p.add_argument("--quick", action="store_true", help="Only check that every blob exists")
    sub.add_parser("repair", help="Re-fetch damaged or missing data from the off-site copy")
    p = sub.add_parser("forget", help="Delete specific snapshots")
    p.add_argument("snapshots", nargs="+")
    p = sub.add_parser("restore", help="Restore everything or one item (preview unless --yes)")
    p.add_argument("what", nargs="?", default="all",
                   help="all | memory | soul | skills | skill:<name> | config | cron | sessions | plugin:<name> | file:<path> | <profile>/<what>")
    p.add_argument("snapshot", nargs="?", default="latest")
    p.add_argument("--profile", help="Limit to one profile")
    p.add_argument("-y", "--yes", action="store_true", help="Apply (otherwise only preview)")


def _ask_new_passphrase() -> Optional[str]:
    env = S.passphrase()
    if env:
        print("Using ARK_PASSPHRASE from your Hermes secrets.")
        return env
    if not sys.stdin.isatty():
        print("✗ No ARK_PASSPHRASE set and no terminal to ask for one. Set ARK_PASSPHRASE or pass --no-encryption.")
        return None
    while True:
        p1 = getpass.getpass("New repository passphrase (min 8 chars): ")
        if len(p1) < 8:
            print("Too short.")
            continue
        if getpass.getpass("Repeat passphrase: ") != p1:
            print("Passphrases do not match.")
            continue
        print("\nIMPORTANT: save this passphrase somewhere safe. Without it the backups cannot be restored.\n"
              "Also store it as ARK_PASSPHRASE so automatic snapshots can run, e.g.\n"
              "  echo 'ARK_PASSPHRASE=...' >> ~/.hermes/.env\n")
        return p1


def op_init(no_encryption: bool) -> str:
    st = S.load()
    be = S.backend(st)
    if Repository.exists(be):
        return f"A repository already exists at {st.repository}."
    if no_encryption and S.passphrase():
        return ("✗ ARK_PASSPHRASE is set, so Ark expects an encrypted repository. Remove it from your .env to create "
                "an unencrypted one (not recommended), or drop --no-encryption.")
    pw = None if no_encryption else _ask_new_passphrase()
    if pw and len(pw) < 8:
        return "✗ ARK_PASSPHRASE is too short: use at least 8 characters (a long phrase is best)."
    if not no_encryption and not pw:
        return "✗ Repository not created."
    repo = Repository.init(be, pw)
    return (f"✓ Created {'encrypted ' if repo.encrypted else ''}Ark repository at {st.repository}\n"
            "  Next: `hermes ark snap` to take the first snapshot.")


def run_cli(args: argparse.Namespace) -> int:
    cmd = getattr(args, "ark_command", None) or "status"
    if cmd == "init":
        out = _guard(op_init, args.no_encryption)
    elif cmd == "status":
        out = _guard(op_status)
    elif cmd == "snap":
        out = _guard(op_snapshot, label=" ".join(args.label), trigger="cli", skip_if_unchanged=args.if_changed)
    elif cmd == "list":
        out = _guard(op_list, args.n)
    elif cmd == "diff":
        out = _guard(op_diff, args.old, args.new)
    elif cmd == "show":
        out = _guard(op_show, args.snapshot)
    elif cmd == "prune":
        out = _guard(op_prune, dry_run=args.dry_run)
    elif cmd == "repair":
        out = _guard(op_repair)
    elif cmd == "push":
        out = _guard(op_push, mirror=args.mirror)
    elif cmd == "pull":
        out = _guard(op_pull, args.remote, args.endpoint, args.region)
    elif cmd == "verify":
        out = _guard(op_verify, remote=args.remote, quick=args.quick)
    elif cmd == "forget":
        out = _guard(op_prune, forget=args.snapshots)
    elif cmd == "restore":
        out = _guard(op_restore, args.what, args.snapshot, yes=args.yes, profile=args.profile)
    else:
        out = f"unknown command {cmd}"
    print(out)
    return 1 if out.startswith(("✗", "🔒")) else 0
