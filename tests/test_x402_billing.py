# tests/test_x402_billing.py
import shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, workspace_engine, identity, routes_billing, api_server
    importlib.reload(workspace_engine)
    importlib.reload(ledger_engine)
    importlib.reload(identity)
    importlib.reload(routes_billing)
    importlib.reload(api_server)
    from fastapi.testclient import TestClient
    yield TestClient(api_server.app), api_server
    shutil.rmtree(tmp, ignore_errors=True)


def test_verified_payment_mints_workspace(client, monkeypatch):
    c, api_server = client
    monkeypatch.setattr(
        "x402_verify.verify_payment",
        lambda header: {"verified": True, "payer_wallet": "0xABC", "tx_hash": "0xTX1"})
    r = c.post("/v1/billing/x402", headers={"X-PAYMENT": "fake-payment-header"})
    assert r.status_code == 200
    assert "workspace_key" in r.json()


def test_unverified_payment_rejected(client, monkeypatch):
    c, api_server = client
    monkeypatch.setattr(
        "x402_verify.verify_payment",
        lambda header: {"verified": False, "payer_wallet": None, "tx_hash": None})
    r = c.post("/v1/billing/x402", headers={"X-PAYMENT": "bad"})
    assert r.status_code == 402


def test_replayed_tx_hash_returns_identical_cached_response(client, monkeypatch):
    """Spec 3b: the settlement tx_hash is the idempotency key. A replay is a
    true no-op — the byte-identical original response, including the key
    minted the first time. Never a fresh key, never a second workspace."""
    c, api_server = client
    monkeypatch.setattr(
        "x402_verify.verify_payment",
        lambda header: {"verified": True, "payer_wallet": "0xDEF", "tx_hash": "0xTX2"})
    r1 = c.post("/v1/billing/x402", headers={"X-PAYMENT": "fake"})
    r2 = c.post("/v1/billing/x402", headers={"X-PAYMENT": "fake-again"})
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()
    assert r1.json()["workspace_key"] is not None


def test_second_payment_same_wallet_new_tx_does_not_invalidate_key(client, monkeypatch):
    """A genuinely new payment from a known wallet resolves to the existing
    workspace with workspace_key: null — the previously issued key must keep
    working (the old behavior silently reissued and broke it)."""
    c, api_server = client
    import workspace_engine
    monkeypatch.setattr(
        "x402_verify.verify_payment",
        lambda header: {"verified": True, "payer_wallet": "0xGHI", "tx_hash": "0xTXA"})
    r1 = c.post("/v1/billing/x402", headers={"X-PAYMENT": "pay1"})
    first_key = r1.json()["workspace_key"]

    monkeypatch.setattr(
        "x402_verify.verify_payment",
        lambda header: {"verified": True, "payer_wallet": "0xGHI", "tx_hash": "0xTXB"})
    r2 = c.post("/v1/billing/x402", headers={"X-PAYMENT": "pay2"})

    assert r2.status_code == 200
    assert r2.json()["workspace_id"] == r1.json()["workspace_id"]
    assert r2.json()["workspace_key"] is None
    assert "message" in r2.json()
    # the original key still resolves — nothing was invalidated
    assert workspace_engine.get_workspace_by_key(first_key)["workspace_id"] \
        == r1.json()["workspace_id"]


def test_verified_payment_without_tx_hash_refused(client, monkeypatch):
    """No tx_hash = no replay key = a payment that cannot be deduplicated.
    Fail closed rather than mint."""
    c, api_server = client
    monkeypatch.setattr(
        "x402_verify.verify_payment",
        lambda header: {"verified": True, "payer_wallet": "0xJKL", "tx_hash": None})
    r = c.post("/v1/billing/x402", headers={"X-PAYMENT": "no-hash"})
    assert r.status_code == 402


def test_different_wallets_get_different_workspaces(client, monkeypatch):
    c, api_server = client
    monkeypatch.setattr(
        "x402_verify.verify_payment",
        lambda header: {"verified": True, "payer_wallet": "0xAAA", "tx_hash": "0xT1"})
    r1 = c.post("/v1/billing/x402", headers={"X-PAYMENT": "p"})
    monkeypatch.setattr(
        "x402_verify.verify_payment",
        lambda header: {"verified": True, "payer_wallet": "0xBBB", "tx_hash": "0xT2"})
    r2 = c.post("/v1/billing/x402", headers={"X-PAYMENT": "p"})
    assert r1.json()["workspace_id"] != r2.json()["workspace_id"]
    assert r1.json()["workspace_key"] and r2.json()["workspace_key"]
