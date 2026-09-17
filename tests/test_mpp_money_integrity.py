"""D-1319 — money-integrity guard for the MPP branch on the x402 paywall.

WHY THIS FILE EXISTS
====================
The MPP branch originally SYNTHESIZED its own settlement result:

    result = mpp_receipt["result"]
    if not result.get("verified"):
        result = {"verified": True, "recipient": X402_PAY_TO, "amount": ...}

Two concrete attacks were REPRODUCED against that code and each minted a live
workspace + workspace_key with no payment behind it:

  1. A receipt whose stashed settlement was absent (`result == {}`) fell through
     the `not verified` branch and minted.
  2. A settlement whose recipient was NOT our pay_to — and whose amount was
     1 atomic unit — also minted, because the branch overwrote `recipient` with
     our own address, so the downstream recipient check passed BY CONSTRUCTION.

The fix: the MPP branch must carry the REAL settled values through unchanged and
refuse when there is no settled payment behind the receipt. These tests lock that
behaviour so the synthesis cannot come back.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

import mpp_verify
import x402_verify


class _FakeServer:
    def __init__(self, make):
        self._make = make

    async def broadcast_credential(self, credential, **kw):
        return self._make()

    async def charge(self, **kw):  # pragma: no cover
        raise AssertionError("unused")


@pytest.fixture
def client():
    import api_server
    return TestClient(api_server.app)


PAY_TO = "0x363c520492EDbA89057bCe696B74263B3295a72A"


def _enable(monkeypatch, make, pay_to=PAY_TO):
    """Turn MPP on with a receiving address.

    The receiving address is REQUIRED: the route fails closed (503) when it
    cannot determine its own pay_to, because an endpoint that does not know who
    it is cannot verify who was paid.

    `_load_x402_config` is patched rather than setenv'd: X402_PAY_TO is read at
    MODULE IMPORT time into `x402_verify.X402_PAY_TO`, so setting the env var
    after import has no effect.
    """
    monkeypatch.setattr(mpp_verify, "MPP_ENABLED", True)
    monkeypatch.setattr(mpp_verify, "mpp_server", _FakeServer(make))
    monkeypatch.setenv("MPP_SECRET_KEY", "test-secret")
    real = mpp_verify._load_x402_config()

    def _cfg():
        c = dict(real)
        c["pay_to"] = pay_to
        return c

    monkeypatch.setattr(mpp_verify, "_load_x402_config", _cfg)


def _receipt_only(ref, wallet):
    from mpp import Receipt
    return Receipt.success(reference=ref, method="x402-base", external_id=wallet)


def _settled(ref, wallet, recipient=None, amount=None):
    cfg = mpp_verify._load_x402_config()
    return {"verified": True,
            "payer_wallet": wallet,
            "tx_hash": ref,
            "recipient": recipient or PAY_TO,
            "amount": amount or str(x402_verify.x402_mint_price_atomic()),
            "asset": cfg["asset"],
            "network": x402_verify.X402_NETWORK,
            "settlement_headers": {}}


def test_receipt_with_no_settlement_backing_cannot_mint(client, monkeypatch):
    """ATTACK 1: a receipt with no stashed settlement. Must NOT mint."""
    ref = f"0xFORGED{uuid.uuid4().hex}"
    wallet = f"0xADV{uuid.uuid4().hex[:34]}"
    mpp_verify._last_settlement_result.pop(ref, None)
    _enable(monkeypatch, lambda: _receipt_only(ref, wallet))

    r = client.post("/v1/billing/x402",
                    headers={"Authorization": "Payment forged"})
    assert r.status_code == 402, (
        f"a receipt with no settlement behind it returned {r.status_code}")
    assert "workspace_id" not in r.text, "FORGED RECEIPT MINTED A WORKSPACE"
    assert "workspace_key" not in r.text, "FORGED RECEIPT ISSUED A KEY"


def test_settlement_to_a_foreign_recipient_cannot_mint(client, monkeypatch):
    """ATTACK 2: settled, but NOT to our address, and underpaid."""
    ref = f"0xWRONG{uuid.uuid4().hex}"
    wallet = f"0xADV{uuid.uuid4().hex[:34]}"
    mpp_verify._last_settlement_result[ref] = _settled(
        ref, wallet, recipient="0xATTACKER000000000000000000000000000000",
        amount="1")
    _enable(monkeypatch, lambda: _receipt_only(ref, wallet))

    r = client.post("/v1/billing/x402",
                    headers={"Authorization": "Payment underpaid"})
    assert r.status_code == 402, (
        f"a payment to a foreign recipient and 1 atomic unit returned "
        f"{r.status_code} — must be refused")
    assert "workspace_id" not in r.text, (
        "FOREIGN-RECIPIENT SETTLEMENT MINTED A WORKSPACE")


def test_an_unverified_settlement_cannot_mint(client, monkeypatch):
    """A stashed result explicitly marked unverified must be refused."""
    ref = f"0xUNVER{uuid.uuid4().hex}"
    wallet = f"0xADV{uuid.uuid4().hex[:34]}"
    s = _settled(ref, wallet)
    s["verified"] = False
    mpp_verify._last_settlement_result[ref] = s
    _enable(monkeypatch, lambda: _receipt_only(ref, wallet))

    r = client.post("/v1/billing/x402",
                    headers={"Authorization": "Payment unverified"})
    assert r.status_code == 402, f"unverified settlement returned {r.status_code}"
    assert "workspace_id" not in r.text


def test_a_genuine_settlement_still_mints(client, monkeypatch):
    """NEGATIVE CONTROL: the happy path must keep working.

    Without this, the guards above could be satisfied by refusing everything —
    which would break the product while looking secure.
    """
    ref = f"0xGOOD{uuid.uuid4().hex}"
    wallet = f"0xMPP{uuid.uuid4().hex[:38]}"
    mpp_verify._last_settlement_result[ref] = _settled(ref, wallet)
    _enable(monkeypatch, lambda: _receipt_only(ref, wallet))

    r = client.post("/v1/billing/x402",
                    headers={"Authorization": "Payment genuine"})
    assert r.status_code == 200, (
        f"a genuine MPP settlement was refused: {r.status_code} {r.text[:300]}")
    body = r.json()
    assert body.get("workspace_id"), f"no workspace minted: {body}"
    assert body.get("workspace_key"), "key missing on first mint"


def test_no_receiving_address_fails_closed(client, monkeypatch):
    """An endpoint that does not know its own pay_to must NOT mint.

    This is the shape the original code shipped: with no X402_PAY_TO in the
    environment the recipient check was skipped entirely, so a settlement to a
    foreign address minted a workspace. Fail closed instead.
    """
    ref = f"0xNOPAYTO{uuid.uuid4().hex}"
    wallet = f"0xADV{uuid.uuid4().hex[:34]}"
    mpp_verify._last_settlement_result[ref] = _settled(ref, wallet)
    _enable(monkeypatch, lambda: _receipt_only(ref, wallet), pay_to="")

    r = client.post("/v1/billing/x402",
                    headers={"Authorization": "Payment nopayto"})
    assert r.status_code == 503, (
        f"no receiving address configured returned {r.status_code}; must fail "
        "closed with 503 rather than mint")
    assert "workspace_id" not in r.text, (
        "endpoint minted a workspace while unable to verify who was paid")
