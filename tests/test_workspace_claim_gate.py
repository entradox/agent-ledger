# tests/test_workspace_claim_gate.py
import os, shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

AL_VERSION = "2026-09-01"


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, workspace_engine, api_server, routes_agents
    importlib.reload(workspace_engine)
    importlib.reload(ledger_engine)
    importlib.reload(routes_agents)
    importlib.reload(api_server)
    from fastapi.testclient import TestClient
    yield TestClient(api_server.app), workspace_engine
    shutil.rmtree(tmp, ignore_errors=True)


def _headers():
    return {"AL-API-Version": AL_VERSION, "Content-Type": "application/json"}


def test_new_claim_without_workspace_key_is_rejected(client):
    c, ws = client
    r = c.post("/v1/track", headers=_headers(),
               json={"agent_id": "no-key-agent", "rail": "api_key",
                     "amount_cents": 100, "service": "s"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "workspace_key_required"


def test_new_claim_with_valid_workspace_key_succeeds(client):
    c, ws = client
    _, raw_key = ws.create_workspace(owner_email="a@example.com")
    r = c.post("/v1/track", headers=_headers(),
               json={"agent_id": "keyed-agent", "rail": "api_key",
                     "amount_cents": 100, "service": "s", "workspace_key": raw_key})
    assert r.status_code == 200
    assert "agent_secret" in r.json()


def test_existing_agent_write_needs_no_workspace_key(client):
    c, ws = client
    _, raw_key = ws.create_workspace(owner_email="a@example.com")
    r1 = c.post("/v1/track", headers=_headers(),
                json={"agent_id": "keyed-agent-2", "rail": "api_key",
                      "amount_cents": 100, "service": "s", "workspace_key": raw_key})
    secret = r1.json()["agent_secret"]
    r2 = c.post("/v1/track", headers=_headers(),
                json={"agent_id": "keyed-agent-2", "rail": "api_key",
                      "amount_cents": 50, "service": "s", "agent_secret": secret})
    assert r2.status_code == 200


def test_free_workspace_agent_cap_enforced(client):
    c, ws = client
    ws.WORKSPACE_SCARCITY_CAP = 0
    _, raw_key = ws.create_workspace(owner_email="a@example.com")
    for i in range(3):
        r = c.post("/v1/track", headers=_headers(),
                    json={"agent_id": f"cap-agent-{i}", "rail": "api_key",
                          "amount_cents": 1, "service": "s", "workspace_key": raw_key})
        assert r.status_code == 200
    r4 = c.post("/v1/track", headers=_headers(),
                json={"agent_id": "cap-agent-3", "rail": "api_key",
                      "amount_cents": 1, "service": "s", "workspace_key": raw_key})
    assert r4.status_code == 402
