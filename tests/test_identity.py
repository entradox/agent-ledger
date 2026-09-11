# tests/test_identity.py
import os, shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest


@pytest.fixture
def mod(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import workspace_engine, ledger_engine, identity
    importlib.reload(workspace_engine)
    importlib.reload(ledger_engine)
    importlib.reload(identity)
    yield identity, workspace_engine, ledger_engine
    shutil.rmtree(tmp, ignore_errors=True)


def test_resolve_workspace_key_valid(mod):
    identity, ws, _ = mod
    _, raw_key = ws.create_workspace(owner_email="a@example.com")
    assert identity.resolve_workspace_key(raw_key) is not None


def test_resolve_workspace_key_invalid(mod):
    identity, ws, _ = mod
    assert identity.resolve_workspace_key("bad-key") is None
    assert identity.resolve_workspace_key(None) is None


def test_resolve_agent_secret_true_and_false(mod):
    identity, ws, ledger_engine = mod
    secret, _ = ledger_engine.ensure_agent_secret("agent-x", workspace_key=ws.create_workspace(owner_email="a@example.com")[1])
    assert identity.resolve_agent_secret("agent-x", secret) is True
    assert identity.resolve_agent_secret("agent-x", "wrong") is False
    assert identity.resolve_agent_secret("no-such-agent", "anything") is False


def test_authorize_agent_access_either_credential(mod):
    identity, ws, ledger_engine = mod
    _, raw_key = ws.create_workspace(owner_email="a@example.com")
    secret, _ = ledger_engine.ensure_agent_secret("agent-y", workspace_key=raw_key)
    assert identity.authorize_agent_access("agent-y", agent_secret=secret) is True
    assert identity.authorize_agent_access("agent-y", workspace_key=raw_key) is True
    assert identity.authorize_agent_access("agent-y", agent_secret="wrong", workspace_key="wrong") is False
