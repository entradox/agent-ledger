# tests/test_x402_renewable_pass.py
"""The $0.01 x402 pass is RENEWABLE per wallet (blueprint v2, 2026-10-11).

x402 is now the only paid path on the domain, so "pay again" and "pay the
first time" are the same endpoint. A repeat settlement from a known wallet
resolves to its existing workspace (no second workspace, no second key —
the raw key only exists at mint) and resets the Pro window. Money already
moved is honoured, never refused.

These tests drive the REAL verify_payment through a stub resource server (the
same seam the SDK sits behind), so they exercise the payer-from-payload read and
count how many times settlement was actually attempted. Mocking verify_payment
wholesale, as the older route tests do, would prove nothing about the wallet
resolution.
"""
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

CHECKSUMMED = "0xAbCdEf0123456789aBcDeF0123456789AbCdEf01"


class _StubResourceServer:
    """Stands in for x402HTTPResourceServerSync: verification passes, and every
    process_settlement call is counted (a call == USDC moved)."""

    def __init__(self, payer):
        self.payer = payer
        self.settle_calls = 0

    def process_http_request(self, ctx):
        return SimpleNamespace(
            response=None,
            payment_payload=SimpleNamespace(
                payload={"authorization": {"from": self.payer}}),
            payment_requirements=SimpleNamespace(
                pay_to="0xTreasury", amount="10000", asset="0xUSDC",
                network="eip155:84532"))

    def process_settlement(self, payload, reqs, ctx):
        self.settle_calls += 1
        return SimpleNamespace(success=True, payer=self.payer,
                               transaction=f"0xTX{self.settle_calls}",
                               network="eip155:84532", headers={},
                               error_reason=None, response=None)


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, workspace_engine, identity, routes_billing, api_server
    importlib.reload(workspace_engine)
    importlib.reload(ledger_engine)
    importlib.reload(identity)
    importlib.reload(routes_billing)
    importlib.reload(api_server)
    import x402_verify
    stub = _StubResourceServer(payer=CHECKSUMMED.lower())
    monkeypatch.setattr(x402_verify, "X402_ENABLED", True)
    monkeypatch.setattr(x402_verify, "resource_server", stub)
    monkeypatch.setattr(x402_verify, "HTTPRequestContext",
                        lambda **kw: SimpleNamespace(**kw))
    from fastapi.testclient import TestClient
    yield SimpleNamespace(client=TestClient(api_server.app), stub=stub,
                          workspace_engine=workspace_engine,
                          x402_verify=x402_verify)
    shutil.rmtree(tmp, ignore_errors=True)


def _pay(env):
    return env.client.post("/v1/billing/x402", headers={"X-PAYMENT": "signed"})


def test_repeat_purchase_from_same_wallet_renews_the_pass(env):
    """THE contract: paying again is honoured, and it renews the same workspace
    rather than minting a duplicate or issuing a second key."""
    r1 = _pay(env)
    assert r1.status_code == 200
    assert env.stub.settle_calls == 1
    ws1 = r1.json()["workspace_id"]

    import time
    before = env.workspace_engine.get_workspace(ws1)["pro_until"]

    time.sleep(0.01)  # the window must visibly reset forward
    r2 = _pay(env)
    assert r2.status_code == 200, "the only paid path cannot refuse a repeat customer"
    err_body = r2.json()
    # Renewed on the SAME workspace — the wallet is the identity.
    assert err_body["workspace_id"] == ws1
    # The raw key was shown once at mint and is never re-exposed.
    assert err_body["workspace_key"] is None
    # USDC actually moved on the second payment.
    assert env.stub.settle_calls == 2
    after = env.workspace_engine.get_workspace(ws1)["pro_until"]
    assert after > before, "the repeat payment did not extend the pass window"
    assert env.workspace_engine.is_workspace_pro(ws1)


def test_checksummed_and_lowercase_address_resolve_to_the_same_workspace(env):
    env.stub.payer = CHECKSUMMED                    # first purchase: checksummed
    r1 = _pay(env)
    assert r1.status_code == 200
    env.stub.payer = CHECKSUMMED.lower()            # second: lowercase
    r2 = _pay(env)
    assert r2.status_code == 200
    env.stub.payer = CHECKSUMMED.upper().replace("0X", "0x")
    r3 = _pay(env)
    assert r3.status_code == 200
    ids = {r1.json()["workspace_id"], r2.json()["workspace_id"],
           r3.json()["workspace_id"]}
    assert len(ids) == 1, "address-case variants minted separate workspaces"
    assert env.stub.settle_calls == 3


def test_a_different_wallet_gets_its_own_workspace(env):
    r1 = _pay(env)
    env.stub.payer = "0x" + "b" * 40
    r2 = _pay(env)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["workspace_id"] != r2.json()["workspace_id"]
    assert r2.json()["workspace_key"] is not None, (
        "a first-time wallet mints a NEW workspace and gets its one-time key")
    assert env.stub.settle_calls == 2


def test_pre_existing_workspace_indexed_under_the_old_raw_case_key_is_found(env):
    """A workspace minted before normalization has its by_wallet index keyed by
    the address exactly as settlement returned it. It must still resolve."""
    we = env.workspace_engine
    wid = "ws_legacy_entry"
    we._write_workspace({"workspace_id": wid, "wallet_address": CHECKSUMMED,
                         "plan": "pro", "agent_cap": None, "pro_until": None})
    (we._index_dir("by_wallet") / we._hash(CHECKSUMMED)).write_text(wid)  # OLD key

    assert we.get_workspace_by_wallet(CHECKSUMMED.lower())["workspace_id"] == wid
    env.stub.payer = CHECKSUMMED.lower()
    r = _pay(env)
    assert r.status_code == 200
    assert r.json()["workspace_id"] == wid
    assert r.json()["workspace_key"] is None
    assert env.stub.settle_calls == 1


def test_repeat_purchase_never_downgrades_a_stripe_subscribed_workspace(env):
    """Money never removes capability: a wallet-bound workspace that still
    carries a Stripe subscription (grandfathered from the retired human door)
    keeps its plan when its wallet buys a pass — the payment is honoured, the
    subscription is not overwritten by a 24h window."""
    we = env.workspace_engine
    ws_id, _ = we.create_workspace(wallet_address=CHECKSUMMED.lower(),
                                   grant_scarcity=False)
    we.mark_pro(ws_id, "cus_live_sub", stripe_subscription_id="sub_1",
                pro_until=None, period_source="checkout.session.completed",
                tier="team")
    env.stub.payer = CHECKSUMMED.lower()
    r = _pay(env)
    assert r.status_code == 200
    assert r.json()["workspace_id"] == ws_id
    rec = we.get_workspace(ws_id)
    assert rec["plan"] == "team", (
        "the $0.01 pass overwrote a paid Stripe subscription — money removed "
        "capability")
    assert rec.get("stripe_customer_id") == "cus_live_sub"


def test_grant_knob_starter_caps_each_purchase_at_the_starter_limit(env, monkeypatch):
    monkeypatch.setattr(env.x402_verify, "X402_TRIAL_GRANT", "starter")
    r = _pay(env)
    ws = env.workspace_engine.get_workspace(r.json()["workspace_id"])
    assert ws["plan"] == "starter"
    assert ws["agent_cap"] == env.workspace_engine.STARTER_AGENT_CAP == 10
    assert ws["pro_until"] is not None          # still a time-boxed pass
    assert env.workspace_engine.is_workspace_pro(ws["workspace_id"]) is True


def test_unrecognised_grant_value_falls_back_to_unlimited(monkeypatch):
    monkeypatch.setenv("X402_TRIAL_GRANT", "starterr")
    import importlib, x402_verify
    try:
        importlib.reload(x402_verify)
        assert x402_verify.X402_TRIAL_GRANT == "unlimited"
        assert x402_verify.x402_pass_tier() == "pro"
    finally:
        monkeypatch.delenv("X402_TRIAL_GRANT")
        importlib.reload(x402_verify)


def test_unreadable_payer_does_not_block_settlement(env):
    """If the payer cannot be read from the payload we do not refuse a payment we
    cannot attribute; the route still honours and logs it."""
    env.stub.process_http_request = lambda ctx: SimpleNamespace(
        response=None, payment_payload=SimpleNamespace(payload={}),
        payment_requirements=SimpleNamespace(pay_to="0xTreasury", amount="10000",
                                             asset="0xUSDC",
                                             network="eip155:84532"))
    assert _pay(env).status_code == 200
    assert env.stub.settle_calls == 1
