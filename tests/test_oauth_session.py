# tests/test_oauth_session.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os


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
