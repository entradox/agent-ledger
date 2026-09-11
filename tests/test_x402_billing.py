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


def test_same_wallet_twice_returns_same_workspace(client, monkeypatch):
    c, api_server = client
    monkeypatch.setattr(
        "x402_verify.verify_payment",
        lambda header: {"verified": True, "payer_wallet": "0xDEF", "tx_hash": "0xTX2"})
    r1 = c.post("/v1/billing/x402", headers={"X-PAYMENT": "fake"})
    r2 = c.post("/v1/billing/x402", headers={"X-PAYMENT": "fake-again"})
    assert r1.json()["workspace_id"] == r2.json()["workspace_id"]
