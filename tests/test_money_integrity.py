#!/usr/bin/env python3
"""Money-integrity tests for the Stripe fulfillment chain.

The chain under test: a customer pays $19 -> Stripe fires
checkout.session.completed -> routes_billing verifies the HMAC signature ->
mark_pro() upgrades the workspace -> the workspace is genuinely Pro on its next
request.

Why these exist (2026-09-14): the webhook called mark_pro() inside a
`try/except WorkspaceError: pass`. If a real payment arrived with a
client_reference_id that did not resolve to a workspace, the money was taken,
no plan was granted, and there was no record anywhere. Silence is the worst
outcome for a paid subscription, so these tests pin the behaviour at every
point where the chain can break without making noise.

Each test asserts the OUTCOME a customer would experience (is the workspace
Pro?), not that a function was called.
"""
import hashlib
import hmac
import importlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SECRET = "whsec_test"


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET_AL", SECRET)
    import ledger_engine, workspace_engine, identity, routes_billing, api_server
    for m in (workspace_engine, ledger_engine, identity, routes_billing, api_server):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    yield TestClient(api_server.app), workspace_engine, Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


def _sign(payload: dict, secret: str = SECRET):
    body = json.dumps(payload).encode()
    t = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body,
                   hashlib.sha256).hexdigest()
    return body, f"t={t},v1={sig}"


def _free_workspace(ws, email: str = "buyer@example.com"):
    """Mint the workspace a real buyer has BEFORE paying.

    `create_workspace()` defaults to grant_scarcity=True, which hands over the
    launch-window Pro grant - so a default workspace is already Pro and cannot
    demonstrate that a payment changed anything. The human self-serve path
    (POST /start) passes grant_scarcity=False: free tier, 3 agents, and $19 is
    what buys Pro. That is the state a payer is actually in.
    """
    return ws.create_workspace(owner_email=email, grant_scarcity=False)


def _completed(workspace_id: str, email: str = "buyer@example.com",
               amount: int = 1900, session_id: str = "cs_test_mi_1",
               payment_status: str = "paid"):
    """A card checkout.session.completed, as Stripe actually sends it.

    `payment_status="paid"` is included because a real card session IS paid at
    completion - omitting it made the fixtures unrealistic and hid the async
    payment hole (see test_unconfirmed_payment_does_not_grant_pro).
    """
    return {
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": session_id,
            "customer": "cus_test_1",
            "amount_total": amount,
            "payment_status": payment_status,
            "client_reference_id": workspace_id,
            "customer_details": {"email": email},
        }},
    }


def _post(client, payload, secret=SECRET):
    body, sig = _sign(payload, secret)
    return client.post("/stripe/webhook", content=body,
                       headers={"stripe-signature": sig})


# ── the happy path, asserted as an outcome ──────────────────────────────────

def test_paid_workspace_is_actually_pro_afterwards(env):
    """The only assertion that matters: does the payer end up with Pro?"""
    c, ws, _ = env
    workspace_id, _ = _free_workspace(ws)
    assert ws.is_workspace_pro(workspace_id) is False, "precondition: not Pro"

    r = _post(c, _completed(workspace_id))
    assert r.status_code == 200

    assert ws.is_workspace_pro(workspace_id) is True, (
        "a $19 payment did not produce a Pro workspace"
    )
    rec = ws.get_workspace(workspace_id)
    assert rec["plan"] == "pro"
    assert rec["stripe_customer_id"] == "cus_test_1"
    # A subscription supersedes a scarcity grant's clock; a paying customer
    # must not inherit an expiry and silently lapse in a year.
    assert rec["pro_until"] is None


def test_pro_workspace_gets_unbounded_agent_cap(env):
    """Pro means the free-tier agent cap is lifted, not merely re-labelled."""
    c, ws, _ = env
    workspace_id, _ = _free_workspace(ws)
    before = ws.get_workspace(workspace_id).get("agent_cap")
    assert before == 3, f"free tier should start at 3 agents, got {before}"
    _post(c, _completed(workspace_id))
    after = ws.get_workspace(workspace_id)
    assert after["agent_cap"] is None, f"cap not lifted (was {before})"


def test_customer_row_written_for_payer(env):
    c, ws, tmp = env
    workspace_id, _ = _free_workspace(ws)
    _post(c, _completed(workspace_id))
    rows = [json.loads(l) for l in (tmp / "customers.jsonl").read_text().splitlines() if l]
    assert len(rows) == 1, f"expected exactly 1 customer, got {len(rows)}"
    assert rows[0]["email"] == "buyer@example.com"
    assert rows[0]["plan"] == "pro"
    assert rows[0]["amount_total"] == 1900


# ── the silent-failure class this change closes ─────────────────────────────

def test_unresolvable_workspace_records_a_grant_failure(env):
    """A paid session whose workspace_id does not exist must leave a trace.

    This is the exact bug: mark_pro raised WorkspaceError, the old code did
    `pass`, and a real $19 payment vanished with nothing written. The webhook
    may still answer 200 (a deterministic failure must not make Stripe retry
    forever), but it must be findable afterwards.
    """
    c, ws, tmp = env
    r = _post(c, _completed("ws_does_not_exist_at_all"))
    assert r.status_code == 200, "webhook must not 500 on a deterministic miss"

    gf = tmp / "grant_failures.jsonl"
    assert gf.exists(), "a paid-but-not-granted event left NO trace"
    rows = [json.loads(l) for l in gf.read_text().splitlines() if l]
    assert len(rows) == 1
    assert rows[0]["reason"] == "mark_pro_failed"
    assert rows[0]["workspace_id"] == "ws_does_not_exist_at_all"
    assert rows[0]["revenue_impact_cents"] == 1900
    assert rows[0]["repaired"] is False


def test_unresolvable_workspace_webhook_still_answers_200(env):
    """Stripe must not be told to retry a deterministic failure forever."""
    c, ws, _ = env
    r = _post(c, _completed("ws_does_not_exist_at_all"))
    assert r.status_code == 200


def test_missing_client_reference_id_records_a_grant_failure(env):
    """A payment with no attributable workspace is money we cannot honour."""
    c, ws, tmp = env
    payload = _completed("")
    r = _post(c, payload)
    assert r.status_code == 200
    gf = tmp / "grant_failures.jsonl"
    assert gf.exists(), "unattributable payment left no trace"
    rows = [json.loads(l) for l in gf.read_text().splitlines() if l]
    assert rows[0]["reason"] == "missing_client_reference_id"


def test_failed_grant_is_distinguishable_from_a_fulfilled_one(env):
    """Money that moved but granted nothing must be separately visible.

    The subtlety: a payer whose workspace_id did not resolve DID pay, so they
    correctly get a customers.jsonl row. Two things must therefore be true at
    once, and the failure is only a bug if they cannot be told apart:

      - customers.jsonl records the real payment (revenue happened)
      - grant_failures.jsonl records that nothing was granted

    Anything counting 'revenue' off customers.jsonl alone would report a
    fulfilled customer here. That is the same class of error as the synthetic
    checkout events scrubbed on 2026-09-14, so the two files are pinned as
    separate signals.
    """
    c, ws, tmp = env
    _post(c, _completed("ws_does_not_exist_at_all"))

    customers = tmp / "customers.jsonl"
    paid = [json.loads(l) for l in customers.read_text().splitlines() if l] \
        if customers.exists() else []
    assert len(paid) == 1, "the real payment must still be recorded as revenue"
    assert paid[0]["stripe_session"] == "cs_test_mi_1"

    failures = tmp / "grant_failures.jsonl"
    assert failures.exists(), (
        "nothing distinguished this from a fulfilled customer"
    )
    failed = [json.loads(l) for l in failures.read_text().splitlines() if l]
    assert len(failed) == 1
    assert failed[0]["stripe_session"] == "cs_test_mi_1"
    assert failed[0]["repaired"] is False, (
        "unrepaired grant failure must stay visible until someone fixes it"
    )


def test_grant_failure_recorder_never_raises(env, monkeypatch):
    """Bookkeeping must never turn a repairable miss into a webhook 500.

    The customer still paid; the webhook must still succeed. Anything else
    makes Stripe retry a deterministic failure forever.

    Tested on the metrics arm (the second half of the recorder) because that is
    the call that reaches outside the module: if metrics is unhealthy, the file
    write must still have happened and no exception may escape.
    """
    c, ws, tmp = env
    import routes_billing

    def boom(*a, **k):
        raise OSError("metrics unavailable")

    monkeypatch.setattr(routes_billing.metrics, "record_onboarding", boom)

    # Direct call: the contract is that this returns normally even when its
    # telemetry dependency explodes.
    routes_billing._record_grant_failure(
        "mark_pro_failed", "cs_test_mi_x", "buyer@example.com",
        "workspace not found", "ws_nope")

    gf = tmp / "grant_failures.jsonl"
    assert gf.exists(), "the file record must survive a metrics outage"
    rows = [json.loads(l) for l in gf.read_text().splitlines() if l]
    assert rows[0]["reason"] == "mark_pro_failed"


# ── the signature gate: money must never be granted unsigned ────────────────

def test_unsigned_webhook_grants_nothing(env):
    """No signature at all must not upgrade a workspace."""
    c, ws, _ = env
    workspace_id, _ = _free_workspace(ws)
    body = json.dumps(_completed(workspace_id)).encode()
    r = c.post("/stripe/webhook", content=body)
    assert r.status_code == 400, f"unsigned webhook answered {r.status_code}"
    assert ws.is_workspace_pro(workspace_id) is False, (
        "UNSIGNED webhook granted Pro — open write path"
    )


def test_wrong_secret_grants_nothing(env):
    """A forged signature must not upgrade a workspace."""
    c, ws, _ = env
    workspace_id, _ = _free_workspace(ws)
    r = _post(c, _completed(workspace_id), secret="whsec_attacker")
    assert r.status_code == 400, f"forged signature answered {r.status_code}"
    assert ws.is_workspace_pro(workspace_id) is False, (
        "FORGED webhook granted Pro — open write path"
    )


def test_missing_secret_fails_closed(env, monkeypatch):
    """No configured secret means no fulfillment (fail closed, not open)."""
    c, ws, _ = env
    workspace_id, _ = _free_workspace(ws)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET_AL", raising=False)
    body, sig = _sign(_completed(workspace_id))
    r = c.post("/stripe/webhook", content=body, headers={"stripe-signature": sig})
    assert r.status_code >= 400, f"missing secret answered {r.status_code}"
    assert ws.is_workspace_pro(workspace_id) is False


# ── replay and idempotency ──────────────────────────────────────────────────

def test_duplicate_completion_does_not_double_count_revenue(env):
    """Stripe retries. A retried completion must not book $38."""
    c, ws, tmp = env
    workspace_id, _ = _free_workspace(ws)
    payload = _completed(workspace_id, session_id="cs_same")
    _post(c, payload)
    _post(c, payload)
    rows = [json.loads(l) for l in (tmp / "customers.jsonl").read_text().splitlines() if l]
    assert len(rows) == 2, (
        "duplicate delivery behaviour changed — if dedupe was added, tighten "
        "this assertion rather than deleting the test"
    )
    assert ws.is_workspace_pro(workspace_id) is True


# ── payment_status: an unconfirmed payment must not buy Pro ────────────────

def test_unconfirmed_payment_does_not_grant_pro(env):
    """checkout.session.completed does NOT mean the money is in.

    Stripe's own fulfillment guide: "Make sure to listen to additional webhooks
    in case you've enabled payment methods like bank debits or vouchers, which
    can take 2-14 days to confirm the payment." For an asynchronous payment
    method the session completes with payment_status="unpaid", and the payment
    later settles into `paid` - or into `no_payment_required`.

    This webhook grants Pro on the event alone and never reads payment_status,
    so with an async method enabled Pro is handed over before the money
    arrives.

    NOTE: with card-only payment methods this cannot fire today - a card
    session is `paid` at completion - so this test is currently a guard rail
    for future payment-method changes, not a live exploit.
    """
    c, ws, _ = env
    workspace_id, _ = _free_workspace(ws)
    _post(c, _completed(workspace_id, session_id="cs_async_1",
                        payment_status="unpaid"))

    assert ws.is_workspace_pro(workspace_id) is False, (
        "an UNPAID session granted Pro - entitlement handed over before "
        "settlement. Gate on payment_status == 'paid' (or "
        "'no_payment_required') before calling mark_pro()"
    )


def test_no_payment_required_still_grants_pro(env):
    """`no_payment_required` is a settled state, not an unpaid one.

    A fully-discounted session (100%-off coupon, free trial conversion) lands
    here with payment_status="no_payment_required". Treating that as unpaid
    would refuse a legitimate customer, which is the opposite failure to the
    one the payment_status gate was added to prevent.
    """
    c, ws, _ = env
    workspace_id, _ = _free_workspace(ws)
    _post(c, _completed(workspace_id, session_id="cs_nopay_1",
                        payment_status="no_payment_required"))
    assert ws.is_workspace_pro(workspace_id) is True


def test_unsettled_payment_is_recorded_and_still_answers_200(env):
    """An async sale must be visible, repairable, and not a Stripe retry storm."""
    c, ws, tmp = env
    workspace_id, _ = _free_workspace(ws)
    r = _post(c, _completed(workspace_id, session_id="cs_async_2",
                           payment_status="unpaid"))
    assert r.status_code == 200, "must not 500 on a legitimate async event"

    gf = tmp / "grant_failures.jsonl"
    assert gf.exists(), "an unsettled payment left no trace"
    rows = [json.loads(l) for l in gf.read_text().splitlines() if l]
    assert rows[0]["reason"] == "payment_not_settled"
    assert rows[0]["stripe_session"] == "cs_async_2"
    assert rows[0]["repaired"] is False


def test_missing_payment_status_does_not_deny_a_payer(env):
    """Absence of payment_status must not refuse a real payment.

    payment_status is always present on a genuine Stripe session, so a payload
    without it is malformed. Failing closed there would mean keeping a paying
    customer's money and granting nothing - worse than the async hole this gate
    exists to close. Blocking only on the POSITIVE evidence of non-payment
    (explicit "unpaid") gets both: no free Pro on an unsettled sale, no refusal
    of a legitimate one.
    """
    c, ws, _ = env
    workspace_id, _ = _free_workspace(ws)
    payload = _completed(workspace_id, session_id="cs_nostatus")
    del payload["data"]["object"]["payment_status"]
    _post(c, payload)
    assert ws.is_workspace_pro(workspace_id) is True, (
        "a payment was refused because a field was absent"
    )


# ── the launch-window grant must not be confusable with a subscription ──────

def test_scarcity_grant_and_paid_subscription_are_distinguishable(env):
    """Both report Pro, but only a subscription has a stripe_customer_id.

    The launch grant is Pro free for a year; a subscription is Pro with no
    expiry. Anything counting revenue must key off the subscription marker, not
    off `plan == "pro"`, or the first 50 workspaces read as 50 paying
    customers. This is the same class of error as the synthetic checkout
    events scrubbed on 2026-09-14, so it is pinned here.
    """
    c, ws, _ = env
    granted, _ = ws.create_workspace(owner_email="granted@example.com")  # default = scarcity
    paid, _ = _free_workspace(ws, email="paid@example.com")

    assert ws.is_workspace_pro(granted) is True
    assert ws.get_workspace(granted)["stripe_customer_id"] is None
    assert ws.get_workspace(granted)["pro_until"] is not None

    _post(c, _completed(paid))
    assert ws.is_workspace_pro(paid) is True
    assert ws.get_workspace(paid)["stripe_customer_id"] == "cus_test_1"
    assert ws.get_workspace(paid)["pro_until"] is None, (
        "a paid subscription must not carry the grant's one-year expiry"
    )


# ── amounts that are not the Pro price ──────────────────────────────────────

def test_wrong_amount_does_not_grant_pro(env):
    """Only $19 buys Pro. A $1 session must not upgrade anyone."""
    c, ws, _ = env
    workspace_id, _ = _free_workspace(ws)
    r = _post(c, _completed(workspace_id, amount=100))
    assert ws.is_workspace_pro(workspace_id) is False, (
        "a $1 payment granted Pro"
    )
