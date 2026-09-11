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


def test_replayed_tx_hash_resolves_same_workspace_without_re_exposing_key(client, monkeypatch):
    """Spec 3b: the settlement tx_hash is the idempotency key, so a replay
    never mints a second workspace. But the raw workspace_key is shown ONCE,
    in the original response — the cached replay must not hand it out again
    to whoever can name the tx_hash. Same rule /v1/track already applies to
    the minted agent_secret."""
    c, api_server = client
    monkeypatch.setattr(
        "x402_verify.verify_payment",
        lambda header: {"verified": True, "payer_wallet": "0xDEF", "tx_hash": "0xTX2"})
    r1 = c.post("/v1/billing/x402", headers={"X-PAYMENT": "fake"})
    r2 = c.post("/v1/billing/x402", headers={"X-PAYMENT": "fake-again"})
    assert r1.status_code == r2.status_code == 200
    # same workspace, no second mint
    assert r2.json()["workspace_id"] == r1.json()["workspace_id"]
    first_key = r1.json()["workspace_key"]
    assert first_key is not None
    # ...but the replay does NOT re-expose it
    assert r2.json().get("workspace_key") is None
    assert first_key not in str(r2.json())
    assert "not included in cached" in r2.json()["_note"]


def test_raw_workspace_key_never_persisted_in_idempotency_store(client, monkeypatch):
    """Defence in depth for the above: the key must be absent from the
    at-rest cache itself, not merely filtered on the way out."""
    c, api_server = client
    monkeypatch.setattr(
        "x402_verify.verify_payment",
        lambda header: {"verified": True, "payer_wallet": "0xCACHE", "tx_hash": "0xTXCACHE"})
    r = c.post("/v1/billing/x402", headers={"X-PAYMENT": "fake"})
    raw_key = r.json()["workspace_key"]
    assert raw_key
    import ledger_engine
    blob = ""
    for p in ledger_engine.DATA_DIR.rglob("*"):
        if p.is_file():
            try:
                blob += p.read_text()
            except Exception:
                continue
    assert raw_key not in blob, "raw workspace_key found at rest"


def test_missing_payer_wallet_is_a_typed_402_not_a_500(client, monkeypatch):
    """A verified settlement with no payer is an identity we cannot bind a
    workspace to. Previously surfaced as sqlite3.IntegrityError -> 500."""
    c, api_server = client
    monkeypatch.setattr(
        "x402_verify.verify_payment",
        lambda header: {"verified": True, "payer_wallet": None, "tx_hash": "0xTXNP"})
    r = c.post("/v1/billing/x402", headers={"X-PAYMENT": "no-payer"})
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "x402_no_payer_wallet"


def test_settlement_to_wrong_recipient_is_refused(client, monkeypatch):
    """Any verified settlement used to mint a workspace regardless of who it
    actually paid."""
    c, api_server = client
    monkeypatch.setenv("X402_RECEIVING_ADDRESS", "0xOURTREASURY")
    monkeypatch.setattr(
        "x402_verify.verify_payment",
        lambda header: {"verified": True, "payer_wallet": "0xW", "tx_hash": "0xTXR1",
                        "recipient": "0xSOMEONEELSE"})
    r = c.post("/v1/billing/x402", headers={"X-PAYMENT": "wrong-recipient"})
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "x402_recipient_mismatch"


def test_settlement_without_recipient_is_refused_when_address_configured(client, monkeypatch):
    c, api_server = client
    monkeypatch.setenv("X402_RECEIVING_ADDRESS", "0xOURTREASURY")
    monkeypatch.setattr(
        "x402_verify.verify_payment",
        lambda header: {"verified": True, "payer_wallet": "0xW", "tx_hash": "0xTXR2"})
    r = c.post("/v1/billing/x402", headers={"X-PAYMENT": "no-recipient"})
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "x402_recipient_mismatch"


def test_settlement_to_our_address_mints(client, monkeypatch):
    """Case-insensitive match (hex addresses are commonly checksummed)."""
    c, api_server = client
    monkeypatch.setenv("X402_RECEIVING_ADDRESS", "0xOurTreasury")
    monkeypatch.setattr(
        "x402_verify.verify_payment",
        lambda header: {"verified": True, "payer_wallet": "0xW2", "tx_hash": "0xTXR3",
                        "recipient": "0xOURTREASURY"})
    r = c.post("/v1/billing/x402", headers={"X-PAYMENT": "right-recipient"})
    assert r.status_code == 200
    assert r.json()["workspace_key"]


def test_verify_payment_surfaces_settlement_detail(monkeypatch):
    """The route can only validate fields verify_payment actually returns."""
    import io, json as _json, x402_verify
    monkeypatch.setattr(x402_verify, "FACILITATOR_URL", "https://facilitator.test/verify")

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=10: _Resp(_json.dumps({
            "verified": True, "payer": "0xP", "transaction_hash": "0xT",
            "recipient": "0xR", "amount": "1000", "asset": "USDC"}).encode()))
    out = x402_verify.verify_payment("hdr")
    assert out["recipient"] == "0xR"
    assert out["amount"] == "1000"
    assert out["asset"] == "USDC"


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
