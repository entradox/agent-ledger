# tests/test_no_paid_downgrade.py
"""Money never takes capability away — a settled Stripe payment may only RAISE a tier.

MEASURED ON LIVE PRODUCTION 2026-09-24, not theorised. A signed
`checkout.session.completed` replay against the real /stripe/webhook with
`amount_total: 1900` ($19 Starter) and `client_reference_id` set to our own
dogfood workspace:

    BEFORE: {"plan": "pro", "agent_cap": null, "pro_until": null}
    AFTER : {"plan": "starter", "agent_cap": 10, "pro_until": 1792972992.02}

`fulfill_paid_session()` derives the tier from the settled AMOUNT alone, then calls
`mark_pro()`, which set `record["plan"] = tier` unconditionally. It failed open on an
*unrecognized* tier name but had no guard against a *lower paid* one.

That makes this the product's most common money interaction, not an edge case:
`create_workspace()` grants Pro via the scarcity window, so a brand-new workspace is
already Pro before anybody pays. The FIRST $19 payment silently downgraded the buyer
to Starter (cap 10) — charging a customer and taking capability away in the same
request.

WHERE THE GUARD LIVES, AND WHY THAT IS THE POINT
    At the amount→tier derivation in `fulfill_paid_session()`, deliberately NOT
    inside `mark_pro()`. mark_pro() is also the x402 day-pass grant, where a
    deliberate Starter cap IS the design (`X402_TRIAL_GRANT`). An earlier version of
    this guard sat in mark_pro() and broke that behaviour plus its two tests — caught
    by running the full suite, and preserved as the reason for the placement.

    `test_the_x402_day_pass_still_caps_deliberately` is the canary for that decision.

This is D-1269 in the mirror: that one granted Pro forever from a single payment;
this one revokes capability when a payment arrives.

These tests drive the REAL webhook handler with an HMAC-signed event and assert the
customer-visible OUTCOME (the plan and cap the buyer's own report will show), not
that a particular branch ran.
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

SECRET = "whsec_test_downgrade_guard"


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET_AL", SECRET)
    monkeypatch.delenv("AL_PRO_ACTIVE", raising=False)
    import workspace_engine, metrics, routes_billing, api_server
    for m in (workspace_engine, metrics, routes_billing, api_server):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    yield TestClient(api_server.app), workspace_engine
    shutil.rmtree(tmp, ignore_errors=True)


def paid_event(workspace_id, amount_cents, session="cs_test_dg"):
    """A real Stripe-shaped checkout.session.completed."""
    return {
        "id": "evt_test_dg",
        "object": "event",
        "type": "checkout.session.completed",
        "created": int(time.time()),
        "data": {"object": {
            "id": session,
            "object": "checkout.session",
            "amount_total": amount_cents,
            "currency": "usd",
            "client_reference_id": workspace_id,
            "customer": "cus_test_dg",
            "customer_details": {"email": "buyer@example.com"},
            "payment_status": "paid",
        }},
    }


def post(client, event):
    payload = json.dumps(event, separators=(",", ":")).encode()
    t = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), f"{t}.".encode() + payload,
                   hashlib.sha256).hexdigest()
    return client.post("/stripe/webhook", content=payload,
                       headers={"stripe-signature": f"t={t},v1={sig}"})


def test_the_first_payment_does_not_downgrade_a_new_workspace(env):
    """THE REGRESSION, in the exact form the live measurement found.

    A brand-new workspace is Pro by the scarcity window with no clock. Before the
    guard, its first $19 payment left it Starter with a 10-agent cap.
    """
    client, we = env
    ws_id, _ = we.create_workspace(owner_email="new@example.com")
    before = we.get_workspace(ws_id)
    assert before["plan"] == "pro" and before["agent_cap"] is None

    r = post(client, paid_event(ws_id, 1900))
    assert r.status_code == 200

    after = we.get_workspace(ws_id)
    assert after["plan"] == "pro", "the first payment downgraded a new workspace"
    assert after["agent_cap"] is None, "the agent cap was cut by an incoming payment"


def test_starter_payment_does_not_downgrade_a_team_workspace(env):
    client, we = env
    ws_id, _ = we.create_workspace(owner_email="team@example.com")
    we.revoke_pro(ws_id)
    we.mark_pro(ws_id, "cus_t", tier="team")
    assert we.get_workspace(ws_id)["plan"] == "team"

    post(client, paid_event(ws_id, 1900))          # a $19 Starter seat

    rec = we.get_workspace(ws_id)
    assert rec["plan"] == "team", "a $19 payment downgraded a Team customer"
    assert rec["agent_cap"] == we.TEAM_AGENT_CAP


def test_a_higher_payment_still_upgrades(env):
    """Canary for over-correction: the guard must not block real upgrades."""
    client, we = env
    ws_id, _ = we.create_workspace(owner_email="up@example.com")
    we.revoke_pro(ws_id)
    we.mark_pro(ws_id, "cus_u", tier="starter")
    assert we.get_workspace(ws_id)["plan"] == "starter"

    post(client, paid_event(ws_id, 7900, session="cs_team"))    # $79 Team
    assert we.get_workspace(ws_id)["plan"] == "team", "a real upgrade was blocked"

    post(client, paid_event(ws_id, 1900, session="cs_starter2"))  # $19 again
    assert we.get_workspace(ws_id)["plan"] == "team", "a starter amount knocked Team down"


def test_free_workspace_still_becomes_starter(env):
    """A FREE workspace is not protected — the first real payment must land."""
    client, we = env
    ws_id, _ = we.create_workspace(owner_email="free@example.com")
    we.revoke_pro(ws_id)
    assert we.get_workspace(ws_id)["plan"] == "free"

    post(client, paid_event(ws_id, 1900))
    rec = we.get_workspace(ws_id)
    assert rec["plan"] == "starter", "a free workspace's first payment was blocked"
    assert rec["agent_cap"] == we.STARTER_AGENT_CAP


def test_the_payment_is_still_recorded_when_the_tier_is_kept(env):
    """Keeping the higher tier must NOT skip the accounting side-effects.

    Two things must survive a non-downgrade, and the second one is subtle:

    1. The MONEY is still recorded — `customers.jsonl` gets the row, so a settled
       payment is never invisible just because it did not change the plan.
    2. The workspace's EXISTING stripe_customer_id is NOT overwritten. This is why
       the guard grants nothing rather than calling mark_pro() with the higher tier:
       mark_pro sets `record["stripe_customer_id"] = <the incoming customer>`, so
       re-granting through it would repoint a Team workspace at the Starter buyer's
       customer id and orphan the Team subscription — a later Team cancellation
       could then never find the workspace it must downgrade.

    An earlier draft of this test asserted the NEW customer id WAS linked. That was
    wrong, and the failure is kept here as the reason the implementation deliberately
    leaves the customer link alone.
    """
    client, we = env
    import routes_billing
    ws_id, _ = we.create_workspace(owner_email="acct@example.com")
    we.revoke_pro(ws_id)
    we.mark_pro(ws_id, "cus_existing_team", tier="team")

    post(client, paid_event(ws_id, 1900))          # a $19 Starter seat

    # Read the file the WRITER actually writes (routes_billing.CUSTOMERS_FILE).
    # It is resolved from DATA_DIR at import time, so reaching for
    # AGENT_LEDGER_DATA here would look in a directory this module never used.
    rows = [json.loads(l) for l in
            routes_billing.CUSTOMERS_FILE.read_text().splitlines() if l.strip()]
    assert rows, "no customer row was written for a settled payment"
    assert rows[-1]["amount_total"] == 1900, "the money was not recorded"
    assert rows[-1]["plan"] == "team", "the recorded plan should be the one that was kept"

    rec = we.get_workspace(ws_id)
    assert rec["plan"] == "team"
    assert rec["stripe_customer_id"] == "cus_existing_team", (
        "the incoming customer id repointed the workspace, which would orphan the "
        "subscription a later cancellation event must be able to find")
    assert we.get_workspace_by_stripe_customer("cus_existing_team") is not None, (
        "the lifecycle index must still resolve the workspace's real subscription")


def test_the_x402_day_pass_still_caps_deliberately(env):
    """CANARY for the placement decision: the guard must NOT live in mark_pro().

    The x402 one-trial pass caps to Starter ON PURPOSE (X402_TRIAL_GRANT). An earlier
    version of this guard sat in mark_pro() and broke it, along with two x402 tests.
    """
    _, we = env
    ws_id, _ = we.create_workspace(owner_email="pass@example.com")
    assert we.get_workspace(ws_id)["plan"] == "pro"

    we.mark_pro(ws_id, "", pro_until=time.time() + 24 * 3600,
                period_source="x402_pass", tier="starter")
    rec = we.get_workspace(ws_id)
    assert rec["plan"] == "starter", "the x402 day-pass cap was blocked"
    assert rec["agent_cap"] == we.STARTER_AGENT_CAP
    assert rec["pro_until"] is not None
