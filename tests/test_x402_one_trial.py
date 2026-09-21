# tests/test_x402_one_trial.py
"""The $0.01 x402 pass is a ONE-TIME trial per wallet.

Before this, $0.01 bought 24h of unlimited agents and could be bought forever,
so a rational agent paid $0.30/mo and never touched the $19 tier. The check
lives in x402_verify.verify_payment — after verification, BEFORE settlement —
because the route only learns the wallet after the money has moved.

These tests drive the REAL verify_payment through a stub resource server (the
same seam the SDK sits behind), so they exercise the payer-from-payload read and
count how many times settlement was actually attempted. Mocking verify_payment
wholesale, as the older route tests do, would prove nothing about this check.
"""
import logging
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


def test_second_trial_from_same_wallet_is_refused_before_settlement(env):
    r1 = _pay(env)
    assert r1.status_code == 200
    assert env.stub.settle_calls == 1

    r2 = _pay(env)
    assert r2.status_code == 403
    # THE money assertion: settlement was never attempted, so nothing was charged.
    assert env.stub.settle_calls == 1, "refusal must happen BEFORE settlement"
    err = r2.json()["error"]
    assert err["type"] == "permission_error"
    assert err["code"] == "x402_trial_already_used"
    assert err["param"] == "wallet"
    assert "/start?plan=starter" in err["message"]
    assert "NOT charged" in err["message"]


def test_checksummed_and_lowercase_address_are_the_same_wallet(env):
    env.stub.payer = CHECKSUMMED                    # first purchase: checksummed
    assert _pay(env).status_code == 200
    env.stub.payer = CHECKSUMMED.lower()            # second: lowercase
    assert _pay(env).status_code == 403
    env.stub.payer = CHECKSUMMED.upper().replace("0X", "0x")
    assert _pay(env).status_code == 403
    assert env.stub.settle_calls == 1


def test_a_different_wallet_still_gets_its_own_trial(env):
    assert _pay(env).status_code == 200
    env.stub.payer = "0x" + "b" * 40
    assert _pay(env).status_code == 200
    assert env.stub.settle_calls == 2


def test_pre_existing_workspace_indexed_under_the_old_raw_case_key_is_found(env):
    """A workspace minted before normalization has its by_wallet index keyed by
    the address exactly as settlement returned it. It must still count."""
    we = env.workspace_engine
    wid = "ws_legacy_entry"
    we._write_workspace({"workspace_id": wid, "wallet_address": CHECKSUMMED,
                         "plan": "pro", "agent_cap": None, "pro_until": None})
    (we._index_dir("by_wallet") / we._hash(CHECKSUMMED)).write_text(wid)  # OLD key

    assert we.wallet_has_used_trial(CHECKSUMMED.lower()) is True
    env.stub.payer = CHECKSUMMED.lower()
    assert _pay(env).status_code == 403
    assert env.stub.settle_calls == 0


def test_mpp_rail_gets_the_same_typed_refusal(env, monkeypatch):
    """MPP reaches settlement through verify_payment, so it raises the same
    TrialAlreadyUsed; the route must turn it into the typed 403, not fall
    through to a generic 'payment required' that invites a retry."""
    import mpp_verify

    def refuse(_auth):
        raise env.x402_verify.TrialAlreadyUsed(CHECKSUMMED)
    monkeypatch.setattr(mpp_verify, "verify_credential", refuse)
    r = env.client.post("/v1/billing/x402",
                        headers={"Authorization": "Payment credential=abc"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "x402_trial_already_used"


def test_raced_second_settlement_is_honoured_and_logged(env, monkeypatch, caplog):
    """Two purchases that both passed the pre-check before either workspace
    existed have BOTH settled. The second must be honoured (pass extended, key
    still valid), never refused after the money moved."""
    first = _pay(env)
    assert first.status_code == 200
    key = first.json()["workspace_key"]

    # Simulate the race: the pre-settlement check does not see the workspace.
    monkeypatch.setattr(env.workspace_engine, "wallet_has_used_trial",
                        lambda w: False)
    with caplog.at_level(logging.WARNING):
        second = _pay(env)
    assert second.status_code == 200
    assert second.json()["workspace_id"] == first.json()["workspace_id"]
    assert second.json()["workspace_key"] is None
    assert env.stub.settle_calls == 2
    assert env.workspace_engine.is_workspace_pro(first.json()["workspace_id"])
    assert env.workspace_engine.get_workspace_by_key(key) is not None
    assert any("trial race honoured" in m for m in caplog.messages)


def test_canary_without_the_check_a_repeat_purchase_settles(env, monkeypatch):
    """CANARY: proves the refusal tests above depend on the check. With the
    check neutered the same wallet pays twice and both settle — i.e. the
    pre-change behaviour the tests would catch."""
    monkeypatch.setattr(env.workspace_engine, "wallet_has_used_trial",
                        lambda w: False)
    assert _pay(env).status_code == 200
    assert _pay(env).status_code == 200
    assert env.stub.settle_calls == 2


def test_trial_grant_defaults_to_unlimited_agents(env):
    r = _pay(env)
    ws = env.workspace_engine.get_workspace(r.json()["workspace_id"])
    assert env.x402_verify.X402_TRIAL_GRANT == "unlimited"
    assert ws["plan"] == "pro" and ws["agent_cap"] is None


def test_trial_grant_starter_caps_the_trial_at_the_starter_limit(env, monkeypatch):
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
        assert x402_verify.x402_trial_tier() == "pro"
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
