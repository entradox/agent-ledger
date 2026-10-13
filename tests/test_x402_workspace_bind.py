# tests/test_x402_workspace_bind.py
"""D-1647: a settled x402 payment can bind to an EXISTING workspace instead of
minting a new wallet-bound one — the missing conversion path.

A free /start workspace that hits its 3-agent cap had no honest way to pay:
the pass always landed on a separate wallet-bound workspace and the free
workspace stayed capped. Now X-Workspace-Id + X-Workspace-Key headers on the
paid request name the upgrade target; the paying wallet binds to it and
renewals resolve there with no headers.

The invariants under test:

- a refused bind NEVER takes money (refusals happen before settlement);
- a settled payment ALWAYS produces a grant (a post-settlement bind conflict
  falls back to the wallet's own workspace rather than eating the payment);
- wallet↔workspace is 1:1 in both directions;
- binding upgrades in place — agents, cap and history survive.
"""
import base64
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40


class _StubResourceServer:
    """Verification always passes; every process_settlement call is counted
    (a call == USDC moved). `on_settle` is an optional hook the race test uses
    to mutate state between the route's pre-check and the real bind write."""

    def __init__(self, payer):
        self.payer = payer
        self.settle_calls = 0
        self.on_settle = None

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
        if self.on_settle:
            self.on_settle()
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
    stub = _StubResourceServer(payer=WALLET_A)
    monkeypatch.setattr(x402_verify, "X402_ENABLED", True)
    monkeypatch.setattr(x402_verify, "resource_server", stub)
    monkeypatch.setattr(x402_verify, "HTTPRequestContext",
                        lambda **kw: SimpleNamespace(**kw))
    from fastapi.testclient import TestClient
    yield SimpleNamespace(client=TestClient(api_server.app), stub=stub,
                          we=workspace_engine)
    shutil.rmtree(tmp, ignore_errors=True)


def _payment_header(wallet):
    """A DECODABLE signed header — the shape payer_from_payment_header reads
    so the pre-settlement conflict checks see this wallet before money moves."""
    inner = {"payload": {"authorization": {"from": wallet}}}
    return base64.urlsafe_b64encode(json.dumps(inner).encode()).decode()


def _pay(env, wallet=WALLET_A, bind_to=None, key=None):
    env.stub.payer = wallet
    headers = {"X-PAYMENT": _payment_header(wallet)}
    if bind_to or key:
        if bind_to:
            headers["X-Workspace-Id"] = bind_to
        if key:
            headers["X-Workspace-Key"] = key
    return env.client.post("/v1/billing/x402", headers=headers)


def _free_ws(env):
    """POST /start's shape: free tier, scarcity grant off, raw key returned."""
    return env.we.create_workspace(grant_scarcity=False)


# ── The happy path: bind upgrades in place ───────────────────────────────────

def test_bind_upgrades_a_free_workspace_in_place(env):
    ws_id, raw_key = _free_ws(env)
    assert env.we.get_workspace(ws_id)["plan"] == "free"
    assert env.we.get_workspace(ws_id)["agent_cap"] == 3

    r = _pay(env, bind_to=ws_id, key=raw_key)
    assert r.status_code == 200
    body = r.json()
    assert body["workspace_id"] == ws_id, (
        "a bound payment must upgrade the NAMED workspace, not mint another")
    assert body["workspace_key"] is None, "the key was already shown once"
    assert body["bound_wallet"] == WALLET_A

    rec = env.we.get_workspace(ws_id)
    assert rec["plan"] == "pro"
    assert rec["pro_period_source"] == "x402_pass"
    assert rec["agent_cap"] is None, "the 3-agent cap did not lift"
    assert rec["wallet_address"] == WALLET_A.lower()
    assert env.we.get_workspace_by_wallet(WALLET_A)["workspace_id"] == ws_id
    assert env.stub.settle_calls == 1


def test_renewal_after_bind_needs_no_headers(env):
    """The point of binding: future payments from this wallet resolve the
    bound workspace on wallet identity alone."""
    ws_id, raw_key = _free_ws(env)
    _pay(env, bind_to=ws_id, key=raw_key)

    time.sleep(0.01)
    r = _pay(env)  # no workspace headers
    assert r.status_code == 200
    assert r.json()["workspace_id"] == ws_id
    assert env.stub.settle_calls == 2


def test_resending_credentials_on_renewal_is_idempotent(env):
    ws_id, raw_key = _free_ws(env)
    _pay(env, bind_to=ws_id, key=raw_key)
    r = _pay(env, bind_to=ws_id, key=raw_key)  # same pair again
    assert r.status_code == 200
    assert r.json()["workspace_id"] == ws_id
    assert env.stub.settle_calls == 2


def test_bind_preserves_agents_already_claimed(env):
    """In place means in place: the workspace's agents survive the upgrade —
    nothing migrates, nothing re-keys."""
    ws_id, raw_key = _free_ws(env)
    import ledger_engine
    for aid in ("bind-a", "bind-b"):
        secret, created = ledger_engine.ensure_agent_secret(
            aid, workspace_key=raw_key)
        assert secret and created
    r = _pay(env, bind_to=ws_id, key=raw_key)
    assert r.status_code == 200
    import identity
    for aid in ("bind-a", "bind-b"):
        assert identity.workspace_of_agent(aid) == ws_id, (
            "the agent was orphaned or re-homed by the bind")


# ── Refusals happen before settlement ───────────────────────────────────────

def test_bad_workspace_key_refused_without_settlement(env):
    ws_id, _ = _free_ws(env)
    r = _pay(env, bind_to=ws_id, key="wk_live_wrong")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "workspace_key_mismatch"
    assert env.stub.settle_calls == 0, "a refused bind must never take money"


def test_one_header_alone_refused_without_settlement(env):
    ws_id, raw_key = _free_ws(env)
    r1 = _pay(env, bind_to=ws_id)          # id without key
    r2 = _pay(env, key=raw_key)            # key without id
    for r in (r1, r2):
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "bind_headers_incomplete"
    assert env.stub.settle_calls == 0


def test_wallet_bound_elsewhere_refused_without_settlement(env):
    """Wallet bound to ws_A cannot claim ws_B — wallet↔workspace is 1:1."""
    ws_b, key_b = _free_ws(env)
    r1 = _pay(env)                          # wallet A mints/binds its own ws
    assert r1.status_code == 200
    ws_a = r1.json()["workspace_id"]

    r2 = _pay(env, bind_to=ws_b, key=key_b)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "wallet_already_bound"
    # The payer is decoded from a signed-but-not-yet-verified header, so
    # pre-settlement anyone can claim to be any wallet. Naming the bound
    # workspace_id here would be a free wallet→workspace de-anonymization
    # oracle — the refusal must name no workspace at all.
    assert ws_a not in r2.json()["error"]["message"]
    assert ws_b not in r2.json()["error"]["message"]
    assert env.stub.settle_calls == 1, "the refused bind settled money anyway"
    assert env.we.get_workspace(ws_b)["wallet_address"] is None


def test_workspace_bound_to_other_wallet_refused_without_settlement(env):
    """The reverse direction: a workspace already bound to wallet A cannot be
    claimed by wallet B even with the workspace's own key."""
    ws_id, raw_key = _free_ws(env)
    r1 = _pay(env, wallet=WALLET_A, bind_to=ws_id, key=raw_key)
    assert r1.status_code == 200

    r2 = _pay(env, wallet=WALLET_B, bind_to=ws_id, key=raw_key)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "wallet_conflict"
    assert env.stub.settle_calls == 1, "wallet B's payment was never attempted"
    # Wallet B is untouched — no workspace minted for it either.
    assert env.we.get_workspace_by_wallet(WALLET_B) is None


def test_mpp_rail_bad_bind_headers_refuse_before_settlement(env, monkeypatch):
    """Regression (Morgan F1): the MPP credential verifies — i.e. SETTLES —
    inside verify_credential. If the bind checks ran after that call, a bad
    bind request on the MPP rail would take the money AND 4xx. The
    credential-independent refusals must precede ALL settlement."""
    import mpp_verify
    settled = []
    def fake_verify(auth):
        settled.append(auth)
        return {"result": {"verified": True, "payer_wallet": WALLET_A,
                           "tx_hash": "0xMPP1", "recipient": "0xTreasury"}}
    monkeypatch.setattr(mpp_verify, "verify_credential", fake_verify)

    ws_id, raw_key = _free_ws(env)
    auth = {"Authorization": "Payment realm-test"}
    r1 = env.client.post("/v1/billing/x402",
                         headers={**auth, "X-Workspace-Id": ws_id})
    r2 = env.client.post("/v1/billing/x402",
                         headers={**auth, "X-Workspace-Id": ws_id,
                                  "X-Workspace-Key": "wk_live_wrong"})
    assert r1.status_code == 400 and r2.status_code == 401
    assert settled == [], (
        "an MPP credential was verified (settlement attempted) before the "
        "bind headers were refused — money moved and nothing was granted")


# ── Money is never eaten ─────────────────────────────────────────────────────

def test_post_settlement_bind_race_falls_back_to_wallet_workspace(env):
    """The pre-settlement checks can race a concurrent bind. If the state has
    changed by write time, the wallet's OWN workspace still receives the pass
    — a settled payment is always honoured — and the caller sees why."""
    ws_b, key_b = _free_ws(env)
    ws_a, _ = _free_ws(env)
    # Simulate the race: between the pre-check and the real bind write (both
    # inside one request), the wallet got bound to ws_a.
    def steal():
        env.we.bind_wallet_to_workspace(ws_a, WALLET_A)
    env.stub.on_settle = steal

    r = _pay(env, wallet=WALLET_A, bind_to=ws_b, key=key_b)
    assert r.status_code == 200, "a settled payment must always be honoured"
    body = r.json()
    assert body["workspace_id"] == ws_a, (
        "the fallback must land the pass on the wallet's real workspace")
    assert body["bind_error"], "the caller was not told the bind was refused"
    rec = env.we.get_workspace(ws_a)
    assert rec["pro_period_source"] == "x402_pass", (
        "money settled but no pass was granted anywhere")
    assert env.stub.settle_calls == 1


def test_ambiguous_settlement_failure_is_never_a_retryable_answer(env):
    """Regression (Morgan F2): process_settlement raising means the payment
    reached the rail and may have moved — a 402/503 would invite a second
    payment on top of the first. The route must answer non-retryable."""
    def boom(payload, reqs, ctx):
        raise RuntimeError("facilitator timeout")
    env.stub.process_settlement = boom
    r = _pay(env)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "x402_settlement_outcome_unknown"
    assert "do NOT retry" in r.json()["error"]["message"]


# ── Entitlement protection on the bound target ──────────────────────────────

def test_bound_workspace_with_live_subscription_is_not_downgraded(env):
    """A bound workspace holding a live non-pass entitlement keeps it — the
    $0.01 pass is honoured but never shrinks a bigger entitlement."""
    ws_id, raw_key = _free_ws(env)
    env.we.mark_pro(ws_id, "cus_live", stripe_subscription_id="sub_1",
                    pro_until=None, period_source="checkout.session.completed",
                    tier="team")
    r = _pay(env, bind_to=ws_id, key=raw_key)
    assert r.status_code == 200
    rec = env.we.get_workspace(ws_id)
    assert rec["plan"] == "team"
    assert rec["wallet_address"] == WALLET_A.lower(), (
        "the wallet still binds — only the plan is preserved")


def test_bound_free_workspace_gets_the_pass_it_paid_for(env):
    """Lapsed-entitlement symmetry: a bound free workspace must receive the
    pass — never 'payment honoured, nothing granted'."""
    ws_id, raw_key = _free_ws(env)
    r = _pay(env, bind_to=ws_id, key=raw_key)
    assert r.status_code == 200
    rec = env.we.get_workspace(ws_id)
    assert rec["plan"] == "pro" and rec["pro_until"] > time.time()
