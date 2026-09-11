#!/usr/bin/env python3
"""MCP + CLI claim-path coverage for the workspace_key precondition.

Before this, al_mcp_http's ledger_track/ledger_set_budget never passed
workspace_key down to ensure_agent_secret, so every NEW-agent claim through
MCP raised an unhandled WorkspaceKeyRequiredError (surfacing as a 500 rather
than a typed error). These tests pin the typed-error contract and the
happy path.
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture
def mcp_env(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="agent-ledger-mcp-claim-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import workspace_engine, ledger_engine, identity, al_mcp_http
    importlib.reload(workspace_engine)
    importlib.reload(ledger_engine)
    importlib.reload(identity)
    importlib.reload(al_mcp_http)
    yield al_mcp_http, workspace_engine
    shutil.rmtree(tmp, ignore_errors=True)


# --- ledger_track -------------------------------------------------------------

def test_track_new_agent_without_workspace_key_is_typed_error(mcp_env):
    al_mcp_http, _ = mcp_env
    result = al_mcp_http.ledger_track(
        agent_id="brand-new-agent", rail="api_key", amount_cents=100,
        service="search_query")
    assert result["error_code"] == "workspace_key_required"
    assert "workspace_key" in result["error"]


def test_track_new_agent_with_valid_workspace_key_succeeds(mcp_env):
    al_mcp_http, workspace_engine = mcp_env
    _, raw_key = workspace_engine.create_workspace(owner_email="a@example.com")
    result = al_mcp_http.ledger_track(
        agent_id="claimed-agent", rail="api_key", amount_cents=100,
        service="search_query", workspace_key=raw_key)
    assert "error" not in result
    assert result["agent_secret"]


def test_track_bad_workspace_key_is_typed_error(mcp_env):
    al_mcp_http, _ = mcp_env
    result = al_mcp_http.ledger_track(
        agent_id="another-new-agent", rail="api_key", amount_cents=100,
        service="search_query", workspace_key="wk_live_not-a-real-key")
    assert result["error_code"] == "workspace_key_required"


def test_track_wrong_agent_secret_is_typed_error(mcp_env):
    al_mcp_http, workspace_engine = mcp_env
    _, raw_key = workspace_engine.create_workspace(owner_email="a@example.com")
    al_mcp_http.ledger_track(agent_id="owned-agent", rail="api_key",
                             amount_cents=100, service="s", workspace_key=raw_key)
    result = al_mcp_http.ledger_track(agent_id="owned-agent", rail="api_key",
                                      amount_cents=100, service="s",
                                      agent_secret="wrong")
    assert result["error_code"] == "agent_secret_mismatch"


# --- ledger_set_budget --------------------------------------------------------

def test_budget_new_agent_without_workspace_key_is_typed_error(mcp_env):
    al_mcp_http, _ = mcp_env
    result = al_mcp_http.ledger_set_budget(agent_id="budget-new-agent",
                                           monthly_cents=5000)
    assert result["error_code"] == "workspace_key_required"


def test_budget_new_agent_with_valid_workspace_key_succeeds(mcp_env):
    al_mcp_http, workspace_engine = mcp_env
    _, raw_key = workspace_engine.create_workspace(owner_email="b@example.com")
    result = al_mcp_http.ledger_set_budget(agent_id="budget-claimed-agent",
                                           monthly_cents=5000,
                                           workspace_key=raw_key)
    assert "error" not in result
    assert result["agent_secret"]


# --- CLI argument passing -----------------------------------------------------

def _parse(argv):
    import importlib, cli
    importlib.reload(cli)

    # cli.main() builds its parser inline and wires the handler via
    # set_defaults(fn=cmd_track), which resolves the module global at call
    # time — so patching the module attribute before main() runs captures
    # the parsed Namespace without executing a real write.
    captured = {}

    def _capture(args):
        captured["args"] = args

    orig_argv = sys.argv
    sys.argv = ["agentledger"] + argv
    try:
        import unittest.mock as mock
        with mock.patch.object(cli, "cmd_track", _capture), \
             mock.patch.object(cli, "cmd_budget", _capture):
            cli.main()
    finally:
        sys.argv = orig_argv
    return captured["args"]


def test_cli_track_accepts_workspace_key():
    args = _parse(["track", "--agent-id", "a1", "--rail", "api_key",
                   "--amount-cents", "100", "--service", "s",
                   "--workspace-key", "wk_live_abc"])
    assert args.workspace_key == "wk_live_abc"


def test_cli_budget_accepts_workspace_key():
    args = _parse(["set-budget", "--agent-id", "a1", "--monthly-cents", "5000",
                   "--workspace-key", "wk_live_xyz"])
    assert args.workspace_key == "wk_live_xyz"


# --- MCP reads are credential-gated, same as REST (Fix 7) --------------------

def _claimed(al_mcp_http, workspace_engine, agent_id="read-gate-agent"):
    _, raw_key = workspace_engine.create_workspace(owner_email="r@example.com")
    r = al_mcp_http.ledger_track(agent_id=agent_id, rail="api_key",
                                 amount_cents=100, service="s",
                                 workspace_key=raw_key)
    return raw_key, r["agent_secret"]


def test_mcp_report_without_credential_is_refused(mcp_env):
    al_mcp_http, workspace_engine = mcp_env
    _claimed(al_mcp_http, workspace_engine)
    result = al_mcp_http.ledger_report(agent_id="read-gate-agent")
    assert result["error_code"] == "agent_secret_mismatch"
    assert "total_spend_cents" not in result


def test_mcp_report_accepts_agent_secret(mcp_env):
    al_mcp_http, workspace_engine = mcp_env
    _, secret = _claimed(al_mcp_http, workspace_engine)
    result = al_mcp_http.ledger_report(agent_id="read-gate-agent",
                                       agent_secret=secret)
    assert result["total_spend_cents"] == 100


def test_mcp_report_accepts_workspace_key(mcp_env):
    al_mcp_http, workspace_engine = mcp_env
    raw_key, _ = _claimed(al_mcp_http, workspace_engine)
    result = al_mcp_http.ledger_report(agent_id="read-gate-agent",
                                       workspace_key=raw_key)
    assert result["total_spend_cents"] == 100


def test_mcp_report_rejects_other_workspaces_key(mcp_env):
    """The bypass that mattered: MCP must not hand an agent's data to a
    caller the REST read gate would have refused."""
    al_mcp_http, workspace_engine = mcp_env
    _claimed(al_mcp_http, workspace_engine)
    _, other_key = workspace_engine.create_workspace(owner_email="other@example.com")
    result = al_mcp_http.ledger_report(agent_id="read-gate-agent",
                                       workspace_key=other_key)
    assert result["error_code"] == "agent_secret_mismatch"


def test_mcp_alerts_without_credential_is_refused(mcp_env):
    al_mcp_http, workspace_engine = mcp_env
    _claimed(al_mcp_http, workspace_engine)
    result = al_mcp_http.ledger_alerts(agent_id="read-gate-agent")
    assert result["error_code"] == "agent_secret_mismatch"
    assert "alerts" not in result


def test_mcp_alerts_accepts_agent_secret(mcp_env):
    al_mcp_http, workspace_engine = mcp_env
    _, secret = _claimed(al_mcp_http, workspace_engine)
    result = al_mcp_http.ledger_alerts(agent_id="read-gate-agent",
                                       agent_secret=secret)
    assert "alerts" in result


def test_cli_workspace_key_defaults_to_none():
    args = _parse(["track", "--agent-id", "a1", "--rail", "api_key",
                   "--amount-cents", "100", "--service", "s"])
    assert args.workspace_key is None
