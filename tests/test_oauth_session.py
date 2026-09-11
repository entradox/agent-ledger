# tests/test_oauth_session.py
import shutil
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os

import pytest


def test_session_roundtrip(monkeypatch):
    monkeypatch.setenv("AL_SESSION_SECRET", "test-secret-for-signing")
    import importlib, session_auth
    importlib.reload(session_auth)
    token = session_auth.sign_session("ws_abc123")
    assert session_auth.verify_session(token) == "ws_abc123"


def test_tampered_session_rejected(monkeypatch):
    monkeypatch.setenv("AL_SESSION_SECRET", "test-secret-for-signing")
    import importlib, session_auth
    importlib.reload(session_auth)
    token = session_auth.sign_session("ws_abc123")
    tampered = token[:-4] + "xxxx"
    assert session_auth.verify_session(tampered) is None


def test_google_auth_url_contains_client_id(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    import importlib, oauth_google
    importlib.reload(oauth_google)
    url = oauth_google.google_auth_url("state123")
    assert "test-client-id" in url
    assert "state123" in url


# --- Google callback: login must not reissue (and thereby break) the key ----

@pytest.fixture
def auth_client(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="agent-ledger-oauth-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    monkeypatch.setenv("AL_SESSION_SECRET", "test-secret-for-signing")
    import importlib
    import workspace_engine, ledger_engine, identity, session_auth
    import routes_auth, api_server
    importlib.reload(workspace_engine)
    importlib.reload(ledger_engine)
    importlib.reload(identity)
    importlib.reload(session_auth)
    importlib.reload(routes_auth)
    importlib.reload(api_server)
    monkeypatch.setattr(
        "oauth_google.exchange_code",
        lambda code: {"email": "u@example.com", "google_sub": "g-login-1"})
    from fastapi.testclient import TestClient
    tc = TestClient(api_server.app, follow_redirects=False)
    yield tc, workspace_engine
    shutil.rmtree(tmp, ignore_errors=True)


def _callback(tc):
    tc.cookies.set("al_oauth_state", "s1")
    return tc.get("/auth/google/callback?code=abc&state=s1")


def test_first_login_reveals_key_once(auth_client):
    tc, workspace_engine = auth_client
    r = _callback(tc)
    assert r.status_code == 307
    assert "al_key_reveal" in r.cookies
    assert workspace_engine.get_workspace_by_key(r.cookies["al_key_reveal"]) is not None


def test_returning_login_does_not_reissue_or_reveal(auth_client):
    """C4: every Google login used to mint a fresh key, invalidating the one
    the user was already using. A returning login must reveal nothing and
    break nothing."""
    tc, workspace_engine = auth_client
    first = _callback(tc)
    original_key = first.cookies["al_key_reveal"]
    tc.cookies.clear()

    second = _callback(tc)
    assert second.status_code == 307
    assert "al_key_reveal" not in second.cookies
    # the key issued at signup still resolves
    assert workspace_engine.get_workspace_by_key(original_key) is not None


def test_dashboard_does_not_promise_key_recovery(auth_client):
    tc, workspace_engine = auth_client
    _callback(tc)
    tc.cookies.delete("al_key_reveal")
    page = tc.get("/dashboard").text
    assert "shown once at signup" in page
    assert "not available yet" in page
    # must not offer re-login as a key-recovery path — it no longer is one
    assert "Log in again" not in page
