import os, shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

AL_VERSION = "2026-09-01"


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    monkeypatch.setenv("AL_SESSION_SECRET", "test-secret-for-signing")
    import importlib
    import ledger_engine, workspace_engine, identity, routes_agents, api_server
    import session_auth
    importlib.reload(workspace_engine)
    importlib.reload(ledger_engine)
    importlib.reload(identity)
    importlib.reload(session_auth)
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


def _workspace_id_of(raw_key):
    import workspace_engine
    return workspace_engine.get_workspace_by_key(raw_key)["workspace_id"]


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


def test_html_report_accepts_workspace_key(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/report/read-scope-agent/html", headers={"X-Workspace-Key": raw_key})
    assert r.status_code == 200


def test_html_report_rejects_wrong_credential(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/report/read-scope-agent/html", headers={"X-Agent-Secret": "wrong"})
    assert r.status_code == 401


def test_tokens_requires_a_credential(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/tokens/read-scope-agent")
    assert r.status_code == 401


def test_tokens_accepts_agent_secret(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/tokens/read-scope-agent", headers={"X-Agent-Secret": secret})
    assert r.status_code == 200


def test_tokens_accepts_workspace_key(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/tokens/read-scope-agent", headers={"X-Workspace-Key": raw_key})
    assert r.status_code == 200


def test_tokens_rejects_wrong_credential(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/tokens/read-scope-agent", headers={"X-Agent-Secret": "wrong"})
    assert r.status_code == 401


def test_alerts_requires_a_credential(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/alerts/read-scope-agent")
    assert r.status_code == 401


def test_alerts_accepts_agent_secret(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/alerts/read-scope-agent", headers={"X-Agent-Secret": secret})
    assert r.status_code == 200


def test_alerts_accepts_workspace_key(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/alerts/read-scope-agent", headers={"X-Workspace-Key": raw_key})
    assert r.status_code == 200


def test_alerts_rejects_wrong_credential(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/alerts/read-scope-agent", headers={"X-Agent-Secret": "wrong"})
    assert r.status_code == 401


# --- session-cookie arm (C2: the dashboard's own agent links used to 401) ----

def test_html_report_accepts_session_cookie_for_own_workspace(client):
    """A browser following the dashboard's /v1/report/{id}/html link sends
    only al_session — no headers at all. That must authorize."""
    tc, raw_key, secret = client
    import session_auth
    cookie = session_auth.sign_session(_workspace_id_of(raw_key))
    tc.cookies.set("al_session", cookie)
    r = tc.get("/v1/report/read-scope-agent/html")
    assert r.status_code == 200


def test_session_cookie_from_other_workspace_still_401(client):
    """The security property that matters: a valid session must NOT grant
    access to an agent belonging to a different workspace."""
    tc, raw_key, secret = client
    import workspace_engine, session_auth
    other_ws_id, _ = workspace_engine.create_workspace(owner_email="intruder@example.com")
    tc.cookies.set("al_session", session_auth.sign_session(other_ws_id))
    r = tc.get("/v1/report/read-scope-agent/html")
    assert r.status_code == 401


def test_forged_session_cookie_rejected(client):
    tc, raw_key, secret = client
    tc.cookies.set("al_session", _workspace_id_of(raw_key) + ".deadbeef")
    r = tc.get("/v1/report/read-scope-agent/html")
    assert r.status_code == 401


def test_json_report_also_accepts_session_cookie(client):
    tc, raw_key, secret = client
    import session_auth
    cookie = session_auth.sign_session(_workspace_id_of(raw_key))
    tc.cookies.set("al_session", cookie)
    r = tc.get("/v1/report/read-scope-agent")
    assert r.status_code == 200
