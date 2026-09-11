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
    import ledger_engine, workspace_engine, identity, routes_agents, api_server
    importlib.reload(workspace_engine)
    importlib.reload(ledger_engine)
    importlib.reload(identity)
    importlib.reload(routes_agents)
    importlib.reload(api_server)
    from fastapi.testclient import TestClient
    tc = TestClient(api_server.app)
    _, raw_key = workspace_engine.create_workspace(owner_email="a@example.com")
    r = tc.post("/v1/track", headers={"AL-API-Version": AL_VERSION},
                json={"agent_id": "read-scope-agent", "rail": "api_key",
                      "amount_cents": 100, "service": "s", "workspace_key": raw_key})
    secret = r.json()["agent_secret"]
    yield tc, raw_key, secret
    shutil.rmtree(tmp, ignore_errors=True)


def test_report_requires_a_credential(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/report/read-scope-agent")
    assert r.status_code == 401


def test_report_accepts_agent_secret(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/report/read-scope-agent", headers={"X-Agent-Secret": secret})
    assert r.status_code == 200


def test_report_accepts_workspace_key(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/report/read-scope-agent", headers={"X-Workspace-Key": raw_key})
    assert r.status_code == 200


def test_report_rejects_wrong_credential(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/report/read-scope-agent", headers={"X-Agent-Secret": "wrong"})
    assert r.status_code == 401


def test_html_report_also_scoped(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/report/read-scope-agent/html")
    assert r.status_code == 401
    r2 = tc.get("/v1/report/read-scope-agent/html", headers={"X-Agent-Secret": secret})
    assert r2.status_code == 200
