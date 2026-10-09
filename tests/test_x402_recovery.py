# tests/test_x402_recovery.py — D-1576 wallet-signed workspace_key recovery
import shutil, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

eth_account = pytest.importorskip("eth_account",
                                  reason="eth_account ships via x402[evm]")
from eth_account import Account
from eth_account.messages import encode_defunct


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


def _sign(acct, wallet, ts):
    msg = f"agent-ledger:recover:{wallet}:{ts}"
    return acct.sign_message(encode_defunct(text=msg)).signature.hex()


def _mint_wallet_workspace(wallet):
    import workspace_engine
    ws_id, raw_key = workspace_engine.create_workspace(wallet_address=wallet)
    return ws_id, raw_key


def test_recovery_returns_a_fresh_key_and_kills_the_old_one(client):
    c, _ = client
    acct = Account.create()
    ws_id, old_key = _mint_wallet_workspace(acct.address)

    ts = int(time.time())
    r = c.post("/v1/billing/x402/recover", json={
        "wallet": acct.address, "timestamp": ts,
        "signature": "0x" + _sign(acct, acct.address, ts)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workspace_id"] == ws_id
    new_key = body["workspace_key"]
    assert new_key.startswith("wk_live_") and new_key != old_key

    # Old key is dead, new key works against a workspace-gated read.
    import workspace_engine
    assert workspace_engine.get_workspace_by_key(old_key) is None
    assert workspace_engine.get_workspace_by_key(new_key)["workspace_id"] == ws_id


def test_wrong_wallet_signature_rejected(client):
    c, _ = client
    payer = Account.create()
    _mint_wallet_workspace(payer.address)
    attacker = Account.create()  # signs, but for a wallet that isn't the payer's

    ts = int(time.time())
    # attacker signs the challenge naming the PAYER's wallet — recovers to
    # attacker, not payer → must not hand out the payer's workspace.
    r = c.post("/v1/billing/x402/recover", json={
        "wallet": payer.address, "timestamp": ts,
        "signature": "0x" + _sign(attacker, payer.address, ts)})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "recover_wrong_wallet"


def test_stale_timestamp_rejected(client):
    c, _ = client
    acct = Account.create()
    _mint_wallet_workspace(acct.address)
    ts = int(time.time()) - 3600  # an hour old — replay window closed
    r = c.post("/v1/billing/x402/recover", json={
        "wallet": acct.address, "timestamp": ts,
        "signature": "0x" + _sign(acct, acct.address, ts)})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "recover_stale_timestamp"


def test_valid_signature_but_wallet_never_paid_is_404(client):
    c, _ = client
    acct = Account.create()  # owns the wallet, but no workspace is bound to it
    ts = int(time.time())
    r = c.post("/v1/billing/x402/recover", json={
        "wallet": acct.address, "timestamp": ts,
        "signature": "0x" + _sign(acct, acct.address, ts)})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "recover_no_workspace"


def test_garbage_signature_is_typed_401_not_500(client):
    c, _ = client
    acct = Account.create()
    _mint_wallet_workspace(acct.address)
    r = c.post("/v1/billing/x402/recover", json={
        "wallet": acct.address, "timestamp": int(time.time()),
        "signature": "0xdeadbeef"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "recover_bad_signature"


def test_bad_wallet_shape_is_400(client):
    c, _ = client
    r = c.post("/v1/billing/x402/recover", json={
        "wallet": "not-an-address", "timestamp": int(time.time()),
        "signature": "0x1234"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "recover_bad_fields"
