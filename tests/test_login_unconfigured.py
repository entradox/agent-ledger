# tests/test_login_unconfigured.py
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    import importlib
    import ledger_engine, workspace_engine, routes_agents, api_server
    importlib.reload(workspace_engine)
    importlib.reload(ledger_engine)
    importlib.reload(routes_agents)
    importlib.reload(api_server)
    from fastapi.testclient import TestClient
    yield TestClient(api_server.app, raise_server_exceptions=False)


def test_login_returns_503_when_google_unconfigured(client):
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 503
    assert r.status_code != 500
    assert r.json()["error"]["code"] == "login_not_configured"


def test_google_callback_returns_503_not_500_when_google_unconfigured(client):
    client.cookies.set("al_oauth_state", "dummy-state")
    r = client.get("/auth/google/callback",
                    params={"code": "dummy-code", "state": "dummy-state"},
                    follow_redirects=False)
    assert r.status_code != 500
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "login_not_configured"


def test_google_callback_maps_exchange_failure_to_400(client, monkeypatch):
    import oauth_google

    def _raise_exchange_failed(code):
        raise oauth_google.GoogleExchangeFailed("token exchange did not return access_token")

    monkeypatch.setattr(oauth_google, "exchange_code", _raise_exchange_failed)
    client.cookies.set("al_oauth_state", "dummy-state")
    r = client.get("/auth/google/callback",
                    params={"code": "dummy-code", "state": "dummy-state"},
                    follow_redirects=False)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "google_exchange_failed"


def test_non_config_keyerror_is_not_mislabeled_as_unconfigured(client, monkeypatch):
    import oauth_google

    def _raise_unrelated_keyerror(code):
        raise KeyError("some_unrelated_key")

    monkeypatch.setattr(oauth_google, "exchange_code", _raise_unrelated_keyerror)
    client.cookies.set("al_oauth_state", "dummy-state")
    r = client.get("/auth/google/callback",
                    params={"code": "dummy-code", "state": "dummy-state"},
                    follow_redirects=False)
    assert r.status_code == 500
    assert r.status_code != 503
