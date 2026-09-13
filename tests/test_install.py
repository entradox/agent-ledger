# tests/test_install.py
"""BUILD-6 — the installer.

A Claude Code settings file is a personal thing that a dozen other tools
already write to. An installer that rewrites it is an installer that breaks
someone's setup, so the property under test is not "does it add the hook" but
"does everything that was already there survive".
"""
import importlib.util
import json
from pathlib import Path

import pytest

CC_DIR = Path(__file__).resolve().parent.parent / "integrations" / "claude-code"
INSTALLER = CC_DIR / "install.py"
REPORTER = CC_DIR / "report-session.py"
spec = importlib.util.spec_from_file_location("cc_install", INSTALLER)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture
def settings_file(tmp_path):
    """Shaped like a real one: several other tools' hooks and MCP servers."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "env": {"SOME": "value"},
        "model": "claude-sonnet-5",
        "mcpServers": {"porkbun": {"command": "npx", "args": ["-y", "@porkbunllc/mcp-server"]},
                       "regulatory-intel": {"type": "http", "url": "https://x/mcp"}},
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "/bin/bash someone-elses.sh"}]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "dcg"}]}],
        },
        "permissions": {"allow": ["Bash(ls:*)"]},
    }, indent=2))
    return path


def test_nothing_that_was_already_there_is_lost(settings_file):
    before = json.loads(settings_file.read_text())
    installer.main(["--settings", str(settings_file), "--reporter", str(REPORTER)])
    after = json.loads(settings_file.read_text())

    # Assert it actually installed. Without this the test passes vacuously if
    # the installer refuses to run at all — which is exactly what it did the
    # first time this test was written, with the wrong path.
    assert "agent-ledger" in after["mcpServers"], "the installer did not install anything"
    assert any("report-session.py" in h["command"]
               for e in after["hooks"]["Stop"] for h in e["hooks"])

    assert after["env"] == before["env"]
    assert after["model"] == before["model"]
    assert after["permissions"] == before["permissions"]
    assert after["mcpServers"]["porkbun"] == before["mcpServers"]["porkbun"]
    assert after["mcpServers"]["regulatory-intel"] == before["mcpServers"]["regulatory-intel"]
    other = [h["command"] for e in after["hooks"]["Stop"] for h in e["hooks"]]
    assert "/bin/bash someone-elses.sh" in other, "an unrelated Stop hook was removed"
    assert after["hooks"]["PreToolUse"] == before["hooks"]["PreToolUse"]


def test_it_adds_the_mcp_server_in_the_http_shape(settings_file):
    installer.main(["--settings", str(settings_file), "--reporter", str(INSTALLER),
                    "--api-base", "https://example.test"])
    entry = json.loads(settings_file.read_text())["mcpServers"]["agent-ledger"]
    assert entry == {"type": "http", "url": "https://example.test/mcp/"}


def test_it_adds_a_stop_hook_that_can_find_the_reporter(settings_file):
    installer.main(["--settings", str(settings_file), "--reporter", str(REPORTER),
                    "--agent-id", "my-cc"])
    settings = json.loads(settings_file.read_text())
    commands = [h["command"] for e in settings["hooks"]["Stop"] for h in e["hooks"]]
    ours = [c for c in commands if "report-session.py" in c]
    assert len(ours) == 1
    assert "AGENT_LEDGER_AGENT_ID=my-cc" in ours[0]


def test_running_it_twice_does_not_duplicate_anything(settings_file):
    installer.main(["--settings", str(settings_file), "--reporter", str(REPORTER)])
    once = json.loads(settings_file.read_text())
    installer.main(["--settings", str(settings_file), "--reporter", str(REPORTER)])
    twice = json.loads(settings_file.read_text())
    assert once == twice

    hooks = [h for e in twice["hooks"]["Stop"] for h in e["hooks"]
             if "report-session.py" in h["command"]]
    assert len(hooks) == 1


def test_a_stale_reporter_path_is_refreshed_rather_than_added_again(settings_file, tmp_path):
    """Someone moves the checkouts around. The hook should follow, not stack up
    a second copy pointing at a path that no longer exists."""
    old_reporter = tmp_path / "old" / "report-session.py"
    old_reporter.parent.mkdir(parents=True)
    old_reporter.write_text("# old copy\n")
    installer.main(["--settings", str(settings_file), "--reporter", str(old_reporter)])
    installer.main(["--settings", str(settings_file), "--reporter", str(REPORTER)])

    commands = [h["command"] for e in json.loads(settings_file.read_text())["hooks"]["Stop"]
                for h in e["hooks"]]
    assert not any("old/report-session.py" in c for c in commands), "the stale hook is still there"
    assert any(str(REPORTER) in c for c in commands)
    assert len([c for c in commands if "report-session.py" in c]) == 1, "the hook was duplicated"


def test_an_env_file_is_sourced_and_the_secret_stays_out_of_settings(settings_file, tmp_path):
    """The credential must not land in settings.json, which is shared, often
    synced, and read by every tool on the machine."""
    env_file = tmp_path / "creds.env"
    env_file.write_text("AGENT_LEDGER_AGENT_SECRET=as_do_not_put_this_in_settings\n")
    installer.main(["--settings", str(settings_file), "--reporter", str(REPORTER),
                    "--env-file", str(env_file), "--agent-id", "cc"])
    text = settings_file.read_text()
    assert str(env_file) in text, "the hook does not source the credential file"
    assert "as_do_not_put_this_in_settings" not in text, "the secret was written into settings.json"

    settings = json.loads(text)
    command = [h["command"] for e in settings["hooks"]["Stop"] for h in e["hooks"]
               if "report-session.py" in h["command"]][0]
    assert command.index(". ") < command.index("report-session.py"), (
        "the file must be sourced BEFORE the reporter runs, or the secret arrives too late")
    assert "set -a" in command, "without set -a the sourced vars are not exported"


def test_a_differently_named_secret_variable_is_remapped(settings_file, tmp_path):
    """The reporter reads AGENT_LEDGER_AGENT_SECRET. A creds file that calls it
    something else must be mapped, not silently ignored — otherwise the hook
    runs, finds no credential, and fails on every Stop without anyone seeing it.
    Found by executing the installed hook instead of trusting the install."""
    env_file = tmp_path / "creds.env"
    env_file.write_text("AGENT_LEDGER_DOGFOOD_SECRET=as_whatever\n")
    installer.main(["--settings", str(settings_file), "--reporter", str(REPORTER),
                    "--env-file", str(env_file),
                    "--secret-env", "AGENT_LEDGER_DOGFOOD_SECRET"])
    settings = json.loads(settings_file.read_text())
    command = [h["command"] for e in settings["hooks"]["Stop"] for h in e["hooks"]
               if "report-session.py" in h["command"]][0]
    assert 'AGENT_LEDGER_AGENT_SECRET="$AGENT_LEDGER_DOGFOOD_SECRET"' in command
    assert command.index("set +a") < command.index("AGENT_LEDGER_AGENT_SECRET"), (
        "the variable must be mapped AFTER the file is sourced")


def test_the_original_is_backed_up(settings_file):
    installer.main(["--settings", str(settings_file), "--reporter", str(REPORTER)])
    backup = settings_file.with_suffix(".json.bak")
    assert backup.exists()
    assert "agent-ledger" not in backup.read_text()


def test_uninstall_removes_only_our_entries(settings_file):
    installer.main(["--settings", str(settings_file), "--reporter", str(REPORTER)])
    installer.main(["--settings", str(settings_file), "--uninstall"])
    settings = json.loads(settings_file.read_text())
    assert "agent-ledger" not in settings.get("mcpServers", {})
    commands = [h["command"] for e in settings["hooks"]["Stop"] for h in e["hooks"]]
    assert not any("report-session.py" in c for c in commands)
    assert "/bin/bash someone-elses.sh" in commands
    assert settings["hooks"]["PreToolUse"], "PreToolUse hooks were collateral damage"


def test_dry_run_changes_nothing_on_disk(settings_file):
    before = settings_file.read_text()
    installer.main(["--settings", str(settings_file), "--dry-run"])
    assert settings_file.read_text() == before
    assert not settings_file.with_suffix(".json.bak").exists()


def test_a_settings_file_with_no_hooks_key_gets_one(tmp_path):
    path = tmp_path / "fresh.json"
    path.write_text("{}")
    installer.main(["--settings", str(path), "--reporter", str(INSTALLER)])
    settings = json.loads(path.read_text())
    assert settings["hooks"]["Stop"][0]["hooks"][0]["type"] == "command"


def test_the_real_settings_file_would_survive_a_merge():
    """The strongest version of the safety property, run against this machine's
    ACTUAL settings: merge into a copy and assert only our keys moved."""
    real = Path.home() / ".claude" / "settings.json"
    if not real.exists():
        pytest.skip("no Claude Code settings on this machine")
    import tempfile
    original = json.loads(real.read_text())
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "settings.json"
        copy.write_text(real.read_text())
        installer.main(["--settings", str(copy), "--reporter", str(REPORTER)])
        merged = json.loads(copy.read_text())

    changed_top = {k for k in set(original) | set(merged)
                   if original.get(k) != merged.get(k)}
    assert changed_top <= {"hooks", "mcpServers"}, (
        f"the installer touched unrelated top-level keys: {changed_top}")

    # every server the user already had is still there, byte for byte.
    # Ours is the exception and is EXPECTED to change: if the installer is
    # pointed at a different base, the existing entry is refreshed rather than
    # added twice — which is the behaviour a re-run on a moved install needs.
    for name, entry in (original.get("mcpServers") or {}).items():
        if name == "agent-ledger":
            continue
        assert merged["mcpServers"][name] == entry, f"mcpServers[{name}] was altered"
    assert "agent-ledger" in merged["mcpServers"], "our own entry disappeared"

    # every hook event they already had is still there
    for event, entries in (original.get("hooks") or {}).items():
        assert event in merged["hooks"], f"hooks.{event} disappeared"
        if event != "Stop":
            assert merged["hooks"][event] == entries, f"hooks.{event} was altered"
