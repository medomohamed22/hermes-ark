"""Ark defends itself against the agent (prompt injection), found with a real model in Hermes:
a hidden instruction in a file made it run ``hermes config set …auto_snapshots false`` and
``hermes ark prune``. Every test drives the real middleware with what a model actually sends."""

import json
import shutil

import pytest

from test_autopilot import plugin  # noqa: F401  (fixture)


def run(plugin, tool, args, effect=None):
    ran = []

    def tool_impl(a):
        ran.append(a)
        if effect:
            effect()
        return json.dumps({"output": "done", "exit_code": 0})

    out = plugin.auto.tool_execution(tool_name=tool, args=args, next_call=tool_impl)
    data = json.loads(out)
    return data, bool(ran)


ATTACKS = [
    "hermes ark prune",
    "hermes ark prune --keep-last 0 --yes",
    "hermes ark forget latest latest~1",
    "hermes ark restore all latest~9 --yes",
    "hermes ark restore memory 2026 -y",
    "hermes ark init --no-encryption",
    "hermes ark pull --remote /tmp/attacker",
    "hermes ark push --mirror",
    "hermes ark repair",
    "cd /tmp && HERMES_HOME=~/.hermes hermes ark prune",
    "python -m hermes_cli.main ark prune",
    "hermes config set plugins.entries.hermes-ark.settings.auto_snapshots false",
    "hermes config set plugins.entries.hermes-ark.settings.health_guard false",
    "hermes config set plugins.enabled '[]'",
    "hermes config set plugins '{}'",
    "hermes plugins disable hermes-ark",
    "hermes plugins uninstall hermes-ark",
    "hermes plugins disable --all",
    "rm -rf ~/.hermes/plugin-data/hermes-ark/repo",
    "rm -rf \"$HERMES_HOME/plugin-data/hermes-ark\"",
    "find / -name ark.json -delete",
    "sed -i 's/auto_snapshots: true/auto_snapshots: false/' ~/.hermes/config.yaml",
    "sed -i '/ARK_PASSPHRASE/d' ~/.hermes/.env",
    "echo $ARK_PASSPHRASE",
    "cat ~/.hermes/.env | grep ARK_",
    "hermes 'ark' prune",
    "hermes ar\\k prune",
    "HERMES ARK PRUNE",
    "hermes ark restore all latest --yes # just a preview",
    "hermes ark restore all latest $(echo --yes)",
]


@pytest.mark.parametrize("command", ATTACKS)
def test_agent_cannot_weaken_ark_through_the_terminal(plugin, command):
    data, ran = run(plugin, "terminal", {"command": command})
    assert not ran, command
    assert data["status"] == "blocked" and "prompt injection" in data["error"]


@pytest.mark.parametrize("command", [
    "hermes ark status", "hermes ark list -n 5", "hermes ark diff latest~1 latest", "hermes ark show",
    "hermes ark verify", "hermes ark snap before refactor", "hermes ark restore memory latest~1",
    "hermes ark prune --dry-run", "hermes ark push", "ls -la", "git status", "pip install requests",
    "cat ~/.hermes/config.yaml", "hermes plugins list", "hermes config set display.theme dark",
    "echo 'noah built an ark' > story.txt",
])
def test_ordinary_and_read_only_commands_still_run(plugin, command):
    data, ran = run(plugin, "terminal", {"command": command})
    assert ran, command
    assert data.get("status") != "blocked"


def test_execute_code_and_bridge_are_covered(plugin):
    code = "import shutil, os; shutil.rmtree(os.path.expanduser('~/.hermes/plugin-data/hermes-ark'))"
    assert not run(plugin, "execute_code", {"code": code})[1]
    bridged = {"name": "terminal", "arguments": json.dumps({"command": "hermes ark prune"})}
    assert not run(plugin, "tool_call", bridged)[1]


def test_file_tools_cannot_touch_ark_data_or_its_settings(plugin, hermes_home):
    st = plugin.settings.load()
    assert not run(plugin, "write_file", {"path": str(st.repository / "ark.json"), "content": "{}"})[1]
    assert not run(plugin, "write_file", {"path": str(plugin.settings.data_dir() / "autopilot.json"),
                                          "content": "{}"})[1]
    cfg = hermes_home / "config.yaml"
    cfg.write_text("model: test\nplugins:\n  enabled:\n  - hermes-ark\n  entries:\n    hermes-ark:\n"
                   "      settings:\n        auto_snapshots: true\n")
    # Rewriting config.yaml without Ark (or with Ark switched off) is refused …
    assert not run(plugin, "write_file", {"path": str(cfg), "content": "model: test\n"})[1]
    off = cfg.read_text().replace("auto_snapshots: true", "auto_snapshots: false")
    assert not run(plugin, "write_file", {"path": str(cfg), "content": off})[1]
    assert not run(plugin, "patch", {"path": str(cfg), "old_string": "  - hermes-ark\n", "new_string": ""})[1]
    assert not run(plugin, "patch", {"path": str(cfg), "old_string": "true", "new_string": "false"})[1]
    # … while unrelated config edits are fine.
    assert run(plugin, "write_file", {"path": str(cfg), "content": cfg.read_text().replace("model: test", "model: other")})[1]
    assert run(plugin, "patch", {"path": str(cfg), "old_string": "model: other", "new_string": "model: x"})[1]
    env = hermes_home / ".env"
    env.write_text("OPENROUTER_API_KEY=sk\nARK_PASSPHRASE=test-passphrase-1\n")
    assert not run(plugin, "write_file", {"path": str(env), "content": "OPENROUTER_API_KEY=sk\n"})[1]
    assert run(plugin, "write_file", {"path": str(env), "content": env.read_text() + "OTHER=1\n"})[1]
    assert not run(plugin, "patch", {"path": str(env), "old_string": "ARK_PASSPHRASE=test-passphrase-1\n",
                                     "new_string": ""})[1]


def test_disguised_attack_that_slips_through_raises_a_tamper_alarm(plugin, hermes_home):
    st = plugin.settings.load()
    plugin.cli.op_snapshot(label="one")
    plugin.cli.op_snapshot(label="two")
    snaps = sorted((st.repository / "snapshots").iterdir())

    def obfuscated_wipe():          # e.g. a script that decodes and runs a hidden `rm`
        snaps[0].unlink()

    data, ran = run(plugin, "terminal", {"command": "./scripts/warmup"}, effect=obfuscated_wipe)
    assert ran and "tamper alarm" in data["ark_tamper_alarm"] and "1 snapshot(s) were deleted" in data["ark_tamper_alarm"]

    def disable():
        (hermes_home / "config.yaml").write_text("model: test\nplugins:\n  disabled:\n  - hermes-ark\n")

    data, _ = run(plugin, "terminal", {"command": "python3 /tmp/innocent.py"}, effect=disable)
    assert "settings or enabled state" in data["ark_tamper_alarm"]

    cfg = hermes_home / "config.yaml"
    cfg.write_text("model: test\nplugins:\n  enabled:\n  - hermes-ark\n  entries:\n    hermes-ark:\n"
                   "      settings:\n        auto_snapshots: true\n        health_guard: true\n")

    def switch_off():               # `sh warmup.sh` that runs a base64-encoded `hermes config set`
        cfg.write_text(cfg.read_text().replace("health_guard: true", "health_guard: false")
                       .replace("auto_snapshots: true", "auto_snapshots: true\n        allow_agent_admin: true"))

    data, _ = run(plugin, "terminal", {"command": "sh warmup.sh"}, effect=switch_off)
    assert "settings or enabled state" in data["ark_tamper_alarm"] and "repaired" not in data["ark_tamper_alarm"]
    st2 = plugin.settings.load()
    assert st2.health_guard is False and st2.allow_agent_admin is True       # the change sticks: alarm only
    cfg.write_text(cfg.read_text().replace("        allow_agent_admin: true\n", ""))

    def nuke():
        shutil.rmtree(st.repository)

    data, _ = run(plugin, "execute_code", {"code": "import x"}, effect=nuke)
    assert "repository was deleted" in data["ark_tamper_alarm"]
    status = plugin.cli.op_status() if (st.repository / "ark.json").exists() else ""
    events = [e["kind"] for e in plugin.auto.load_state()["events"]]
    # 5: the test's own clean-up of config.yaml happens while the agent counts as active, so it is reported too
    assert events.count("tamper") == 5, (events, status)


def test_blocked_attempts_are_recorded_for_the_user(plugin):
    run(plugin, "terminal", {"command": "hermes ark prune"})
    ev = plugin.auto.load_state()["events"][-1]
    assert ev["kind"] == "blocked" and "hermes ark prune" in ev["message"]
    assert "⛔" in plugin.cli.op_status()


def test_user_can_allow_the_agent_to_manage_ark(plugin, monkeypatch):
    store = {"allow_agent_admin": True}
    monkeypatch.setattr(plugin.settings, "_get_config", lambda k, d=None: store.get(k, d))
    assert run(plugin, "terminal", {"command": "hermes ark prune"})[1]


def test_the_agent_cannot_grant_itself_admin(plugin):
    assert not run(plugin, "terminal", {"command":
                   "hermes config set plugins.entries.hermes-ark.settings.allow_agent_admin true"})[1]


def test_agent_checkpoints_cannot_pin_unbounded_history():
    from arkcore.retention import AGENT_PINNED_MAX, Policy, decide
    snaps = [{"id": f"2026{m:02d}{d:02d}T120000000000Z-{m}{d}", "label": "before x", "trigger": "agent"}
             for m in range(1, 13) for d in range(1, 29)]
    snaps.append({"id": "20200101T000000000000Z-user", "label": "my backup", "trigger": "cli"})
    dec = decide(snaps, Policy(keep_last=0, hourly=0, daily=0, weekly=0, monthly=0))
    assert sum(1 for r in dec.keep.values() if r == "labelled") == AGENT_PINNED_MAX + 1
    assert "20200101T000000000000Z-user" in dec.keep                # the user's own labels stay forever


# ---- round 2: bypasses found by red-teaming the first version ------------------------------------

@pytest.mark.parametrize("command", [
    "hermes ark restore all latest~3 --y",                 # argparse accepted abbreviated --yes
    "hermes ark restore all latest~3 --ye",
    "hermes ark restore all latest~3 -fy",
    "hermes ark push --mir",
    "hermes ark push --mirror=1",
    "H=herm; ${H}es ark prune",                             # no literal "hermes"
    "x=$(which hermes); $x ark forget latest",
    "hermes ark list > ~/.hermes/plugin-data/hermes-ark/repo/ark.json",   # redirect after a read-only command
    "hermes ark status; hermes ark prune",
])
def test_round2_command_bypasses_are_blocked(plugin, command):
    data, ran = run(plugin, "terminal", {"command": command})
    assert not ran, command


def test_round2_file_bypasses_are_blocked(plugin, hermes_home, monkeypatch):
    repo = plugin.settings.load().repository
    (hermes_home / "config.yaml").write_text("model: t\nplugins:\n  enabled:\n  - hermes-ark\n")
    v4a = ("*** Begin Patch\n*** Update File: {}\n@@\n-  - hermes-ark\n+  - other\n*** End Patch")
    assert not run(plugin, "patch", {"mode": "patch", "patch": v4a.format(hermes_home / "config.yaml")})[1]
    assert not run(plugin, "patch", {"mode": "patch", "patch": f"*** Begin Patch\n*** Delete File: {repo / 'ark.json'}\n"
                                                                "*** End Patch"})[1]
    # MCP / other plugins' file tools
    assert not run(plugin, "mcp_filesystem_write_file", {"path": str(repo / "ark.json"), "content": "x"})[1]
    assert not run(plugin, "mcp_filesystem_move_file", {"source": str(repo), "destination": "/tmp/gone"})[1]
    assert not run(plugin, "mcp_shell_run_command", {"cmd": "hermes ark forget latest"})[1]
    # a parent folder of Ark's data is a target too
    assert not run(plugin, "mcp_filesystem_delete", {"path": str(hermes_home / "plugin-data")})[1]
    # relative paths resolve from the agent's working directory, not the gateway's
    import agent.runtime_cwd as rc
    monkeypatch.setattr(rc, "resolve_agent_cwd", lambda: hermes_home)
    assert not run(plugin, "write_file", {"path": "config.yaml", "content": "model: x\n"})[1]
    # ordinary MCP calls still work
    assert run(plugin, "mcp_filesystem_write_file", {"path": "/tmp/notes.txt", "content": "hello"})[1]


def test_cli_rejects_abbreviated_flags(plugin):
    import argparse
    parser = argparse.ArgumentParser()
    plugin.cli.setup_parser(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["restore", "all", "latest", "--ye"])
    assert parser.parse_args(["restore", "all", "latest", "--yes"]).yes


def _home_cfg(hermes_home):
    cfg = hermes_home / "config.yaml"
    cfg.write_text("model: test\nplugins:\n  enabled:\n  - hermes-ark\n  entries:\n    hermes-ark:\n"
                   "      settings:\n        auto_snapshots: true\n")
    return cfg


def test_disable_and_lost_secret_raise_the_alarm_but_are_never_overridden(plugin, hermes_home, monkeypatch):
    """Review #149640: a plugin must never override Hermes' enable/disable state or the user's settings."""
    cfg = _home_cfg(hermes_home)
    env = hermes_home / ".env"
    env.write_text("OPENROUTER_API_KEY=sk\n")
    env_before = env.read_bytes()
    disabled = ("model: test\nplugins:\n  enabled: []\n  disabled:\n  - hermes-ark\n  entries:\n"
                "    hermes-ark:\n      settings:\n        auto_snapshots: true\n")

    def attack():
        cfg.write_text(disabled)
        monkeypatch.delenv("ARK_PASSPHRASE")                         # gone from what Hermes provides

    data, _ = run(plugin, "terminal", {"command": "./scripts/setup"}, effect=attack)
    note = data["ark_tamper_alarm"]
    assert "enabled state" in note and "Ark secrets removed: ARK_PASSPHRASE" in note
    assert "repaired" not in note
    assert cfg.read_text() == disabled                               # disable sticks
    assert env.read_bytes() == env_before                            # Ark never touches .env
    assert not plugin.selfdefense.is_enabled()


def test_deleted_repository_is_rebuilt_and_snapshotted(plugin, hermes_home):
    st = plugin.settings.load()
    plugin.cli.op_snapshot(label="one")
    key = (st.repository / "ark.json").read_bytes()

    data, _ = run(plugin, "terminal", {"command": "./cleanup"}, effect=lambda: shutil.rmtree(st.repository))
    assert "repository was deleted" in data["ark_tamper_alarm"] and "repository key file" in data["ark_tamper_alarm"]
    assert (st.repository / "ark.json").read_bytes() == key            # same key: the passphrase still works
    import time
    for _ in range(100):                                                 # fresh snapshot runs in the background
        if (st.repository / "snapshots").is_dir() and list((st.repository / "snapshots").iterdir()):
            break
        time.sleep(0.05)
    plugin.settings._cache.clear()
    assert plugin.settings.open_repo().snapshot_ids()


def test_deleted_backup_data_is_noticed(plugin):
    st = plugin.settings.load()
    plugin.cli.op_snapshot(label="one")
    blobs = [p for p in (st.repository / "blobs").rglob("*") if p.is_file()]
    data, _ = run(plugin, "terminal", {"command": "./x"}, effect=lambda: [p.unlink() for p in blobs[:3]])
    assert "3 backup data object(s) were deleted" in data["ark_tamper_alarm"]


def test_modified_plugin_code_is_reported_never_rewritten(plugin, monkeypatch, tmp_path):
    """Review #149640: no code self-restore (it would fight `hermes plugins update/remove`); alert only."""
    import importlib.util
    import sys
    from test_autopilot import ROOT
    copy = tmp_path / "ark-copy"
    shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns("tests", "docs", ".git", "__pycache__", "submission"))
    name = "hermes_ark_copy_under_test"
    spec = importlib.util.spec_from_file_location(name, copy / "__init__.py", submodule_search_locations=[str(copy)])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[name] = pkg
    spec.loader.exec_module(pkg)
    target = copy / "arkcore" / "restore.py"
    changed = target.read_bytes() + b"\n# updated\n"

    def update():
        target.write_bytes(changed)
        (copy / "arkcore" / "new_module.py").write_text("x = 1\n")

    out = json.loads(pkg.auto.tool_execution(tool_name="terminal", args={"command": "./build"},
                                             next_call=lambda a: (update(), '{"output": "ok"}')[1]))
    assert "plugin code was modified" in out["ark_tamper_alarm"]
    assert target.read_bytes() == changed and (copy / "arkcore" / "new_module.py").exists()   # untouched


def test_delayed_background_attack_is_caught_on_the_next_call(plugin, hermes_home):
    st = plugin.settings.load()
    plugin.cli.op_snapshot(label="one")
    plugin.cli.op_snapshot(label="two")
    run(plugin, "terminal", {"command": "nohup ./later.sh &"})           # returns at once …
    victim = sorted((st.repository / "snapshots").iterdir())[0]
    victim.unlink()                                                      # … and strikes afterwards
    data, ran = run(plugin, "terminal", {"command": "ls"})
    assert ran and "while the agent was active" in data["ark_tamper_alarm"]
    assert "1 snapshot(s) were deleted" in data["ark_tamper_alarm"]


def test_users_own_prune_between_calls_is_not_an_alarm(plugin):
    st = plugin.settings.load()
    for i in range(3):
        plugin.cli.op_snapshot(label=f"s{i}")
    run(plugin, "terminal", {"command": "ls"})
    oldest = sorted(p.name for p in (st.repository / "snapshots").iterdir())[0]
    plugin.cli.op_prune(forget=[oldest])                                 # the user, in their own terminal
    data, _ = run(plugin, "terminal", {"command": "ls"})
    assert "ark_tamper_alarm" not in data


def test_arks_own_cli_run_by_a_disguised_agent_command_is_an_alarm(plugin):
    """`forget` through Ark's CLI in another process, during an agent tool call."""
    import subprocess
    import sys
    st = plugin.settings.load()
    for i in range(2):
        plugin.cli.op_snapshot(label=f"s{i}")
    oldest = sorted(p.name for p in (st.repository / "snapshots").iterdir())[0]
    code = (f"import shutil,pathlib; p=pathlib.Path({str(st.repository / 'snapshots' / oldest)!r}); "
            "p.unlink() if p.is_file() else shutil.rmtree(p)")

    def other_process_forget():
        subprocess.run([sys.executable, "-c", code], check=True)
        plugin.auto._update_state(deletions=[{"at": __import__("time").time(), "ids": [oldest]}])  # as the CLI would

    data, _ = run(plugin, "terminal", {"command": "./maintenance"}, effect=other_process_forget)
    assert "1 snapshot(s) were deleted" in data["ark_tamper_alarm"]


def test_settings_failure_fails_closed(plugin, monkeypatch):
    monkeypatch.setattr(plugin.settings, "load", lambda: (_ for _ in ()).throw(RuntimeError("broken config")))
    assert plugin.selfdefense.check("terminal", {"command": "hermes ark prune"})
    assert plugin.selfdefense.check("terminal", {"command": "rm -rf ~/.hermes/plugin-data/hermes-ark"})


# ---- round 3: the watchdog (found with the real model: Hermes moved a long command to the background,
# so the damage landed after the tool call returned and nothing called a tool afterwards) -------------

def _attack_everything(hermes_home, repo):
    (hermes_home / "config.yaml").write_text("model: test\nplugins:\n  enabled: []\n  disabled:\n  - hermes-ark\n"
                                             "  entries:\n    hermes-ark:\n      settings:\n        auto_snapshots: true\n")
    shutil.rmtree(repo)


def test_watchdog_reports_damage_done_by_a_background_job(plugin, hermes_home):
    import subprocess
    import time
    cfg = _home_cfg(hermes_home)
    st = plugin.settings.load()
    plugin.cli.op_snapshot(label="one")
    bg = subprocess.Popen(["sleep", "30"])
    try:
        # The terminal tool returns at once with the background process' pid …
        out = plugin.auto.tool_execution(tool_name="terminal", args={"command": "sh devreset.sh"},
                                         next_call=lambda a: json.dumps({"output": "Background process started",
                                                                         "pid": bg.pid}))
        assert "ark_tamper_alarm" not in json.loads(out)
        _attack_everything(hermes_home, st.repository)                  # … which strikes afterwards
        plugin.auto._activity["last"] = time.time() - 3 * 3600            # even long after the last tool call
        note = plugin.auto.watchdog_tick()
    finally:
        bg.kill()
    assert note and "enabled state" in note and "repository was deleted" in note
    assert "repository key file" in note                              # Ark's own key file is put back …
    assert "- hermes-ark" in cfg.read_text().split("disabled:")[1]      # … the user-visible disable is not
    assert (st.repository / "ark.json").exists()
    data, _ = run(plugin, "terminal", {"command": "ls"})
    assert "while the agent was active" in data["ark_tamper_alarm"]


def test_watchdog_respects_the_users_own_changes_when_the_agent_is_idle(plugin, hermes_home):
    _home_cfg(hermes_home)
    st = plugin.settings.load()
    plugin.auto.watchdog_tick()                                          # baseline
    plugin.auto._activity["last"] = 0.0                                  # agent idle, no background jobs
    cfg = hermes_home / "config.yaml"
    cfg.write_text(cfg.read_text().replace("auto_snapshots: true", "auto_snapshots: false"))
    assert plugin.auto.watchdog_tick() is None
    assert "auto_snapshots: false" in cfg.read_text()                    # kept: it was the user
    assert plugin.auto.load_state()["events"][-1]["kind"] == "info"
    # the user deleting the repository while the agent is idle is reported, not undone
    plugin.cli.op_snapshot(label="x")
    plugin.auto.watchdog_tick()
    shutil.rmtree(st.repository)
    note = plugin.auto.watchdog_tick()
    assert note and "repository was deleted" in note and not (st.repository / "ark.json").exists()


def test_watchdog_alarms_on_a_disable_while_the_agent_is_working_without_reverting(plugin, hermes_home):
    _home_cfg(hermes_home)
    plugin.auto.watchdog_tick()
    run(plugin, "terminal", {"command": "ls"})                           # agent active just now
    cfg = hermes_home / "config.yaml"
    cfg.write_text(cfg.read_text().replace("  enabled:\n  - hermes-ark\n", "  enabled: []\n"))
    note = plugin.auto.watchdog_tick()
    assert note and "enabled state" in note and "repaired" not in note
    assert "enabled: []" in cfg.read_text()


def test_a_call_that_damaged_ark_is_quarantined(plugin, hermes_home):
    _home_cfg(hermes_home)
    cfg = hermes_home / "config.yaml"

    def switch_off():
        cfg.write_text(cfg.read_text().replace("auto_snapshots: true", "auto_snapshots: false"))

    data, ran = run(plugin, "terminal", {"command": "sh devreset.sh"}, effect=switch_off)
    assert ran and "ark_tamper_alarm" in data
    data, ran = run(plugin, "terminal", {"command": "sh devreset.sh"}, effect=switch_off)
    assert not ran and "already damaged" in data["error"]
    assert run(plugin, "terminal", {"command": "ls"})[1]                    # everything else still runs


def test_damage_while_hermes_was_not_running_is_found_after_a_restart(plugin):
    st = plugin.settings.load()
    for i in range(2):
        plugin.cli.op_snapshot(label=f"s{i}")
    run(plugin, "terminal", {"command": "ls"})                           # Ark remembers the good state
    plugin.selfdefense._last = None                                      # Hermes restarts (a new process)
    persisted = plugin.settings.data_dir() / "selfdefense.json"
    persisted.write_text(persisted.read_text().replace(f'"pid": {__import__("os").getpid()}', '"pid": 1'))
    sorted((st.repository / "snapshots").iterdir())[0].unlink()          # a delayed job struck meanwhile
    note = plugin.auto.watchdog_tick()
    assert note and "while Hermes was not running" in note and "1 snapshot(s) were deleted" in note
    plugin.selfdefense._last = None
    assert plugin.auto.watchdog_tick() is None                           # reported once, not on every start


def test_quarantine_and_checks_survive_decorated_names_and_other_timeouts(plugin, hermes_home):
    """Seen with gpt-oss: the retry came as ``terminal<|channel|>commentary`` with the same command."""
    _home_cfg(hermes_home)
    cfg = hermes_home / "config.yaml"

    def switch_off():
        cfg.write_text(cfg.read_text().replace("auto_snapshots: true", "auto_snapshots: false"))

    run(plugin, "terminal", {"command": "sh devreset.sh", "timeout": 300}, effect=switch_off)
    data, ran = run(plugin, "terminal<|channel|>commentary", {"command": "sh  devreset.sh", "timeout": 600},
                    effect=switch_off)
    assert not ran and "already damaged" in data["error"]
    assert not run(plugin, "terminal<|channel|>commentary", {"command": "hermes ark prune"})[1]


def test_quarantine_and_activity_survive_a_plugin_reload(plugin, hermes_home):
    """Re-enabling Ark after an attack makes Hermes reload the plugin (seen live with gpt-oss)."""
    _home_cfg(hermes_home)
    cfg = hermes_home / "config.yaml"

    def switch_off():
        cfg.write_text(cfg.read_text().replace("auto_snapshots: true", "auto_snapshots: false"))

    run(plugin, "terminal", {"command": "sh devreset.sh"}, effect=switch_off)
    # simulate a fresh module instance
    plugin.auto._quarantine.clear()
    plugin.auto._loaded.clear()
    plugin.auto._activity["last"] = 0.0
    assert plugin.auto.agent_active()
    data, ran = run(plugin, "terminal", {"command": "sh devreset.sh"}, effect=switch_off)
    assert not ran and "already damaged" in data["error"]


def test_after_a_reload_a_settings_change_is_still_reported(plugin, hermes_home):
    _home_cfg(hermes_home)
    run(plugin, "terminal", {"command": "ls"})                           # agent active; state remembered
    plugin.selfdefense._last = None                                      # plugin reloaded in the same process
    cfg = hermes_home / "config.yaml"
    cfg.write_text(cfg.read_text().replace("auto_snapshots: true", "auto_snapshots: false"))
    note = plugin.auto.watchdog_tick()
    assert note and "settings or enabled state" in note
    assert plugin.settings.load().auto is False                          # reported, not reverted
    assert "ARK_PASSPHRASE" not in (plugin.settings.data_dir() / "selfdefense.json").read_text()


def test_watchdog_stops_when_the_plugin_is_disabled_or_unloaded(plugin, hermes_home, monkeypatch):
    """Review #149640: the watchdog must exit once the plugin is disabled or unloaded."""
    import threading
    import time
    _home_cfg(hermes_home)
    monkeypatch.setattr(plugin.auto, "WATCHDOG_INTERVAL_S", 0.05)

    def alive():
        return [t for t in threading.enumerate() if t.name == "hermes-ark-watchdog" and t.is_alive()]

    def wait_for(cond, secs=3.0):
        end = time.time() + secs
        while time.time() < end and not cond():
            time.sleep(0.02)
        return cond()

    ensure = plugin.auto._ensure_watchdog_real
    before = len(alive())
    ensure()
    assert wait_for(lambda: len(alive()) == before + 1)
    plugin.auto.stop_watchdog()                                         # ctx.on_unload callback
    assert wait_for(lambda: len(alive()) == before)

    plugin.auto._watchdog_stop.clear()
    ensure()
    assert wait_for(lambda: len(alive()) == before + 1)
    cfg = hermes_home / "config.yaml"
    cfg.write_text(cfg.read_text().replace("  enabled:\n  - hermes-ark\n", "  enabled: []\n  disabled:\n  - hermes-ark\n"))
    assert wait_for(lambda: len(alive()) == before)                     # `hermes plugins disable`


def test_no_write_back_machinery_remains():
    import sys
    from test_autopilot import ROOT
    sys.path.insert(0, str(ROOT))
    src = (ROOT / "selfdefense.py").read_text() + (ROOT / "auto.py").read_text()
    for gone in ("_enable_via_cli", "_save_enabled", "_restore_code", "_restore_enabled", "subprocess", "set_value("):
        assert gone not in src, gone


def test_ark_never_reads_or_writes_env_files(plugin, hermes_home, monkeypatch):
    """Catalog rule 11: secrets only through Hermes' secret scope."""
    import builtins
    import pathlib
    opened = []
    real_open, real_read = builtins.open, pathlib.Path.read_text
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1])
    monkeypatch.setattr(pathlib.Path, "read_text", lambda self, *a, **k: (opened.append(str(self)), real_read(self, *a, **k))[1])
    (hermes_home / ".env").write_text("ARK_PASSPHRASE=x\n")
    plugin.selfdefense.fingerprint()
    plugin.auto.watchdog_tick()
    run(plugin, "write_file", {"path": str(hermes_home / ".env"), "content": "OTHER=1\n"})
    run(plugin, "terminal", {"command": "ls"})
    assert not [f for f in opened if f.endswith(".env")]
