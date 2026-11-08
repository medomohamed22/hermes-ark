import sqlite3

import pytest

from arkcore.backends import LocalBackend
from arkcore.repo import Repository
from arkcore.restore import RestoreError, Selector, apply_plan, plan_restore
from arkcore.scanner import scan
from arkcore.snapshot import create_snapshot


@pytest.fixture
def repo(tmp_path):
    return Repository.init(LocalBackend(tmp_path / "repo"), "pw-12345678")


def tree(home):
    return {e.rel: e.path.read_bytes() for e in scan(home) if not e.rel.endswith(".db")}


def test_selector_parse():
    assert Selector.parse("all") == Selector()
    assert Selector.parse("memories").kind == "memory"
    assert Selector.parse("skills") == Selector(kind="skill")
    assert Selector.parse("skill:weather") == Selector(kind="skill", item="weather")
    assert Selector.parse("work/memory") == Selector(kind="memory", profile="work")
    assert Selector.parse("file:skills/weather/SKILL.md").path == "skills/weather/SKILL.md"
    with pytest.raises(RestoreError):
        Selector.parse("bogus")
    with pytest.raises(RestoreError):
        Selector.parse("file:../etc/passwd")


def test_restore_wiped_memory_only(hermes_home, repo):
    good = create_snapshot(repo, hermes_home).snapshot_id
    original_memory = (hermes_home / "memories" / "MEMORY.md").read_bytes()
    (hermes_home / "memories" / "MEMORY.md").write_text("")          # the agent "cleaned up" its memory
    (hermes_home / "skills" / "weather" / "SKILL.md").write_text("# changed on purpose\n")

    plan = plan_restore(repo, good, hermes_home, Selector.parse("memory"))
    assert [e["p"] for e in plan.write] == ["memories/MEMORY.md"] and not plan.delete
    assert plan.unchanged >= 1                                        # USER.md untouched
    apply_plan(repo, plan, hermes_home)
    assert (hermes_home / "memories" / "MEMORY.md").read_bytes() == original_memory
    # Out of scope: the deliberate skill edit is kept.
    assert (hermes_home / "skills" / "weather" / "SKILL.md").read_text() == "# changed on purpose\n"


def test_restore_single_skill_and_remove_added_file(hermes_home, repo):
    good = create_snapshot(repo, hermes_home).snapshot_id
    skill = hermes_home / "skills" / "weather"
    (skill / "SKILL.md").unlink()
    (skill / "broken.py").write_text("oops")
    plan = plan_restore(repo, good, hermes_home, Selector.parse("skill:weather"))
    assert [e["p"] for e in plan.write] == ["skills/weather/SKILL.md"]
    assert plan.delete == ["skills/weather/broken.py"]
    apply_plan(repo, plan, hermes_home)
    assert (skill / "SKILL.md").read_text().startswith("# Weather")
    assert not (skill / "broken.py").exists()


def test_restore_deleted_skill_folder_and_profile_scope(hermes_home, repo):
    good = create_snapshot(repo, hermes_home).snapshot_id
    import shutil
    shutil.rmtree(hermes_home / "skills" / "weather")
    (hermes_home / "profiles" / "work" / "memories" / "MEMORY.md").write_text("")
    (hermes_home / "memories" / "MEMORY.md").write_text("main changed\n")
    plan = plan_restore(repo, good, hermes_home, Selector.parse("work/memory"))
    assert [e["p"] for e in plan.write] == ["profiles/work/memories/MEMORY.md"]
    apply_plan(repo, plan, hermes_home)
    assert (hermes_home / "memories" / "MEMORY.md").read_text() == "main changed\n"   # other profile untouched
    apply_plan(repo, plan_restore(repo, good, hermes_home, Selector.parse("skills")), hermes_home)
    assert (hermes_home / "skills" / "weather" / "SKILL.md").exists()


def test_full_restore_roundtrip(hermes_home, repo):
    before = tree(hermes_home)
    good = create_snapshot(repo, hermes_home).snapshot_id
    for p in list(hermes_home.rglob("*.md")):
        p.write_text("garbage")
    (hermes_home / "config.yaml").unlink()
    (hermes_home / "skills" / "evil").mkdir()
    (hermes_home / "skills" / "evil" / "SKILL.md").write_text("bad")
    apply_plan(repo, plan_restore(repo, good, hermes_home, Selector()), hermes_home)
    assert tree(hermes_home) == before
    assert not (hermes_home / "skills" / "evil").exists()             # empty dir pruned too


def test_restore_never_deletes_uncaptured_credentials(hermes_home, repo):
    good = create_snapshot(repo, hermes_home).snapshot_id               # secrets NOT captured
    apply_plan(repo, plan_restore(repo, good, hermes_home, Selector()), hermes_home)
    assert (hermes_home / ".env").read_text().startswith("OPENROUTER_API_KEY")
    assert (hermes_home / "auth.json").exists()


def test_restore_never_writes_credentials_from_an_older_snapshot(hermes_home, repo, monkeypatch):
    """A snapshot made by an older Ark with include_secrets still holds .env: it is never written back."""
    import arkcore.scanner as scanner
    monkeypatch.setattr(scanner, "_is_secret", lambda rel: False)           # behave like the old option
    old = create_snapshot(repo, hermes_home).snapshot_id
    monkeypatch.undo()
    assert ".env" in {e["p"] for e in repo.load_manifest(old)["files"]}
    (hermes_home / ".env").write_text("OPENROUTER_API_KEY=new-key\n")
    (hermes_home / "auth.json").unlink()
    plan = plan_restore(repo, old, hermes_home, Selector())
    assert not [e for e in plan.write if e["p"] in (".env", "auth.json", "profiles/work/.env")]
    assert ".env" not in plan.delete and "auth.json" not in plan.delete
    apply_plan(repo, plan, hermes_home)
    assert (hermes_home / ".env").read_text() == "OPENROUTER_API_KEY=new-key\n"     # untouched
    assert not (hermes_home / "auth.json").exists()                                  # not resurrected


def test_restore_sqlite_database(hermes_home, repo):
    good = create_snapshot(repo, hermes_home).snapshot_id
    db = sqlite3.connect(hermes_home / "state.db")
    db.execute("delete from messages")
    db.commit()
    db.close()
    plan = plan_restore(repo, good, hermes_home, Selector.parse("sessions"))
    assert plan.touches_sessions_db
    apply_plan(repo, plan, hermes_home)
    assert sqlite3.connect(hermes_home / "state.db").execute("select count(*) from messages").fetchone()[0] == 200


def test_damaged_backup_aborts_before_touching_disk(hermes_home, repo, tmp_path):
    good = create_snapshot(repo, hermes_home).snapshot_id
    for p in (tmp_path / "repo" / "blobs").rglob("*"):
        if p.is_file():
            p.write_bytes(p.read_bytes()[:-3] + b"xyz")                # corrupt every blob
    (hermes_home / "memories" / "MEMORY.md").write_text("current")
    (hermes_home / "SOUL.md").write_text("current soul")
    plan = plan_restore(repo, good, hermes_home, Selector())
    with pytest.raises(Exception):
        apply_plan(repo, plan, hermes_home)
    assert (hermes_home / "memories" / "MEMORY.md").read_text() == "current"    # nothing half-restored
    assert (hermes_home / "SOUL.md").read_text() == "current soul"


def test_restore_refuses_symlink_escape(hermes_home, repo, tmp_path):
    good = create_snapshot(repo, hermes_home).snapshot_id
    import shutil
    shutil.rmtree(hermes_home / "skills")
    outside = tmp_path / "outside"
    outside.mkdir()
    (hermes_home / "skills").symlink_to(outside)                        # skills/ now points outside home
    plan = plan_restore(repo, good, hermes_home, Selector.parse("skills"))
    with pytest.raises(RestoreError):
        apply_plan(repo, plan, hermes_home)
    assert not any(outside.iterdir())


def test_nothing_to_do_and_no_match(hermes_home, repo):
    good = create_snapshot(repo, hermes_home).snapshot_id
    assert plan_restore(repo, good, hermes_home, Selector.parse("memory")).empty
    with pytest.raises(RestoreError):
        plan_restore(repo, good, hermes_home, Selector.parse("skill:does-not-exist"))


def test_restore_never_touches_runtime_lock_files(hermes_home, repo):
    good = create_snapshot(repo, hermes_home).snapshot_id
    lock = hermes_home / "memories" / "MEMORY.md.lock"
    lock.write_text("")                                                 # memory tool's lock, held by another process
    (hermes_home / "memories" / "MEMORY.md").write_text("")
    plan = plan_restore(repo, good, hermes_home, Selector.parse("memory"))
    assert "memories/MEMORY.md.lock" not in plan.delete
    apply_plan(repo, plan, hermes_home)
    assert lock.exists()


def test_tampered_manifest_paths_and_modes_are_refused(hermes_home, tmp_path):
    from arkcore.restore import _safe_target
    for bad in ("C:/Windows/x", "..\\..\\x", "a/../../b", "/etc/passwd", "a\x00b"):
        with pytest.raises(RestoreError):
            _safe_target(hermes_home, bad)
    repo = Repository.init(LocalBackend(tmp_path / "r"), None)              # unencrypted: manifest can be edited
    sid = create_snapshot(repo, hermes_home).snapshot_id
    m = repo.load_manifest(sid)
    for f in m["files"]:
        f["m"] = 0o4777                                                     # setuid + world-writable bits
    repo.save_manifest(m)
    (hermes_home / "SOUL.md").write_text("changed")
    apply_plan(repo, plan_restore(repo, sid, hermes_home, Selector.parse("soul")), hermes_home)
    assert (hermes_home / "SOUL.md").stat().st_mode & 0o7000 == 0


def test_decompression_bomb_is_refused(tmp_path):
    import zlib
    from arkcore import repo as repo_mod
    repo = Repository.init(LocalBackend(tmp_path / "r"), None)
    bomb = b"Z" + zlib.compress(b"\0" * (repo_mod.MAX_OBJECT + 10), 9)       # tiny on disk, huge in memory
    (tmp_path / "r" / "blobs" / "ab").mkdir(parents=True)
    (tmp_path / "r" / "blobs" / "ab" / ("ab" + "0" * 62)).write_bytes(bomb)
    with pytest.raises(repo_mod.RepoError):
        repo.get_blob("ab" + "0" * 62)


def test_nested_skill_categories(hermes_home, repo):
    """Hermes stores skills as skills/<category>/<skill>/SKILL.md."""
    import shutil
    for cat, name in (("productivity", "maps"), ("productivity", "airtable"), ("research", "arxiv")):
        d = hermes_home / "skills" / cat / name
        (d / "scripts").mkdir(parents=True)
        (d / "SKILL.md").write_text(f"# {name}")
        (d / "scripts" / "run.py").write_text("print(1)")
    sid = create_snapshot(repo, hermes_home).snapshot_id
    items = {f["i"] for f in repo.load_manifest(sid)["files"] if f["k"] == "skill"}
    assert {"productivity/maps", "productivity/airtable", "research/arxiv", "weather"} <= items
    shutil.rmtree(hermes_home / "skills" / "productivity" / "maps")
    (hermes_home / "skills" / "productivity" / "airtable" / "SKILL.md").write_text("broken")
    plan = plan_restore(repo, sid, hermes_home, Selector.parse("skill:maps"))         # by short name
    assert sorted(e["p"] for e in plan.write) == ["skills/productivity/maps/SKILL.md", "skills/productivity/maps/scripts/run.py"]
    assert not plan.delete
    apply_plan(repo, plan, hermes_home)
    assert (hermes_home / "skills" / "productivity" / "maps" / "scripts" / "run.py").exists()
    assert (hermes_home / "skills" / "productivity" / "airtable" / "SKILL.md").read_text() == "broken"   # untouched
    plan = plan_restore(repo, sid, hermes_home, Selector.parse("skill:productivity/airtable"))
    assert [e["p"] for e in plan.write] == ["skills/productivity/airtable/SKILL.md"]


def test_guard_sees_single_nested_skill_deletions(hermes_home):
    import shutil
    from arkcore.guard import compare, measure
    for name in ("a", "b", "c", "d"):
        d = hermes_home / "skills" / "productivity" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(name)
    before = measure(hermes_home)
    assert {"default/productivity/a", "default/productivity/d"} <= set(before.skills)
    shutil.rmtree(hermes_home / "skills" / "productivity" / "a")
    shutil.rmtree(hermes_home / "skills" / "productivity" / "b")
    alarms = compare(before, measure(hermes_home))
    assert alarms and "2 skill(s) removed" in alarms[0].message and "a, b" in alarms[0].message


def test_skills_restore_leaves_hermes_skill_bookkeeping_alone(hermes_home, repo):
    import shutil
    sid = create_snapshot(repo, hermes_home).snapshot_id
    shutil.rmtree(hermes_home / "skills" / "weather")
    ledger = hermes_home / "skills" / ".curator_ledger.jsonl"
    ledger.write_text('{"deleted": "weather"}\n')                       # written by Hermes after the deletion
    plan = plan_restore(repo, sid, hermes_home, Selector.parse("skills"))
    assert "skills/.curator_ledger.jsonl" not in plan.delete
    apply_plan(repo, plan, hermes_home)
    assert ledger.read_text().startswith('{"deleted"') and (hermes_home / "skills" / "weather" / "SKILL.md").exists()
    # A full restore is a deliberate whole-state rollback and may still include it.
    assert "skills/.curator_ledger.jsonl" in plan_restore(repo, sid, hermes_home, Selector()).delete
