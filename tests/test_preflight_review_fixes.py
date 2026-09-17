"""D-1319 pre-deploy review fixes — defects found by an independent risk review.

WHY THIS FILE EXISTS
====================
Two adversarial reviews and a Stripe/Tempo validator (13/13 green) all passed
over the defects below. Each test pins one, and each was proven load-bearing by
reverting the fix and watching it go RED.

WHAT WAS WRONG
--------------
A. THE MPP PAYING PATH COULD NEVER RUN AT ALL (critical, found while writing
   the guard for B). X402BaseChargeIntent passed the x402 SDK's own
   X402HTTPRequestContext into x402_verify.verify_payment(), which reads
   `.url.path`, `.method`, `.headers` off its argument. X402HTTPRequestContext
   exposes only `payment_header` and `route_pattern`, so every real credential
   raised "'HTTPRequestContext' object has no attribute 'url'" BEFORE settlement
   was attempted — and the route's bare `except Exception: pass` turned that
   crash into a retryable 402. The rail was unreachable, and its own tests could
   not see it because none of them drove the real intent (they monkeypatch the
   server and hand-build the credential).

B. SETTLE-THEN-FAIL WAS INDISTINGUISHABLE FROM UNPAID (money risk). A failure
   after the payment entered the settlement rail fell through to the x402 branch,
   which answered "retry with a payment header" — telling an agent that HAD
   already paid to pay again. The settled tx_hash was never recorded, so the
   retry was not even deduplicated. Now: a raise from inside settlement is
   classified as ambiguous and gets a NON-retryable 409 quoting a reference;
   a definitive refusal (`settle.success == False`) stays a retryable 402/503.

C. THE CREDENTIAL CONTRACT WAS UNPUBLISHED. The intent requires the signed x402
   payment under payload["x402_header"] — a field this service invented, carried
   nowhere a payer could read. No off-the-shelf MPP client could construct an
   acceptable credential. It is now advertised in the challenge's `extra`.

D. SERVED SURFACES ADVERTISED MPP UNCONDITIONALLY. "accepts both x402 and MPP"
   was frozen at import, so it was served even when MPP_ENABLED is False and no
   WWW-Authenticate challenge is emitted. Now derived per request. And /agents.txt
   advertised /skill.md as "(x402 + MPP, markdown)" when the buyer skill contains
   zero MPP content.

The common shape: the money path and the discovery surface drifted apart, and
nothing compared them. Each test below compares the two.
"""
import base64
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

REPO = Path(__file__).resolve().parent.parent
PAY_TO = "0x363c520492EDbA89057bCe696B74263B3295a72A"


@pytest.fixture(autouse=True)
def data_dir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="agent-ledger-preflight-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    yield
    shutil.rmtree(tmp, ignore_errors=True)


def _enable(monkeypatch, settlement):
    """Configure MPP enabled and drive the REAL intent through `settlement`."""
    monkeypatch.setenv("MPP_SECRET_KEY", "preflight-secret")
    monkeypatch.setenv("X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("X402_NETWORK", "eip155:84532")
    for mod in ("x402_verify", "mpp_verify", "routes_billing", "api_server"):
        sys.modules.pop(mod, None)

    import x402_verify
    import mpp_verify

    x402_verify.X402_ENABLED = True
    x402_verify.resource_server = settlement
    mpp_verify.MPP_ENABLED = True

    class _Server:
        async def broadcast_credential(self, auth, **kw):
            # The REAL intent — this is the line the old tests replaced.
            return await mpp_verify.X402BaseChargeIntent().verify(
                type("C", (), {"payload": {"x402_header": "SIGNED-X402"}})(), {})

    mpp_verify.mpp_server = _Server()
    return x402_verify, mpp_verify


class _OkSettlement:
    """Settles successfully; records what the context looked like."""

    def __init__(self):
        self.seen = {}

    def process_http_request(self, ctx):
        self.seen["adapter"] = type(ctx.adapter).__name__
        self.seen["header"] = ctx.adapter.get_header("payment-signature")

        class O:
            pass

        o = O()
        o.response = None
        o.payment_payload = "P"
        o.payment_requirements = "R"
        return o

    def process_settlement(self, *a, **k):
        self.seen["settled"] = True

        class S:
            pass

        s = S()
        s.success = True
        s.payer = "0xPAYER"
        s.transaction = "0xTX"
        s.network = "eip155:84532"
        s.headers = {}
        return s


class _DeclineSettlement(_OkSettlement):
    """Settlement runs and definitively declines — nothing moved."""

    def process_settlement(self, *a, **k):
        class S:
            pass

        s = S()
        s.success = False
        s.error_reason = "insufficient funds"
        s.response = None
        return s


class _CrashInSettlement(_OkSettlement):
    """Settlement is entered, then blows up — funds may have moved."""

    def process_settlement(self, *a, **k):
        raise RuntimeError("facilitator timed out AFTER broadcast")


# ── A. the MPP paying path must actually reach settlement ─────────────────────

def test_the_real_mpp_intent_reaches_settlement(monkeypatch):
    """The credential path must get as far as the settlement call.

    RED before the fix: verify_payment() was handed X402HTTPRequestContext,
    which has no `.url`/`.headers`, so it raised AttributeError at the very
    first line of verification — before settlement was ever attempted. The
    rail could not take money at all. No existing test caught it because every
    paying-path test replaced the intent instead of driving it.
    """
    settlement = _OkSettlement()
    _enable(monkeypatch, settlement)

    import mpp_verify
    out = mpp_verify.verify_credential("Payment abc123")

    assert settlement.seen.get("settled") is True, (
        "the real MPP intent never reached settlement — the paying path is "
        "unreachable, which is the defect this test exists to prevent")
    assert settlement.seen.get("header") == "SIGNED-X402", (
        "the credential's x402 envelope was not forwarded to the settlement rail")
    assert out["receipt"].reference == "0xTX"


# ── B. settle-then-fail must never invite a retry ─────────────────────────────

def test_a_failure_after_settlement_is_not_a_retryable_402(monkeypatch):
    """MONEY: an ambiguous settlement must not tell a paid agent to pay again.

    RED before the fix: the exception was swallowed, the route fell through to
    the x402 branch, and the caller got a 402 saying "retry with a payment
    header" — a double charge, with no dedup because the tx_hash was never
    recorded.
    """
    from fastapi.testclient import TestClient

    _enable(monkeypatch, _CrashInSettlement())
    import api_server

    c = TestClient(api_server.app, raise_server_exceptions=False)
    r = c.post("/v1/billing/x402", headers={"Authorization": "Payment abc123"})

    assert r.status_code != 402, (
        "a settlement that may have completed was reported as a plain unpaid "
        "402, inviting the customer to pay twice — this is the double-charge "
        "bug. Got 402.")
    assert r.status_code == 409, (
        f"expected a non-retryable 409 for an ambiguous settlement, got "
        f"{r.status_code}")
    assert "workspace_key" not in r.text, "minted without a confirmed settlement"
    body = r.text.lower()
    assert "not retry" in body or "do not retry" in body, (
        "the error must tell the agent not to retry, or a well-behaved client "
        "will pay again")


def test_a_definitive_refusal_is_still_safely_retryable(monkeypatch):
    """DIRECTION 2: the ambiguity fix must not block honest retries.

    A settlement that runs and DECLINES moved no money, so the agent must get
    the ordinary retryable refusal. If the fix over-triggered here, every
    underfunded agent would be stranded on a 409 with no way to pay — a worse
    outcome than the bug it fixes.
    """
    from fastapi.testclient import TestClient

    _enable(monkeypatch, _DeclineSettlement())
    import api_server

    c = TestClient(api_server.app, raise_server_exceptions=False)
    r = c.post("/v1/billing/x402", headers={"Authorization": "Payment abc123"})

    assert r.status_code != 409, (
        "a settlement that definitively declined was reported as ambiguous — "
        "honest retries would be permanently blocked")
    assert r.status_code in (402, 503), f"got {r.status_code}"
    assert "workspace_key" not in r.text


# ── C. the credential contract must be advertised to payers ───────────────────

def test_the_challenge_publishes_the_credential_payload_contract(monkeypatch):
    """A payer must be told the payload shape, or no generic client can pay.

    RED before the fix: the challenge carried only amount/currency/recipient,
    while the server required payload["x402_header"] — an invented field
    documented nowhere a client could read it.
    """
    _enable(monkeypatch, _OkSettlement())
    import mpp_verify

    # Use the REAL Mpp server for the challenge (built by mpp_verify._init()
    # from the env above), so this asserts what a payer would actually receive
    # rather than what a stub would emit.
    mpp_verify._init()
    assert mpp_verify.MPP_ENABLED is True, (
        f"precondition: MPP must be enabled here; got "
        f"{mpp_verify.MPP_DISABLED_REASON!r}")
    challenge = mpp_verify.build_challenge(None, realm="test.example")
    assert challenge is not None

    # Read what a real payer receives: decode the advertised challenge request.
    header = challenge.to_www_authenticate("test.example")
    b64 = header.split('request="')[1].split('"')[0]
    request = json.loads(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)))

    blob = json.dumps(request).lower()
    assert "x402_header" in blob, (
        f"the advertised challenge does not mention the required "
        f"credential_payload field x402_header, so an off-the-shelf MPP client "
        f"cannot construct an acceptable credential. Advertised: {request}")


# ── D. served surfaces must match what the deployment can actually do ─────────

def test_llms_txt_does_not_advertise_mpp_when_it_is_disabled(monkeypatch):
    """A served doc must not advertise a rail the deployment cannot accept.

    RED before the fix: the "accepts both x402 and MPP" sentence was frozen at
    import, so it was served even with MPP_ENABLED False and no challenge
    emitted — advertising a payment method that does not exist.
    """
    monkeypatch.delenv("MPP_SECRET_KEY", raising=False)
    monkeypatch.delenv("X402_PAY_TO", raising=False)
    for mod in ("mpp_verify", "x402_verify", "api_server"):
        sys.modules.pop(mod, None)

    import mpp_verify
    import api_server

    assert mpp_verify.MPP_ENABLED is False, "precondition: MPP must be off here"
    served = api_server.llms_txt()
    assert "accepts both x402" not in served, (
        f"llms.txt advertises MPP while MPP_ENABLED is False: "
        f"{[l for l in served.splitlines() if 'MPP' in l][:3]}")
    assert "MPP is NOT enabled" in served, (
        "silence about MPP is not honesty either — say that it is off")


def test_agents_txt_does_not_over_advertise_the_buyer_skill(monkeypatch):
    """/agents.txt must not claim the buyer skill covers MPP.

    RED before the fix: it said "(x402 + MPP, markdown)" while the served skill
    contains zero MPP content, so an MPP-wired agent following the pointer finds
    nothing about MPP.
    """
    monkeypatch.setenv("MPP_SECRET_KEY", "preflight-secret")
    monkeypatch.setenv("X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("X402_NETWORK", "eip155:84532")
    for mod in ("mpp_verify", "x402_verify", "api_server"):
        sys.modules.pop(mod, None)

    import api_server

    skill_body = (REPO / "skill" / "agent-ledger-buyer.md").read_text().lower()
    text = api_server.agents_txt()
    line = [l for l in text.splitlines() if "/skill.md" in l][0]

    has_mpp_in_skill = "mpp" in skill_body
    claims_mpp_in_skill = "mpp" in line.lower()
    assert has_mpp_in_skill or not claims_mpp_in_skill, (
        f"/agents.txt claims the buyer skill covers MPP ({line.strip()!r}) but "
        f"the served skill contains no MPP content")
