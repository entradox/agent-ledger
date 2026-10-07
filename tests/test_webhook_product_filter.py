#!/usr/bin/env python3
"""D-1571 — shared-Stripe-account product filter.

One Stripe account hosts five products; every subscribed event is delivered to
every endpoint. Before this filter, `checkout.session.expired` from GasPermit's
$29 probe harness was recorded as AgentLedger `checkout_abandoned` (57 of 89
all-time rows), and a sibling product's `completed` session was one
ws_-shaped client_reference_id away from granting AgentLedger Pro.

These tests pin the attribution contract:

  * payment_link in the allowlist -> ours (every AL session is link-created)
  * payment_link outside it      -> foreign (a sibling link's session can
    never be our sale, whatever client_reference_id it carries)
  * ws_-shaped ref, no link      -> ours
  * anything else                -> unknown: excluded from the funnel (a
    polluted counter is the only cost of a wrong call) but still processed
    on the MONEY path so an unattributable paid session keeps reaching
    grant_failures.jsonl — the payer controls client_reference_id as a URL
    param, so an odd ref cannot prove a session foreign

Run: /opt/miniconda3/bin/python3 -m pytest tests/test_webhook_product_filter.py -q
"""
import hashlib
import hmac
import json
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

WEBHOOK_SECRET = "whsec_test_d1555"
OUR_PLINK = "plink_1UD8hQAYgZaqWHnhiT4bKfrM"      # live Starter link id
FOREIGN_PLINK = "plink_gaspermits_link_000"      # any id outside the allowlist


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="agent-ledger-d1555-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    monkeypatch.setenv("AL_ADMIN_SECRET", "test-admin")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET_AL", WEBHOOK_SECRET)
    import importlib
    import metrics, ledger_engine, workspace_engine, identity, routes_agents
    importlib.reload(metrics)
    importlib.reload(ledger_engine)
    importlib.reload(workspace_engine)
    importlib.reload(identity)
    importlib.reload(routes_agents)
    import api_server
    importlib.reload(api_server)
    import routes_billing
    importlib.reload(routes_billing)
    importlib.reload(routes_agents)
    importlib.reload(api_server)
    monkeypatch.setattr(workspace_engine, "DEFAULT_BUDGET", {})
    from fastapi.testclient import TestClient
    return TestClient(api_server.app)


def _signed(client, event):
    payload = json.dumps(event).encode()
    t = int(time.time())
    sig = hmac.new(WEBHOOK_SECRET.encode(), f"{t}.".encode() + payload,
                   hashlib.sha256).hexdigest()
    return client.post("/stripe/webhook", content=payload,
                       headers={"content-type": "application/json",
                                "stripe-signature": f"t={t},v1={sig}"})


def _mint(client):
    r = client.post("/start")
    assert r.status_code == 200
    return re.search(r"workspace_id: (ws_[A-Za-z0-9_\-]+)", r.text).group(1)


def _abandoned_rows():
    import metrics
    if not metrics.METRICS_FILE.exists():
        return []
    return [json.loads(l) for l in metrics.METRICS_FILE.read_text().splitlines()
            if '"checkout_abandoned"' in l]


def _grant_failures(tmp):
    gf = Path(tmp) / "grant_failures.jsonl"
    if not gf.exists():
        return []
    return [json.loads(l) for l in gf.read_text().splitlines() if l.strip()]


def _tmp(client):
    import ledger_engine
    return Path(ledger_engine.DATA_DIR)


# ── expired: the funnel only counts OUR sessions ────────────────────────────

def test_gaspermit_probe_expired_is_not_counted_as_our_abandonment(client):
    """The exact session shape that poisoned the funnel: API-created probe,
    payment_link null, empty client_reference_id."""
    r = _signed(client, {"id": "evt_gp", "type": "checkout.session.expired",
                         "data": {"object": {"id": "cs_gp_probe",
                                             "amount_total": 2900,
                                             "payment_link": None,
                                             "client_reference_id": ""}}})
    assert r.status_code == 200
    assert _abandoned_rows() == []


def test_foreign_payment_link_expired_is_not_counted(client):
    """A session from a sibling product's payment link is never ours —
    even if it happens to carry a ws_-shaped reference."""
    ws = _mint(client)
    r = _signed(client, {"id": "evt_fl", "type": "checkout.session.expired",
                         "data": {"object": {"id": "cs_foreign_link",
                                             "amount_total": 1900,
                                             "payment_link": FOREIGN_PLINK,
                                             "client_reference_id": ws}}})
    assert r.status_code == 200
    assert _abandoned_rows() == []


def test_our_payment_link_expired_is_counted(client):
    """A buyer who opened our link but skipped the workspace query param is
    still our abandonment — payment_link is the attribution."""
    r = _signed(client, {"id": "evt_ours", "type": "checkout.session.expired",
                         "data": {"object": {"id": "cs_ours",
                                             "amount_total": 1900,
                                             "payment_link": OUR_PLINK}}})
    assert r.status_code == 200
    assert len(_abandoned_rows()) == 1


def test_workspace_ref_expired_is_counted(client):
    """The /v1/billing/checkout path always appends a ws_ ref."""
    ws = _mint(client)
    r = _signed(client, {"id": "evt_ws", "type": "checkout.session.expired",
                         "data": {"object": {"id": "cs_ws",
                                             "client_reference_id": ws}}})
    assert r.status_code == 200
    assert len(_abandoned_rows()) == 1


# ── completed: foreign sessions can never grant ─────────────────────────────

def test_foreign_link_completed_with_our_ws_ref_grants_nothing(client):
    """Defect 3 closed: a sibling product's $19 session naming one of our
    workspaces must not grant it Pro."""
    import workspace_engine
    ws = _mint(client)
    r = _signed(client, {"id": "evt_fx", "type": "checkout.session.completed",
                         "data": {"object": {
                             "id": "cs_xproduct", "amount_total": 1900,
                             "payment_status": "paid", "customer": "cus_gp",
                             "payment_link": FOREIGN_PLINK,
                             "client_reference_id": ws,
                             "customer_details": {"email": "probe@gasstationpermit.com"}}}})
    assert r.status_code == 200
    assert r.json().get("ignored") == "foreign_session"
    assert workspace_engine.is_workspace_pro(ws) is False
    assert not (_tmp(client) / "customers.jsonl").exists()


def test_odd_ref_completed_still_reaches_the_repair_queue(client):
    """A non-ws ref cannot prove the session foreign (the payer controls it
    as a URL param) — a PAID session with an unusable ref must leave a money
    trail. The grant itself is still refused by the shape check downstream."""
    r = _signed(client, {"id": "evt_fr", "type": "checkout.session.completed",
                         "data": {"object": {
                             "id": "cs_fref", "amount_total": 1900,
                             "payment_status": "paid", "customer": "cus_gp",
                             "client_reference_id": "gp_order_991",
                             "customer_details": {"email": "p@gasstationpermit.com"}}}})
    assert r.status_code == 200
    rows = _grant_failures(_tmp(client))
    assert rows and rows[-1]["reason"] == "client_reference_id_not_a_workspace_id"


def test_unattributed_completed_still_reaches_the_repair_queue(client):
    """No link, no ref: cannot be proven foreign, and it is PAID — the
    money trail must survive even though nothing can be granted."""
    r = _signed(client, {"id": "evt_un", "type": "checkout.session.completed",
                         "data": {"object": {
                             "id": "cs_amb", "amount_total": 1900,
                             "payment_status": "paid", "customer": "cus_amb",
                             "client_reference_id": "",
                             "customer_details": {"email": "mystery@x.com"}}}})
    assert r.status_code == 200
    rows = _grant_failures(_tmp(client))
    assert rows and rows[-1]["reason"] == "missing_client_reference_id"


def test_our_link_completed_grants_pro(client):
    import workspace_engine
    ws = _mint(client)
    r = _signed(client, {"id": "evt_ok", "type": "checkout.session.completed",
                         "data": {"object": {
                             "id": "cs_al", "amount_total": 1900,
                             "payment_status": "paid", "customer": "cus_al",
                             "payment_link": OUR_PLINK,
                             "client_reference_id": ws,
                             "customer_details": {"email": "b@example.com"}}}})
    assert r.status_code == 200
    assert workspace_engine.is_workspace_pro(ws) is True


# ── lifecycle: foreign objects stay countable but out of the repair queue ────

def test_foreign_subscription_deleted_is_not_a_repair_row(client):
    """A sibling product's cancellation must not enter grant_failures.jsonl —
    it cannot be repaired because it was never ours."""
    code = _signed(client, {"id": "evt_sd", "type": "customer.subscription.deleted",
                            "data": {"object": {"id": "sub_gp",
                                                "customer": "cus_gp",
                                                "status": "canceled",
                                                "current_period_end":
                                                    int(time.time()) + 86400}}})
    assert code.status_code == 200
    assert _grant_failures(_tmp(client)) == []
    import metrics
    assert metrics.snapshot()["totals"].get("lifecycle_unresolved", 0) == 1


def test_foreign_invoice_paid_is_not_counted_as_our_renewal(client):
    """An unattributed subscription_cycle invoice is a sibling's revenue."""
    r = _signed(client, {"id": "evt_ip", "type": "invoice.paid",
                         "data": {"object": {"id": "in_gp", "customer": "cus_gp",
                                             "amount_paid": 2900,
                                             "billing_reason": "subscription_cycle"}}})
    assert r.status_code == 200
    import metrics
    assert metrics.snapshot()["totals"].get("subscription_renewed", 0) == 0
    if metrics.METRICS_FILE.exists():
        assert "subscription_renewed" not in metrics.METRICS_FILE.read_text()


def test_unresolved_lifecycle_with_our_payer_email_stays_a_repair_row(client):
    """The exception: the email matches a recorded payer, so it may be ours."""
    tmp = _tmp(client)
    (tmp / "customers.jsonl").write_text(
        json.dumps({"ts": 1, "email": "payer@x.com", "plan": "pro",
                    "status": "active"}) + "\n")
    r = _signed(client, {"id": "evt_ou", "type": "customer.subscription.deleted",
                         "data": {"object": {"id": "sub_ours",
                                             "customer": "cus_unindexed",
                                             "customer_email": "payer@x.com",
                                             "status": "canceled"}}})
    assert r.status_code == 200
    rows = _grant_failures(tmp)
    assert rows and rows[-1]["reason"] == "cancel_unresolved"


def test_refless_lifecycle_writes_no_spurious_missing_ref_row(client):
    """Lifecycle objects carry no client_reference_id. Feeding an empty one
    into the session resolver used to file a missing_client_reference_id
    failure for every lifecycle event — repair-queue noise."""
    r = _signed(client, {"id": "evt_nr", "type": "customer.subscription.updated",
                         "data": {"object": {"id": "sub_gp2",
                                             "customer": "cus_gp",
                                             "status": "active"}}})
    assert r.status_code == 200
    assert _grant_failures(_tmp(client)) == []


def test_foreign_async_payment_succeeded_grants_nothing(client):
    """The delayed-settlement path takes the same ownership gate."""
    import workspace_engine
    ws = _mint(client)
    r = _signed(client, {"id": "evt_as", "type": "checkout.session.async_payment_succeeded",
                         "data": {"object": {
                             "id": "cs_ach_gp", "amount_total": 1900,
                             "payment_status": "paid", "customer": "cus_gp",
                             "payment_link": FOREIGN_PLINK,
                             "client_reference_id": ws,
                             "customer_details": {"email": "p@gasstationpermit.com"}}}})
    assert r.status_code == 200
    assert r.json().get("ignored") == "foreign_session"
    assert workspace_engine.is_workspace_pro(ws) is False
