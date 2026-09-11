#!/usr/bin/env python3
"""Coverage for scripts/migrate_agents_to_workspace.py (I5).

The script used to call create_workspace(owner_email=...) with no
google_sub, minting a workspace nobody could log into — the operator's real
Google login resolved to a different one, so the migrated agents never
showed on their dashboard.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "migrate_agents_to_workspace.py"


@pytest.fixture
def data_dir(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="agent-ledger-migrate-"))
    monkeypatch.setenv("AGENT_LEDGER_DATA", str(tmp))
    agents = tmp / "agents"
    for name in ("legacy-agent-a", "legacy-agent-b"):
        (agents / name).mkdir(parents=True)
        (agents / name / "secret.txt").write_text("pre-existing-secret")
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


def _run(data_dir, *args):
    import os
    env = dict(os.environ, AGENT_LEDGER_DATA=str(data_dir))
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, env=env, cwd=str(REPO))


def test_migration_binds_workspace_to_google_sub(data_dir):
    r = _run(data_dir, "--owner-email", "op@example.com", "--google-sub", "g-operator")
    assert r.returncode == 0, r.stderr

    import importlib, workspace_engine
    importlib.reload(workspace_engine)
    ws = workspace_engine.get_workspace_by_google_sub("g-operator")
    assert ws is not None, "migrated workspace must be reachable by the operator's login"

    for name in ("legacy-agent-a", "legacy-agent-b"):
        assigned = (data_dir / "agents" / name / "workspace_id.txt").read_text().strip()
        assert assigned == ws["workspace_id"]
    # existing secrets untouched
    assert (data_dir / "agents" / "legacy-agent-a" / "secret.txt").read_text() \
        == "pre-existing-secret"


def test_migration_without_google_sub_warns_loudly(data_dir):
    r = _run(data_dir, "--owner-email", "op@example.com", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "WARNING" in r.stdout
    assert "--google-sub" in r.stdout


def test_dry_run_changes_nothing(data_dir):
    _run(data_dir, "--owner-email", "op@example.com", "--google-sub", "g-op", "--dry-run")
    assert not (data_dir / "agents" / "legacy-agent-a" / "workspace_id.txt").exists()
    assert not (data_dir / "workspaces").exists()


def test_rerun_is_idempotent_and_reissues_nothing(data_dir):
    """A second run must reuse the same workspace and must NOT mint a new
    key — the operator's existing key has to keep working."""
    first = _run(data_dir, "--owner-email", "op@example.com", "--google-sub", "g-op")
    assert "key shown once" in first.stdout

    import importlib, workspace_engine
    importlib.reload(workspace_engine)
    raw_key = first.stdout.split("key shown once): ")[1].strip().splitlines()[0]
    ws_id = workspace_engine.get_workspace_by_key(raw_key)["workspace_id"]

    before = len(list((data_dir / "workspaces").glob("*.json")))
    second = _run(data_dir, "--owner-email", "op@example.com", "--google-sub", "g-op")
    assert second.returncode == 0, second.stderr
    # Everything migrated on run 1, so run 2 has nothing to do and must not
    # call create_workspace at all — inside the launch window that would burn
    # a scarcity slot for zero agents.
    assert "Nothing to migrate" in second.stdout
    assert len(list((data_dir / "workspaces").glob("*.json"))) == before
    # the original key still resolves to the same workspace
    assert workspace_engine.get_workspace_by_key(raw_key)["workspace_id"] == ws_id


def test_no_agents_to_migrate_mints_nothing(data_dir):
    """Running against an instance with nothing to migrate must not create a
    workspace (and therefore must not consume a scarcity slot)."""
    import shutil
    shutil.rmtree(data_dir / "agents", ignore_errors=True)
    (data_dir / "agents").mkdir(parents=True, exist_ok=True)
    r = _run(data_dir, "--owner-email", "op@example.com", "--google-sub", "g-none")
    assert r.returncode == 0, r.stderr
    assert "Nothing to migrate" in r.stdout
    assert not (data_dir / "workspaces").exists() or \
        list((data_dir / "workspaces").glob("*.json")) == []
