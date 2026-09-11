import hashlib, hmac, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os, shutil, tempfile
import pytest


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET_AL", "whsec_test")
    import importlib
    import ledger_engine, workspace_engine, identity, routes_billing, api_server
    importlib.reload(workspace_engine)
    importlib.reload(ledger_engine)
    importlib.reload(identity)
    importlib.reload(routes_billing)
    importlib.reload(api_server)
    from fastapi.testclient import TestClient
    yield TestClient(api_server.app), workspace_engine
    shutil.rmtree(tmp, ignore_errors=True)


def _signed_webhook_body(payload: dict, secret: str) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    t = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={t},v1={sig}"


def test_completed_checkout_marks_correct_workspace_pro(client):
    c, ws = client
    workspace_id, _ = ws.create_workspace(owner_email="a@example.com")
    payload = {
        "type": "checkout.session.completed",
        "data": {"object": {
            "customer_details": {"email": "a@example.com"},
            "amount_total": 1900,
            "id": "cs_test_1",
            "client_reference_id": workspace_id,
            "customer": "cus_test_1",
        }},
    }
    body, sig = _signed_webhook_body(payload, "whsec_test")
    r = c.post("/stripe/webhook", content=body, headers={"stripe-signature": sig})
    assert r.status_code == 200
    assert ws.is_workspace_pro(workspace_id) is True


def test_completed_checkout_does_not_mark_other_workspaces_pro(client, monkeypatch):
    c, ws = client
    # Neutralize the scarcity-window auto-pro (Task 1: first N workspaces are
    # minted pro by default) so this test isolates webhook scoping behavior,
    # not creation-time scarcity.
    monkeypatch.setattr(ws, "WORKSPACE_SCARCITY_CAP", 0)
    workspace_a, _ = ws.create_workspace(owner_email="a@example.com")
    workspace_b, _ = ws.create_workspace(owner_email="b@example.com")
    assert ws.is_workspace_pro(workspace_b) is False  # sanity: starts free
    payload = {
        "type": "checkout.session.completed",
        "data": {"object": {
            "customer_details": {"email": "a@example.com"},
            "amount_total": 1900, "id": "cs_test_2",
            "client_reference_id": workspace_a, "customer": "cus_test_2",
        }},
    }
    body, sig = _signed_webhook_body(payload, "whsec_test")
    c.post("/stripe/webhook", content=body, headers={"stripe-signature": sig})
    assert ws.is_workspace_pro(workspace_a) is True
    assert ws.is_workspace_pro(workspace_b) is False


def test_checkout_endpoint_requires_session(client):
    c, ws = client
    r = c.post("/v1/billing/checkout")
    assert r.status_code == 401


def test_checkout_endpoint_returns_checkout_url_for_logged_in_workspace(client, monkeypatch):
    c, ws = client
    import identity, routes_billing
    workspace_id, _ = ws.create_workspace(owner_email="a@example.com")

    monkeypatch.setattr(identity, "resolve_session", lambda cookie: workspace_id)
    monkeypatch.setenv("AL_STRIPE_PRICE_ID", "price_test_123")
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_fake")

    class _FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps({"url": "https://checkout.stripe.com/pay/cs_test_fake"}).encode()

    def _fake_urlopen(req, timeout=10):
        return _FakeResp()

    monkeypatch.setattr(routes_billing.urllib.request, "urlopen", _fake_urlopen)

    c.cookies.set("al_session", "whatever")
    r = c.post("/v1/billing/checkout")
    assert r.status_code == 200
    assert r.json() == {"checkout_url": "https://checkout.stripe.com/pay/cs_test_fake"}
