#!/usr/bin/env python3
"""End-to-end: can a real buyer pay, subscribe, renew, cancel, and lapse?

Exercises the full money-in chain against the real app with signed webhook
payloads exactly as Stripe sends them, then reads back the entitlement.

D-1269. These tests previously DOCUMENTED four defects (Pro never expires,
cancellation ignored, failed renewal ignored, renewals invisible). They now
ASSERT the corrected behaviour, per the principal's decision (2026-09-15):
cancellation is PERIOD-END, and a failed renewal gets a grace window.

Run: /opt/miniconda3/bin/python3 -m pytest tests/test_subscription_lifecycle.py -v
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

from routes_billing import RENEWAL_GRACE_SECONDS  # noqa: E402

M = 30 * 24 * 60 * 60  # ~one month


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
    r = c.post("/stripe/webhook", content=body, headers={"stripe-signature": sig})
    return r.status_code, r.json()


def signed_up(env, email="realbuyer@example.com", sid="cs_sub_1", customer="cus_real"):
    """Mint a workspace and run a real $19 checkout through the webhook."""
    c, ws, tmp = env
    wid, _ = ws.create_workspace(owner_email=email, grant_scarcity=False)
    code, body = post(c, {
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": sid, "customer": customer, "amount_total": 1900,
            "payment_status": "paid", "client_reference_id": wid,
            "subscription": "sub_" + sid,
            "customer_details": {"email": email}}},
    })
    assert code == 200, body
    assert body.get("granted") is True, f"PAYER NOT GRANTED PRO: {body}"
    return wid


# ── the core path ───────────────────────────────────────────────────────────

def test_a_real_payer_gets_pro(env):
    """THE question that matters most. A $19 payer must end up Pro."""
    _, ws, _ = env
    wid = signed_up(env)
    assert ws.is_workspace_pro(wid) is True, "not Pro after paying"
    rec = ws.get_workspace(wid)
    assert rec["plan"] == "pro"
    assert rec["stripe_customer_id"] == "cus_real"


def test_pro_now_has_a_real_expiry(env):
    """D-1269 core fix: one payment must NOT grant Pro forever."""
    _, ws, _ = env
    wid = signed_up(env)
    rec = ws.get_workspace(wid)
    assert rec["pro_until"] is not None, (
        "pro_until is None, which is_workspace_pro() reads as 'never expires' — "
        "this is the defect D-1269 fixed")
    assert rec["pro_until"] > time.time(), "expiry is already in the past"
    assert rec.get("stripe_subscription_id"), "subscription id not recorded"
    assert rec.get("pro_period_source") == "checkout.session.completed"


def test_lapsed_subscription_downgrades_once_period_passes(env):
    """Past the paid period the workspace is free — the whole point."""
    _, ws, _ = env
    wid = signed_up(env)
    assert ws.is_workspace_pro(wid) is True
    ws.set_pro_period(wid, time.time() - 1, source="test", reason="simulate lapse")
    assert ws.is_workspace_pro(wid) is False, "still Pro after the period ended"
    assert ws.effective_agent_cap(ws.get_workspace(wid)) == 3


# ── period-end cancellation (the principal's decision) ──────────────────────

def test_cancellation_keeps_pro_UNTIL_PERIOD_END_then_stops(env):
    """PERIOD-END: cancelling must not revoke access the customer paid for."""
    c, ws, _ = env
    wid = signed_up(env)
    period_end = int(time.time() + 20 * 24 * 60 * 60)  # 20 days of paid time left

    code, body = post(c, {"type": "customer.subscription.deleted",
                          "data": {"object": {"id": "sub_cs_sub_1",
                                              "customer": "cus_real",
                                              "status": "canceled",
                                              "current_period_end": period_end}}})
    assert code == 200, body
    assert body.get("workspace_id") == wid, f"could not resolve workspace: {body}"
    assert body.get("pro_until") == period_end

    # Honoured for the rest of the paid period.
    assert ws.is_workspace_pro(wid) is True, (
        "cancelling revoked Pro immediately, but the customer paid through "
        f"{period_end} — D-1269 requires period-end")
    rec = ws.get_workspace(wid)
    assert abs(rec["pro_until"] - period_end) < 1

    # And stops when the period actually ends.
    ws.set_pro_period(wid, time.time() - 1, source="test")
    assert ws.is_workspace_pro(wid) is False, "Pro outlived the paid period"


def test_cancellation_with_no_period_end_fails_OPEN(env):
    """A payload with no period end must not expire a paying customer."""
    c, ws, _ = env
    wid = signed_up(env)
    code, body = post(c, {"type": "customer.subscription.deleted",
                          "data": {"object": {"id": "sub_cs_sub_1",
                                              "customer": "cus_real",
                                              "status": "canceled"}}})
    assert code == 200
    assert ws.is_workspace_pro(wid) is True, (
        "a cancellation with no period end revoked Pro — must fail open so a "
        "paying customer is never cut off on a guess")


def test_cancellation_resolves_workspace_without_client_reference_id(env):
    """Real subscription events carry a CUSTOMER id, not a workspace id.

    Without the by_stripe_customer index a cancellation cannot be attributed and
    silently does nothing — which is how the defect hid in the first place.
    """
    c, ws, _ = env
    wid = signed_up(env)
    # No client_reference_id at all — exactly how Stripe sends it.
    code, body = post(c, {"type": "customer.subscription.deleted",
                          "data": {"object": {"id": "sub_x", "customer": "cus_real",
                                              "status": "canceled",
                                              "current_period_end":
                                                  int(time.time() + 5 * 24 * 3600)}}})
    assert code == 200
    assert body.get("workspace_id") == wid, (
        f"customer->workspace resolution failed: {body}")


# ── payment failure: grace, not instant cut-off ─────────────────────────────

def test_failed_renewal_GRANTS_GRACE_and_keeps_pro(env):
    c, ws, _ = env
    wid = signed_up(env)
    before = ws.get_workspace(wid)["pro_until"]

    code, body = post(c, {"type": "invoice.payment_failed",
                          "data": {"object": {"id": "in_fail", "customer": "cus_real",
                                              "amount_due": 1900, "attempt_count": 1}}})
    assert code == 200, body
    assert body.get("grace") is True, f"no grace granted: {body}"
    assert body.get("grace_days") == 7
    assert ws.is_workspace_pro(wid) is True, (
        "a declined card revoked Pro instantly — grace is required, the customer "
        "is actively trying to pay")

    after = ws.get_workspace(wid)["pro_until"]
    assert after > time.time(), "grace deadline is in the past"
    # Grace is `max(now + 7 days, the paid period the customer already has)`.
    # It must never SHORTEN a period already paid for: if someone is 20 days
    # into a paid month, a declined card must not cut them off on day 7. So the
    # floor is the grace window and the paid period can only extend it.
    assert after >= time.time() + RENEWAL_GRACE_SECONDS - 5, "grace shorter than 7 days"
    assert after >= before - 1, "grace cut the paid period short"
    if after > time.time() + RENEWAL_GRACE_SECONDS + 60:
        assert abs(after - before) < 1, (
            "grace exceeded 7 days but did not match the paid period — an "
            "unrelated deadline was invented")


def test_grace_expires_and_downgrades(env):
    """Grace must actually end, or the leak just moves."""
    _, ws, _ = env
    wid = signed_up(env)
    ws.set_pro_period(wid, time.time() - 1, source="test", reason="payment_failed grace")
    assert ws.is_workspace_pro(wid) is False, "still Pro after grace expired"


def test_recovered_payment_clears_grace_and_continues_pro(env):
    """Stripe retries; a successful retry must resume normal service."""
    c, ws, _ = env
    wid = signed_up(env)
    post(c, {"type": "invoice.payment_failed",
             "data": {"object": {"id": "in_f", "customer": "cus_real",
                                 "amount_due": 1900}}})
    assert ws.is_workspace_pro(wid) is True
    assert str(ws.get_workspace(wid).get("pro_period_reason") or "").startswith("payment_failed")

    new_period = int(time.time() + 30 * 24 * 3600)
    code, body = post(c, {"type": "invoice.paid",
                          "data": {"object": {"id": "in_r", "customer": "cus_real",
                                              "amount_paid": 1900,
                                              "billing_reason": "subscription_cycle",
                                              "current_period_end": new_period}}})
    assert code == 200, body
    assert ws.is_workspace_pro(wid) is True
    rec = ws.get_workspace(wid)
    assert abs(rec["pro_until"] - new_period) < 1, "renewal did not advance the period"
    assert not str(rec.get("pro_period_reason") or "").startswith("payment_failed"), (
        "grace marker not cleared after the customer paid — they would still "
        "read as lapsed")


# ── renewal revenue (months 2..N were invisible before) ─────────────────────

def test_renewal_is_recorded_as_revenue(env):
    c, ws, tmp = env
    wid = signed_up(env)
    code, body = post(c, {"type": "invoice.paid",
                          "data": {"object": {"id": "in_2", "customer": "cus_real",
                                              "amount_paid": 1900,
                                              "billing_reason": "subscription_cycle",
                                              "current_period_end":
                                                  int(time.time() + 30 * 24 * 3600)}}})
    assert code == 200
    assert body.get("renewal") is True, f"renewal not recognised: {body}"
    assert body.get("amount_paid") == 1900

    # The renewal must land in the money record, not just the response. Check
    # both the durable file and the in-process counters — in this harness the
    # event may only have reached memory, which still proves it was recorded.
    import metrics
    found = "subscription_renewed" in json.dumps(metrics.snapshot())
    for f in Path(tmp).rglob("*.jsonl"):
        if "subscription_renewed" in f.read_text():
            found = True
    assert found, "renewal revenue was not recorded anywhere"


def test_initial_invoice_is_NOT_double_counted_as_revenue(env):
    """billing_reason=subscription_create is the SAME money as checkout.

    Counting it again would double our revenue figure — a reporting bug that
    looks like growth.
    """
    c, ws, _ = env
    wid = signed_up(env)
    code, body = post(c, {"type": "invoice.paid",
                          "data": {"object": {"id": "in_1", "customer": "cus_real",
                                              "amount_paid": 1900,
                                              "billing_reason": "subscription_create",
                                              "current_period_end":
                                                  int(time.time() + 30 * 24 * 3600)}}})
    assert code == 200
    assert body.get("renewal") is False, (
        "the initial invoice was counted as a renewal — that double-counts the "
        "first payment")


# ── robustness ──────────────────────────────────────────────────────────────

def test_modern_period_location_on_items_is_read(env):
    """Stripe moved current_period_end onto subscription ITEMS.

    Reading only the old location yields 0, which would write a 1970 deadline
    and instantly downgrade a paying customer.
    """
    c, ws, _ = env
    wid = signed_up(env)
    period_end = int(time.time() + 25 * 24 * 3600)
    code, body = post(c, {"type": "customer.subscription.updated",
                          "data": {"object": {
                              "id": "sub_cs_sub_1", "customer": "cus_real",
                              "status": "active",
                              "items": {"data": [{"current_period_end": period_end}]}}}})
    assert code == 200, body
    assert abs(ws.get_workspace(wid)["pro_until"] - period_end) < 1, (
        "period end on items.data[] was not read — a paying customer would be "
        "downgraded immediately")


def test_replayed_webhook_is_idempotent(env):
    """Stripe retries. A retry must not change entitlement or double revenue."""
    c, ws, _ = env
    wid = signed_up(env)
    first = ws.get_workspace(wid)["pro_until"]
    for _ in range(3):
        code, body = post(c, {"type": "customer.subscription.deleted",
                              "data": {"object": {
                                  "id": "sub_cs_sub_1", "customer": "cus_real",
                                  "status": "canceled",
                                  "current_period_end": int(first)}}})
        assert code == 200
    assert abs(ws.get_workspace(wid)["pro_until"] - first) < 1, (
        "a replayed cancellation changed the period end")
    assert ws.is_workspace_pro(wid) is True


def test_unknown_event_is_ignored_not_errored(env):
    """An event we do not handle must 200, or Stripe retries it forever."""
    c, _, _ = env
    code, body = post(c, {"type": "payment_intent.created",
                          "data": {"object": {"id": "pi_1"}}})
    assert code == 200, "an unknown event type must not error"
    assert body.get("ignored") == "payment_intent.created"


def test_non_1900_amount_does_not_grant_pro(env):
    """A $1 payment must not buy Pro."""
    c, ws, _ = env
    wid, _ = ws.create_workspace(owner_email="cheap@example.com", grant_scarcity=False)
    code, body = post(c, {"type": "checkout.session.completed",
                          "data": {"object": {"id": "cs_cheap", "customer": "cus_c",
                                              "amount_total": 100,
                                              "payment_status": "paid",
                                              "client_reference_id": wid,
                                              "customer_details": {"email": "cheap@example.com"}}}})
    assert code == 200
    assert body.get("granted") is not True, "$1 bought Pro"
    assert ws.is_workspace_pro(wid) is False


def test_x402_wallet_path_unaffected(env):
    """The x402 mint must not be disturbed by the subscription changes."""
    _, ws, _ = env
    wid, _ = ws.create_workspace(wallet_address="0x" + "ab" * 20, grant_scarcity=True)
    assert ws.is_workspace_pro(wid) is True, "scarcity grant broken"
    assert ws.get_workspace(wid)["pro_until"] is not None, "grant lost its year"
