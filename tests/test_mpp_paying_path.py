"""D-1319 — the MPP PAYING path (a VALID credential), which shipped broken.

WHY THIS FILE EXISTS
====================
The frame card's acceptance test was Stripe/Tempo's own validator
(`npx mppx@latest validate`). That tool exercises exactly one credential case:
MALFORMED. So the whole suite went green — 644 passed, validator 13/13 — while
the path that actually takes money was dead, because `verify_credential` called
the async `broadcast_credential` WITHOUT awaiting it. The only trace was a
RuntimeWarning:

    RuntimeWarning: coroutine 'Mpp.broadcast_credential' was never awaited

and the route's fall-through `except Exception: pass` turned the resulting
AttributeError into a plain 402. Net effect: a real MPP payment could never mint
a workspace, and every instrument we had said the feature was healthy.

The lesson is the fleet's recurring one: a validator that tests the failure case
does not test the success case. These tests cover the success case.

WHAT THESE TESTS PIN
====================
1. `verify_credential` AWAITS the SDK (no un-awaited coroutine, no RuntimeWarning).
2. A settled credential yields a Receipt with a usable `.reference` (the tx hash).
3. The route mints a workspace from an MPP-settled payment — the money path.
4. A credential the rail REFUSES is refused, not silently minted.
5. A non-Receipt return is rejected rather than treated as paid.
"""
import asyncio
import json
import warnings

import pytest
from fastapi.testclient import TestClient

import mpp_verify
import x402_verify


@pytest.fixture
def client():
    import api_server
    return TestClient(api_server.app)


SETTLED = {
    "verified": True,
    "payer_wallet": "0xMPPPAYER0000000000000000000000000000001",
    "tx_hash": "0xMPPTXSETTLED0000000000000000000000000000000000000000000000000001",
    "recipient": None,          # filled from config below
    "amount": None,
    "asset": None,
    "network": None,
    "settlement_headers": {},
}


class _FakeMppServer:
    """Stand-in for the real Mpp server.

    A bare `object()` cannot take attribute patches, and the real Mpp instance
    needs live credentials to construct — so tests substitute this.
    """

    async def broadcast_credential(self, credential, *, intent=None,
                                   request=None, **kw):  # pragma: no cover
        raise AssertionError(
            "each test must monkeypatch broadcast_credential")

    async def charge(self, *, authorization=None, amount=None, **kw):  # pragma: no cover
        raise AssertionError("each test must monkeypatch charge")


def _enable_mpp(monkeypatch):
    """Force MPP on for the module without needing real credentials."""
    monkeypatch.setattr(mpp_verify, "MPP_ENABLED", True)
    monkeypatch.setattr(mpp_verify, "mpp_server", _FakeMppServer())
    monkeypatch.setenv("MPP_SECRET_KEY", "test-secret")


def _settled_result(wallet=None):
    """A settled x402 result. Wallet is unique per call by default.

    A wallet maps to exactly ONE workspace, and the dev DB persists across
    runs — so a fixed wallet makes the second run resolve to an existing
    workspace and (correctly) refuse to re-show its key. Unique by default.
    """
    import uuid
    r = dict(SETTLED)
    r["payer_wallet"] = wallet or f"0xMPP{uuid.uuid4().hex[:38]}"
    cfg = mpp_verify._load_x402_config()
    r["recipient"] = x402_verify.X402_PAY_TO
    r["amount"] = str(x402_verify.x402_mint_price_atomic())
    r["asset"] = cfg["asset"]
    r["network"] = x402_verify.X402_NETWORK
    return r


# ── 1. the await bug itself ───────────────────────────────────────────────────

def test_verify_credential_awaits_the_sdk(monkeypatch):
    """The regression test for the shipped bug.

    `broadcast_credential` is a coroutine. If it is called without being awaited,
    this test fails: the return value is a coroutine, and `.reference` raises.
    A RuntimeWarning about an un-awaited coroutine is also a failure.
    """
    _enable_mpp(monkeypatch)
    from mpp import Receipt
    saw_await = {"awaited": False}

    async def fake_broadcast(credential, *, intent=None, request=None, **kw):
        # Only reachable if the caller awaited us.
        saw_await["awaited"] = True
        receipt = Receipt.success(reference="0xTX_AWAITED", method="x402-base",
                                  external_id="0xPAYER")
        mpp_verify._last_settlement_result["0xTX_AWAITED"] = _settled_result()
        return receipt

    monkeypatch.setattr(mpp_verify.mpp_server, "broadcast_credential",
                        fake_broadcast, raising=False)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = mpp_verify.verify_credential("Payment abc123")

    assert saw_await["awaited"], (
        "the SDK coroutine was never awaited — verify_credential regressed to the "
        "bug that made a real MPP payment impossible")
    unawaited = [w for w in caught
                 if "never awaited" in str(w.message)]
    assert not unawaited, f"un-awaited coroutine warning: {unawaited}"
    assert out["receipt"].reference == "0xTX_AWAITED"
    assert out["result"]["verified"] is True


def test_verify_credential_rejects_a_non_receipt(monkeypatch):
    """A Challenge (or anything without .reference) is NOT a settlement.

    Advertising success on a non-Receipt is how a paywall mints free workspaces.
    """
    _enable_mpp(monkeypatch)

    async def fake_broadcast(credential, **kw):
        from mpp import Challenge
        return Challenge(id="c1", realm="x", method="x402-base", intent="charge",
                         request={})

    monkeypatch.setattr(mpp_verify.mpp_server, "broadcast_credential",
                        fake_broadcast, raising=False)
    with pytest.raises(Exception):
        mpp_verify.verify_credential("Payment abc123")


def test_verify_credential_refuses_when_mpp_is_disabled(monkeypatch):
    """Fail closed: no MPP config means no verification, not a free pass."""
    monkeypatch.setattr(mpp_verify, "MPP_ENABLED", False)
    monkeypatch.setattr(mpp_verify, "mpp_server", None)
    with pytest.raises(Exception):
        mpp_verify.verify_credential("Payment abc123")


# ── 2. the money path: an MPP-settled payment mints a workspace ───────────────

def test_mpp_settled_payment_mints_a_workspace(client, monkeypatch):
    """End-to-end: a VALID MPP credential buys a workspace.

    This is the test the validator could not write. It drives the same route
    with an `Authorization: Payment ...` header and asserts a workspace comes
    back — i.e. money in, entitlement out.

    Uses a UNIQUE tx hash per run. The tx hash is the idempotency key, and a
    replayed hash correctly returns the workspace WITHOUT re-showing the key
    ("shown once at claim time"), so a fixed hash would make this test assert
    replay behaviour instead of first-payment behaviour.
    """
    import uuid
    _enable_mpp(monkeypatch)
    tx = f"0xMPPTX{uuid.uuid4().hex}"

    async def fake_broadcast(credential, *, intent=None, request=None, **kw):
        from mpp import Receipt
        settled = _settled_result()
        settled["tx_hash"] = tx
        mpp_verify._last_settlement_result[tx] = settled
        return Receipt.success(reference=tx, method="x402-base",
                               external_id=settled["payer_wallet"])

    monkeypatch.setattr(mpp_verify.mpp_server, "broadcast_credential",
                        fake_broadcast, raising=False)

    r = client.post("/v1/billing/x402",
                    headers={"Authorization": "Payment test-credential"})
    assert r.status_code == 200, (
        f"an MPP-settled payment did not mint a workspace: {r.status_code} "
        f"{r.text[:300]}")
    body = r.json()
    assert body.get("workspace_id"), f"no workspace_id in {body}"
    assert body.get("workspace_key"), (
        "first-time payer must receive a workspace_key (shown once)")


def test_mpp_settled_replay_does_not_re_expose_the_key(client, monkeypatch):
    """A replayed MPP settlement resolves the workspace but must NOT re-show
    the key — same contract the x402 path already holds.

    Pins BOTH the wallet and the tx hash so the second request is a genuine
    replay of the same payment rather than a new one.
    """
    import uuid
    _enable_mpp(monkeypatch)
    tx = f"0xMPPTX{uuid.uuid4().hex}"
    wallet = f"0xMPP{uuid.uuid4().hex[:38]}"

    async def fake_broadcast(credential, *, intent=None, request=None, **kw):
        from mpp import Receipt
        settled = _settled_result(wallet=wallet)
        settled["tx_hash"] = tx
        mpp_verify._last_settlement_result[tx] = settled
        return Receipt.success(reference=tx, method="x402-base",
                               external_id=wallet)

    monkeypatch.setattr(mpp_verify.mpp_server, "broadcast_credential",
                        fake_broadcast, raising=False)

    first = client.post("/v1/billing/x402",
                        headers={"Authorization": "Payment test-credential"})
    assert first.status_code == 200, first.text[:300]
    ws = first.json()["workspace_id"]
    replay = client.post("/v1/billing/x402",
                         headers={"Authorization": "Payment test-credential"})
    assert replay.status_code == 200
    rb = replay.json()
    assert rb.get("workspace_id") == ws, "replay minted a different workspace"
    assert not rb.get("workspace_key"), (
        "replay re-exposed the workspace_key — the key is shown once only")


def test_mpp_refused_payment_does_not_mint(client, monkeypatch):
    """A credential the rail refuses must NOT yield a workspace."""
    _enable_mpp(monkeypatch)

    async def fake_broadcast(credential, **kw):
        from mpp.errors import PaymentError
        raise PaymentError("insufficient funds")

    monkeypatch.setattr(mpp_verify.mpp_server, "broadcast_credential",
                        fake_broadcast, raising=False)

    r = client.post("/v1/billing/x402",
                    headers={"Authorization": "Payment bad-credential"})
    assert r.status_code in (402, 503), (
        f"a refused MPP payment returned {r.status_code}, expected 402/503")
    assert "workspace_id" not in r.text, "refused payment minted a workspace"


def test_an_mpp_authorization_header_never_falls_through_to_a_free_mint(
        client, monkeypatch):
    """If MPP verification raises unexpectedly, the route must not treat the
    request as an unpaid x402 request AND must not mint. Silent fall-through is
    what hid the await bug."""
    _enable_mpp(monkeypatch)

    async def fake_broadcast(credential, **kw):
        raise RuntimeError("simulated SDK explosion")

    monkeypatch.setattr(mpp_verify.mpp_server, "broadcast_credential",
                        fake_broadcast, raising=False)

    r = client.post("/v1/billing/x402",
                    headers={"Authorization": "Payment boom"})
    assert r.status_code != 200 or "workspace_id" not in r.text, (
        "an exploding MPP verification minted a workspace")
