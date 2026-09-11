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


def test_checkout_endpoint_requires_workspace_credentials(client):
    """No session, no cookie: the retired Google arm is gone, so a caller
    with nothing gets a typed 401 rather than "log in first"."""
    c, ws = client
    r = c.post("/v1/billing/checkout")
    assert r.status_code == 401


def test_checkout_endpoint_rejects_mismatched_workspace_key(client):
    c, ws = client
    workspace_id, _raw_key = ws.create_workspace(owner_email="a@example.com")
    r = c.post("/v1/billing/checkout",
               headers={"X-Workspace-Id": workspace_id,
                        "X-Workspace-Key": "wk_live_not_the_key"})
    assert r.status_code == 401


def test_checkout_url_carries_client_reference_id_for_that_workspace(client, monkeypatch):
    """The point of the re-key (D-1162): the upgrade link must identify the
    workspace, because client_reference_id is what the Stripe webhook reads to
    mark it Pro. Without it a real card payment charges the card and upgrades
    nothing. No Stripe API key and no browser session is involved any more."""
    c, ws = client
    workspace_id, raw_key = ws.create_workspace(owner_email="a@example.com")
    monkeypatch.setenv("AL_STRIPE_PAYMENT_LINK", "https://buy.stripe.com/test_link")
    r = c.post("/v1/billing/checkout",
               headers={"X-Workspace-Id": workspace_id, "X-Workspace-Key": raw_key})
    assert r.status_code == 200
    body = r.json()
    assert body["workspace_id"] == workspace_id
    assert body["checkout_url"] == (
        f"https://buy.stripe.com/test_link?client_reference_id={workspace_id}")
    assert raw_key not in json.dumps(body)
