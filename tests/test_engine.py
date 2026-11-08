import json
import sqlite3

import pytest

from arkcore import crypto
from arkcore.backends import BackendError, LocalBackend
from arkcore.repo import LockedError, Repository, RepoError
from arkcore.scanner import classify, scan
from arkcore.snapshot import create_snapshot, diff_manifests, list_snapshots, read_file_from_snapshot


def rels(home, **kw):
    return {e.rel for e in scan(home, **kw)}


# ---- scanner ----------------------------------------------------------------------------------

def test_scan_keeps_user_data_and_skips_regenerable(hermes_home):
    r = rels(hermes_home)
    assert {"memories/MEMORY.md", "SOUL.md", "config.yaml", "skills/weather/SKILL.md", "cron/jobs.json",
            "state.db", "cache/images/cat.png", "profiles/work/memories/MEMORY.md"} <= r
    assert "logs/agent.log" not in r
    assert "cache/models_dev_cache.json" not in r
    assert not any("__pycache__" in p for p in r)


def test_scan_never_captures_credentials(hermes_home):
    """Review #149640: Hermes credential files are never read, opt-in or not."""
    r = rels(hermes_home)
    assert ".env" not in r and "auth.json" not in r and "profiles/work/.env" not in r
    import inspect
    from arkcore.scanner import scan
    assert "include_secrets" not in inspect.signature(scan).parameters


def test_scan_skips_symlinks_and_own_repo(hermes_home, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("not hermes")
    (hermes_home / "link.txt").symlink_to(outside)
    (hermes_home / "plugin-data" / "hermes-ark" / "repo").mkdir(parents=True)
    (hermes_home / "plugin-data" / "hermes-ark" / "repo" / "ark.json").write_text("{}")
    (hermes_home / "plugin-data" / "other").mkdir(parents=True)
    (hermes_home / "plugin-data" / "other" / "state.json").write_text("{}")
    r = rels(hermes_home)
    assert "link.txt" not in r
    assert not any(p.startswith("plugin-data/hermes-ark") for p in r)
    assert "plugin-data/other/state.json" in r


def test_scan_extra_excludes(hermes_home):
    assert not any(p.startswith("skills/weather") for p in rels(hermes_home, extra_excludes=["skills/weather"]))


def test_classify():
    assert classify("memories/MEMORY.md") == ("default", "memory", "MEMORY.md")
    assert classify("skills/weather/SKILL.md") == ("default", "skill", "weather")
    assert classify("profiles/work/SOUL.md") == ("work", "soul", "")
    assert classify("state.db") == ("default", "sessions", "")
    assert classify("cron/jobs.json") == ("default", "cron", "")


# ---- crypto -----------------------------------------------------------------------------------

def test_crypto_roundtrip_and_tamper():
    block, keys = crypto.new_encryption_block("correct horse")
    sealed = crypto.seal(keys.enc, b"hello", b"aad")
    assert crypto.open_sealed(keys.enc, sealed, b"aad") == b"hello"
    with pytest.raises(crypto.ArkCryptoError):
        crypto.open_sealed(keys.enc, sealed[:-1] + bytes([sealed[-1] ^ 1]), b"aad")
    with pytest.raises(crypto.ArkCryptoError):
        crypto.open_sealed(keys.enc, sealed, b"other-aad")
    assert crypto.unlock(block, "correct horse") == keys
    with pytest.raises(crypto.ArkCryptoError):
        crypto.unlock(block, "wrong")


def test_rewrap_keeps_data_readable():
    block, keys = crypto.new_encryption_block("old-pass")
    new_block = crypto.rewrap(block, keys, "new-pass")
    assert crypto.unlock(new_block, "new-pass") == keys
    with pytest.raises(crypto.ArkCryptoError):
        crypto.unlock(new_block, "old-pass")


# ---- repository + snapshots -------------------------------------------------------------------

def test_encrypted_repo_hides_plaintext(hermes_home, tmp_path):
    repo = Repository.init(LocalBackend(tmp_path / "repo"), "pass-phrase")
    create_snapshot(repo, hermes_home)
    raw = b"".join(p.read_bytes() for p in (tmp_path / "repo").rglob("*") if p.is_file())
    assert b"Building Hermes Ark".lower() not in raw.lower()
    assert b"Likes Arabic" not in raw
    assert b"pass-phrase" not in raw
    cfg = json.loads((tmp_path / "repo" / "ark.json").read_text())
    assert "wrapped_key" in cfg["encryption"] and "key" not in cfg


def test_open_requires_right_passphrase(tmp_path):
    be = LocalBackend(tmp_path / "repo")
    Repository.init(be, "s3cret-pass")
    with pytest.raises(LockedError):
        Repository.open(be, None)
    with pytest.raises(LockedError):
        Repository.open(be, "nope")
    assert Repository.open(be, "s3cret-pass").encrypted


def test_init_twice_refused(tmp_path):
    be = LocalBackend(tmp_path / "repo")
    Repository.init(be, None)
    with pytest.raises(RepoError):
        Repository.init(be, None)


def test_snapshot_dedup_and_diff(hermes_home, tmp_path):
    repo = Repository.init(LocalBackend(tmp_path / "repo"), "pw-12345678")
    first = create_snapshot(repo, hermes_home, label="first")
    assert first.files > 5 and first.blobs_new > 0
    second = create_snapshot(repo, hermes_home)
    assert second.blobs_new == 0 and second.bytes_new == 0          # nothing re-stored
    assert create_snapshot(repo, hermes_home, skip_if_unchanged=True).unchanged

    (hermes_home / "memories" / "MEMORY.md").write_text("")        # memory wiped!
    (hermes_home / "skills" / "new-skill").mkdir()
    (hermes_home / "skills" / "new-skill" / "SKILL.md").write_text("# new\n")
    (hermes_home / "SOUL.md").unlink()
    third = create_snapshot(repo, hermes_home)
    assert third.blobs_new >= 1

    changes = {c.path: c for c in diff_manifests(repo.load_manifest(second.snapshot_id), repo.load_manifest(third.snapshot_id))}
    assert changes["memories/MEMORY.md"].status == "modified" and changes["memories/MEMORY.md"].kind == "memory"
    assert changes["memories/MEMORY.md"].size_after == 0
    assert changes["skills/new-skill/SKILL.md"].status == "added"
    assert changes["SOUL.md"].status == "removed"
    assert len(list_snapshots(repo)) == 3
    assert repo.resolve("latest") == third.snapshot_id
    assert repo.resolve("latest~2") == first.snapshot_id


def test_snapshot_content_roundtrip_including_live_sqlite(hermes_home, tmp_path):
    live = sqlite3.connect(hermes_home / "state.db")           # keep a writer open (WAL)
    live.execute("insert into messages(body) values ('uncheckpointed')")
    live.commit()
    repo = Repository.init(LocalBackend(tmp_path / "repo"), "pw-12345678")
    res = create_snapshot(repo, hermes_home)
    live.close()
    m = repo.load_manifest(res.snapshot_id)
    files = {f["p"]: f for f in m["files"]}
    assert read_file_from_snapshot(repo, files["memories/MEMORY.md"]) == (hermes_home / "memories" / "MEMORY.md").read_bytes()
    out = tmp_path / "restored.db"
    out.write_bytes(read_file_from_snapshot(repo, files["state.db"]))
    rows = sqlite3.connect(out).execute("select count(*), max(body) from messages").fetchone()
    assert rows[0] == 201                                      # WAL content captured consistently


def test_large_file_is_chunked_and_partially_deduped(hermes_home, tmp_path):
    import os
    big = bytearray(os.urandom(6 << 20))
    (hermes_home / "sessions").mkdir(exist_ok=True)
    (hermes_home / "sessions" / "big.bin").write_bytes(big)
    repo = Repository.init(LocalBackend(tmp_path / "repo"), None)
    create_snapshot(repo, hermes_home)
    big[100] ^= 0xFF                                           # touch one byte in the first MiB
    (hermes_home / "sessions" / "big.bin").write_bytes(big)
    res = create_snapshot(repo, hermes_home)
    assert res.blobs_new == 1 and res.bytes_new == 1 << 20     # only that chunk re-stored


def test_credentials_never_captured_even_in_an_encrypted_repo(hermes_home, tmp_path):
    repo = Repository.init(LocalBackend(tmp_path / "repo"), "pw-12345678")
    sid = create_snapshot(repo, hermes_home).snapshot_id
    m = repo.load_manifest(sid)
    paths = {e["p"] for e in m["files"]}
    assert not paths & {".env", "auth.json", "profiles/work/.env"} and m["secrets"] is False
    with pytest.raises(TypeError):
        create_snapshot(repo, hermes_home, include_secrets=True)


def test_corrupted_blob_detected(hermes_home, tmp_path):
    repo = Repository.init(LocalBackend(tmp_path / "repo"), None)
    res = create_snapshot(repo, hermes_home)
    entry = next(f for f in repo.load_manifest(res.snapshot_id)["files"] if f["p"] == "SOUL.md")
    blob_path = tmp_path / "repo" / "blobs" / entry["b"][0][:2] / entry["b"][0]
    data = bytearray(blob_path.read_bytes())
    data[-1] ^= 0x01
    blob_path.write_bytes(bytes(data))
    with pytest.raises(Exception):
        read_file_from_snapshot(repo, entry)


def test_backend_rejects_path_traversal(tmp_path):
    be = LocalBackend(tmp_path / "repo")
    for bad in ("../x", "/etc/passwd", "a/../../b", "a\\b", ""):
        with pytest.raises(BackendError):
            be.write(bad, b"x")


def test_scan_skips_gateway_runtime_and_webapp_token(hermes_home):
    for rel in ("webapp/session-token", "state/gateway.heartbeat", "runtime/x/.lock", "install_id",
                "models_dev_cache.json", "gateway_state.json", "cron/ticker_heartbeat",
                "plugin-update-checks/results.json", "desktop-plugins/pkg/plugin.js", "desktop-plugins/pkg/.hermes-package.json"):
        p = hermes_home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    (hermes_home / "desktop-plugins" / "mine").mkdir()
    (hermes_home / "desktop-plugins" / "mine" / "plugin.js").write_text("export default {}")
    r = rels(hermes_home)
    assert not any(p.startswith(("webapp/", "state/", "runtime/", "plugin-update-checks/", "desktop-plugins/pkg")) for p in r)
    assert not {"install_id", "models_dev_cache.json", "gateway_state.json", "cron/ticker_heartbeat"} & r
    assert "desktop-plugins/mine/plugin.js" in r          # a hand-written desktop plugin is user data
    assert "cron/jobs.json" in r and "state.db" in r


def test_diff_between_brain_and_full_snapshots_only_compares_common_kinds(hermes_home, tmp_path):
    from arkcore.snapshot import BRAIN
    repo = Repository.init(LocalBackend(tmp_path / "r"), None)
    full = create_snapshot(repo, hermes_home).snapshot_id
    (hermes_home / "memories" / "MEMORY.md").write_text("changed")
    brain = create_snapshot(repo, hermes_home, scope=BRAIN).snapshot_id
    changes = diff_manifests(repo.load_manifest(full), repo.load_manifest(brain))
    assert [c.path for c in changes] == ["memories/MEMORY.md"]          # no fake "state.db removed"


def test_scan_skips_hermes_internal_caches(hermes_home):
    for rel in (".clean_shutdown", ".skills_prompt_snapshot.json", ".curator_backups/blobs/ab12"):
        p = hermes_home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    (hermes_home / "skills" / ".bundled_manifest").write_text("x")
    r = rels(hermes_home)
    assert not {".clean_shutdown", ".skills_prompt_snapshot.json"} & r
    assert not any(p.startswith(".curator_backups") for p in r)
    assert "skills/.bundled_manifest" in r and classify("skills/.bundled_manifest")[2] == ""
