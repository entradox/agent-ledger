#!/usr/bin/env python3
"""End-to-end: can a real buyer actually pay and get Pro?

Simulates the full money-in chain against the real app, using signed webhook
payloads exactly as Stripe sends them, then reads back the entitlement.

This answers the operational question directly instead of by code reading:
    "agent/person pays -> does Pro actually get granted -> and what happens
     afterwards when they renew, cancel, or their card fails?"

Run: /opt/miniconda3/bin/python3 tests/test_subscription_lifecycle.py
"""
import hashlib
import hmac
import importlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
SECRET = "whsec_lifecycle"


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET_AL", SECRET)
    import ledger_engine, workspace_engine, identity, routes_billing, api_server
    for m in (workspace_engine, ledger_engine, identity, routes_billing, api_server):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    yield TestClient(api_server.app), workspace_engine, tmp
    shutil.rmtree(tmp, ignore_errors=True)


def post(c, payload):
    body = json.dumps(payload).encode()
    t = str(int(time.time()))
    sig = f"t={t},v1=" + hmac.new(SECRET.encode(),
                                  f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    r = c.post("/stripe/webhook", content=body,
               headers={"stripe-signature": sig})
    return r.status_code, r.json()


def paid_session(ws, sid="cs_sub_1", amount=1900, status="paid"):
    return {"type": "checkout.session.completed", "data": {"object": {
        "id": sid, "customer": "cus_real", "amount_total": amount,
        "payment_status": status, "client_reference_id": ws,
        "customer_details": {"email": "realbuyer@example.com"}}},
        "subscription": "sub_real_123"}


def test_a_real_payer_gets_pro(env):
    """THE core question. A $19 payer must end up Pro."""
    c, ws, tmp = env
    wid, _ = ws.create_workspace(owner_email="realbuyer@example.com",
                                 grant_scarcity=False)
    assert ws.is_workspace_pro(wid) is False, "precondition: not Pro before paying"

    code, body = post(c, paid_session(wid))
    assert code == 200, f"webhook rejected a real payment: {body}"
    assert body.get("granted") is True, f"PAYER NOT GRANTED PRO: {body}"
    assert ws.is_workspace_pro(wid) is True, "not Pro after paying"

    rec = ws.get_workspace(wid)
    assert rec["plan"] == "pro"
    assert rec["stripe_customer_id"] == "cus_real"
    print(f"\n  PASS: $19 payer -> Pro. pro_until={rec.get('pro_until')!r}")


def test_pro_never_expires_after_first_payment(env):
    """Documents CURRENT behaviour: one payment = Pro indefinitely.

    mark_pro() sets pro_until=None, and is_workspace_pro() reads None as
    "subscription, never expires". So the first successful payment grants
    permanent Pro. There is no renewal clock.
    """
    c, ws, tmp = env
    wid, _ = ws.create_workspace(owner_email="buyer2@example.com", grant_scarcity=False)
    post(c, paid_session(wid, sid="cs_sub_2"))
    rec = ws.get_workspace(wid)
    assert rec["pro_until"] is None, (
        "pro_until is not None - a renewal clock now exists, update this test")
    # far future
    assert ws.is_workspace_pro(wid) is True
    print("\n  CURRENT: Pro is permanent after one payment (no renewal clock)")


def test_cancellation_is_ignored_so_a_canceller_keeps_pro(env):
    """REVENUE LEAK: Stripe sends customer.subscription.deleted on cancel.

    The webhook has no branch for any non-`checkout.session.*` event - they all
    fall through to `return {"received": True, "ignored": event_type}`. So a
    customer who cancels keeps Pro forever.
    """
    c, ws, tmp = env
    wid, _ = ws.create_workspace(owner_email="canceller@example.com", grant_scarcity=False)
    post(c, paid_session(wid, sid="cs_sub_3"))
    assert ws.is_workspace_pro(wid) is True

    code, body = post(c, {"type": "customer.subscription.deleted",
                          "data": {"object": {"id": "sub_real_123",
                                              "customer": "cus_real",
                                              "status": "canceled"}}})
    assert code == 200
    still_pro = ws.is_workspace_pro(wid)
    print(f"\n  cancellation response: {body}")
    print(f"  still Pro after cancelling: {still_pro}")
    assert body.get("ignored") == "customer.subscription.deleted", (
        "cancellation is now handled - this test documents the old gap; "
        "update it to assert Pro is revoked")
    assert still_pro is True, (
        "PRO REVOKED on cancel, which contradicts the ignored-event path")


def test_failed_renewal_is_ignored_so_non_payers_keep_pro(env):
    """REVENUE LEAK: invoice.payment_failed is ignored.

    A subscriber whose card fails keeps Pro forever - we keep serving them for
    free. Same root cause: no subscription lifecycle handling.
    """
    c, ws, tmp = env
    wid, _ = ws.create_workspace(owner_email="lapsed@example.com", grant_scarcity=False)
    post(c, paid_session(wid, sid="cs_sub_4"))

    code, body = post(c, {"type": "invoice.payment_failed",
                          "data": {"object": {"id": "in_1",
                                              "customer": "cus_real",
                                              "amount_due": 1900,
                                              "attempt_count": 1}}})
    assert code == 200
    print(f"\n  payment_failed response: {body}")
    print(f"  still Pro after failed renewal: {ws.is_workspace_pro(wid)}")
    assert body.get("ignored") == "invoice.payment_failed"


def test_subscription_renewal_invoice_is_ignored(env):
    """invoice.paid (a RENEWAL) is ignored too. Because Pro is already
    permanent this is harmless today, but it means we never record recurring
    revenue: every month after the first is invisible in our own metrics."""
    c, ws, tmp = env
    wid, _ = ws.create_workspace(owner_email="renewer@example.com", grant_scarcity=False)
    post(c, paid_session(wid, sid="cs_sub_5"))

    code, body = post(c, {"type": "invoice.paid",
                          "data": {"object": {"id": "in_2",
                                              "customer": "cus_real",
                                              "amount_paid": 1900,
                                              "billing_reason": "subscription_cycle"}}})
    assert code == 200
    assert body.get("ignored") == "invoice.paid", (
        "invoice.paid is now handled - renewals are recorded; update this test")

    # And the durable money record only ever saw the first payment. Metrics may
    # be in-memory only in this harness, in which case the point stands a
    # fortiori: the renewal produced no record anywhere.
    metrics = Path(tmp) / "metrics.jsonl"
    if metrics.exists():
        rev = [json.loads(l) for l in metrics.read_text().splitlines()
               if '"kind": "checkout_completed"' in l]
        print(f"\n  revenue events recorded: {len(rev)} (renewals are invisible)")
        assert len(rev) <= 1, "renewal revenue is now recorded - update this test"
    else:
        print("\n  no durable metrics file in this harness; renewal recorded nowhere")


def test_non_1900_amount_does_not_grant_pro(env):
    """A $1 payment must not buy Pro."""
    c, ws, tmp = env
    wid, _ = ws.create_workspace(owner_email="cheapskate@example.com",
                                 grant_scarcity=False)
    before = ws.is_workspace_pro(wid)
    code, body = post(c, paid_session(wid, sid="cs_wrong_amount", amount=100))
    assert code == 200
    assert not (body.get("granted") is True and before is False), (
        f"$1 bought Pro: {body}")
    print(f"\n  $1 payment response: {body}")
