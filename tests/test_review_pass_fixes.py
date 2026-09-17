"""D-1319 review-pass fixes — defects found by two independent adversarial reviews.

WHY THIS FILE EXISTS
====================
The frame card's own acceptance test (Stripe/Tempo's `npx mppx@latest validate`)
went 13/13 green over three separate real defects. A second, independent review
pass then found three MORE that nothing in the suite could see. Each test here
pins one of those, and each was proven load-bearing by reverting the fix and
watching it go RED.

WHAT THE REVIEWS FOUND THAT THE SUITE COULD NOT
-----------------------------------------------
A. The PUBLIC, UNAUTHENTICATED challenge path was not exception-safe.
   `mpp_verify.build_challenge` was called bare on the discovery path, so any
   exception inside it propagated out of the route and an agent that asked what
   it owed got an unhandled 500 instead of a 402/503. A challenge is an
   ENHANCEMENT to the response, never a precondition for it.
B. The SDK's raw exception text was reflected to unauthenticated callers.
   `note = result.get("error") or ...` put the upstream error string straight
   into the public body — including absolute filesystem paths and internal
   module names. Reproduced with a synthesized reason containing a secret path.
   The refusal must still NAME THE CONFIGURED NETWORK (an unexplained
   non-payment surface is its own defect) — so the fix names the network from
   config and drops everything else.
C. /agents.txt claimed "MPP is LIVE" on a served discovery surface while the
   project's own state.md records that no MPP payment has ever settled funds.
   This project has shipped five stale-claim defects; this was the fifth.

Note that (A) and (B) are the same shape as the defects this project keeps
shipping: the money path was fine, and the DISCOVERY surface lied or broke.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def data_dir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="agent-ledger-review-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    yield
    shutil.rmtree(tmp, ignore_errors=True)


# ── A. the public challenge path must never 500 ───────────────────────────────

def _boot(client_env, monkeypatch):
    """Import a fresh api_server under the given env, with MPP configured."""
    monkeypatch.setenv("MPP_SECRET_KEY", "review-test-secret")
    monkeypatch.setenv("X402_PAY_TO", "0x363c520492EDbA89057bCe696B74263B3295a72A")
    monkeypatch.setenv("X402_NETWORK", "eip155:84532")
    for k, v in client_env.items():
        monkeypatch.setenv(k, v)
    for mod in ("mpp_verify", "x402_verify", "api_server"):
        sys.modules.pop(mod, None)
    import api_server
    return api_server


def test_an_exploding_mpp_challenge_does_not_500_the_discovery_path(monkeypatch):
    """A fault inside build_challenge must degrade, not crash the endpoint.

    RED before the fix: the exception propagated out of the route and the
    caller got an unhandled 500 (TestClient re-raises; uvicorn sends a bare
    500). The endpoint must stay payable — x402 still emits its
    PAYMENT-REQUIRED header even when the MPP challenge cannot be built.
    """
    from fastapi.testclient import TestClient
    api = _boot({}, monkeypatch)

    import mpp_verify
    monkeypatch.setattr(mpp_verify, "MPP_ENABLED", True)

    def exploding_challenge(request, realm=None):
        raise RuntimeError("simulated SDK explosion in build_challenge")

    monkeypatch.setattr(mpp_verify, "build_challenge", exploding_challenge)

    c = TestClient(api.app, raise_server_exceptions=False)
    r = c.post("/v1/billing/x402")
    assert r.status_code != 500, (
        "an exception in build_challenge turned the public discovery response "
        "into a 500 — the challenge is an enhancement, not a precondition")
    assert r.status_code in (402, 503), (
        f"expected a payable/refusal response, got {r.status_code}")


# ── B. internals must not leak; the network must still be named ───────────────

def test_a_refusal_never_reflects_internal_error_text(monkeypatch):
    """A raw SDK error string must not reach an unauthenticated caller.

    RED before the fix: the upstream error text was copied into the public body
    verbatim, so a reason carrying an absolute path was disclosed.
    """
    from fastapi.testclient import TestClient
    api = _boot({}, monkeypatch)

    import x402_verify
    leak = "/Users/someone/SECRET/internal/path.py"

    def leaking_verify(request):
        raise x402_verify.X402Unavailable(leak)

    monkeypatch.setattr(x402_verify, "verify_payment", leaking_verify)

    c = TestClient(api.app, raise_server_exceptions=False)
    r = c.post("/v1/billing/x402")
    body = r.text
    assert leak not in body, (
        "the upstream exception text was reflected into the public response — "
        "an unauthenticated caller can read our filesystem paths")
    assert "/Users/" not in body and "site-packages" not in body, (
        f"an internal path leaked into the public body: {body[:300]}")


def test_a_refusal_still_names_the_configured_network(monkeypatch):
    """Dropping the leak must not cost the network name.

    The refusal has to tell an agent WHICH network this deployment serves — an
    unexplained non-payment surface is its own defect (asserted for every
    surface by test_the_till_envelope_matches_the_configured_network). This is
    the direction-2 half of the leak fix: the behaviour that had to SURVIVE.
    """
    from fastapi.testclient import TestClient
    api = _boot({}, monkeypatch)

    import x402_verify
    monkeypatch.setattr(
        x402_verify, "verify_payment",
        lambda request: (_ for _ in ()).throw(
            x402_verify.X402Unavailable("X402_PAY_TO not configured")))

    c = TestClient(api.app, raise_server_exceptions=False)
    r = c.post("/v1/billing/x402")
    assert x402_verify.X402_NETWORK in r.text, (
        f"the refusal stopped naming the configured network "
        f"({x402_verify.X402_NETWORK}); body was: {r.text[:300]}")


# ── C. served surfaces must not over-promise MPP liveness ─────────────────────

def test_agents_txt_does_not_claim_mpp_is_live(monkeypatch):
    """No served surface may claim MPP is LIVE while no MPP payment has settled.

    state.md records plainly: "A real MPP round-trip that SETTLES funds is not
    done ... STILL NOT DONE: no real MPP payment has settled funds." A surface
    that says otherwise is the fifth stale-claim defect of this class.

    This asserts the PROPERTY (no unqualified liveness claim), not one sentence,
    so rewording cannot satisfy it: the surface must state that the rail is not
    yet exercised end-to-end.
    """
    api = _boot({}, monkeypatch)
    text = api.agents_txt()
    low = text.lower()
    assert "mpp is live" not in low, (
        "/agents.txt claims 'MPP is LIVE' while no MPP payment has ever settled "
        "funds — state.md criterion 4 is unmet")
    # The honest replacement must actually be present, not merely the false
    # claim deleted: silence about the rail's status is not honesty either.
    assert "not yet settled" in low or "not yet exercised" in low, (
        "/agents.txt no longer claims MPP is live, but it also does not say the "
        "rail is unexercised — say which is true")
