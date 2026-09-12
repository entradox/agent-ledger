#!/usr/bin/env python3
"""Onboarding funnel instrumentation (D-1163).

The product had a live abandoned checkout and could not say anything about it:
`/start` GET and POST shared one reach bucket, the upgrade button left no trace
because it is a plain link to Stripe, and the webhook only subscribed to
`checkout.session.completed`. These tests pin the step stream, its privacy
rules, and the page invariant that carries the money path.
"""
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

AL_VERSION = "2026-09-01"
WEBHOOK_SECRET = "whsec_test_d1163"


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="agent-ledger-onboarding-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    monkeypatch.setenv("AL_ADMIN_SECRET", "test-admin")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET_AL", WEBHOOK_SECRET)
    import importlib
    # ORDER MATTERS: api_server binds routes_agents' router at import time, so
    # routes_agents (and everything it imported) must be reloaded BEFORE
    # api_server or the app keeps stale function objects and new code never
    # runs. This bit the D-1163 build — the activation events silently never
    # fired under the test fixture while working fine in production shape.
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
    importlib.reload(routes_agents)   # re-decorate onto the reloaded api_server app
    importlib.reload(api_server)
    from fastapi.testclient import TestClient
    return TestClient(api_server.app)


def _mint(client):
    r = client.post("/start")
    assert r.status_code == 200
    return re.search(r"workspace_id: (ws_[A-Za-z0-9_\-]+)", r.text).group(1), r.text


def _funnel():
    import metrics
    return {s["step"]: s for s in metrics.onboarding_funnel()["steps"]}


def test_minting_emits_workspace_minted_and_key_revealed(client):
    ws, _ = _mint(client)
    f = _funnel()
    assert f["workspace_minted"]["workspaces"] == 1
    assert f["key_revealed"]["workspaces"] == 1
    # the mint is the top of the funnel: no predecessor, so no drop-off to
    # report. None, not 0 — 0 would read as "everybody dropped off".
    assert f["workspace_minted"]["dropped_from_previous"] is None


def test_the_stream_carries_no_ip_and_no_credential(client):
    """This stream is read by humans; a raw IP hash or a key must never land in
    it. `record_onboarding` strips them even if a caller passes them."""
    import metrics
    metrics.record_onboarding("workspace_minted", "ws_abc1234567890",
                              ip_hash="deadbeefcafe", workspace_key="wk_live_secret",
                              agent_secret="nope")
    line = [l for l in open(metrics.METRICS_FILE) if "ws_abc1234567890" in l][-1]
    assert "deadbeefcafe" not in line
    assert "wk_live_secret" not in line
    assert "nope" not in line
    assert "ws_abc1234567890" in line


def test_an_unknown_step_is_ignored_not_recorded(client):
    """A typo must not silently widen the stream."""
    import metrics
    before = len([l for l in open(metrics.METRICS_FILE)] ) if metrics.METRICS_FILE.exists() else 0
    metrics.record_onboarding("definitely_not_a_step", "ws_abc1234567890")
    after = len([l for l in open(metrics.METRICS_FILE)]) if metrics.METRICS_FILE.exists() else 0
    assert after == before
    assert "definitely_not_a_step" not in {s["step"] for s in metrics.onboarding_funnel()["steps"]}


def test_checkout_click_beacon_records_for_a_real_workspace(client):
    ws, _ = _mint(client)
    assert client.get(f"/v1/_beacon?event=start_checkout_click&ws={ws}").status_code == 200
    assert _funnel()["checkout_clicked"]["workspaces"] == 1


def test_checkout_click_beacon_ignores_a_junk_workspace_id(client):
    """The allowlist guards the event name; the workspace id is validated too,
    or a caller could invent workspaces in the funnel."""
    import metrics
    for junk in ("../../etc/passwd", "ws_short", "", "'; DROP TABLE--",
                 "ws_" + "a" * 200):
        assert client.get(
            f"/v1/_beacon?event=start_checkout_click&ws={junk}").status_code == 200
    assert _funnel().get("checkout_clicked", {}).get("workspaces", 0) == 0


def test_unknown_beacon_event_is_still_not_recorded(client):
    import metrics
    assert client.get("/v1/_beacon?event=made_up_event").status_code == 200
    assert metrics.snapshot()["totals"].get("made_up_event", 0) == 0


def test_the_dropoff_between_steps_is_computed(client):
    """Two mints, one click: the funnel must show the step where they stop."""
    ws1, _ = _mint(client)
    _mint(client)
    client.get(f"/v1/_beacon?event=start_checkout_click&ws={ws1}")
    f = _funnel()
    assert f["workspace_minted"]["workspaces"] == 2
    assert f["checkout_clicked"]["workspaces"] == 1
    step = f["checkout_clicked"]
    assert step["dropped_from_previous"] == 1
    assert step["conversion_from_previous"] == 0.5


def _signed(client, event):
    payload = json.dumps(event).encode()
    t = int(time.time())
    sig = hmac.new(WEBHOOK_SECRET.encode(), f"{t}.".encode() + payload,
                   hashlib.sha256).hexdigest()
    return client.post("/stripe/webhook", content=payload,
                       headers={"content-type": "application/json",
                                "stripe-signature": f"t={t},v1={sig}"})


def test_an_expired_session_is_recorded_as_abandoned_and_fulfils_nothing(client):
    """Stripe's only abandonment signal (expiry, ~24h late). It must record the
    step and must NOT touch the fulfillment path."""
    import metrics
    ws, _ = _mint(client)
    r = _signed(client, {"id": "evt_exp", "type": "checkout.session.expired",
                         "data": {"object": {"id": "cs_expired",
                                             "client_reference_id": ws}}})
    assert r.status_code == 200
    assert r.json().get("ignored") == "checkout.session.expired"
    assert _funnel()["checkout_abandoned"]["workspaces"] == 1
    # no customer row, no pro flip
    from ledger_engine import DATA_DIR
    assert not (DATA_DIR / "customers.jsonl").exists()


def test_a_completed_session_enters_the_onboarding_stream(client):
    ws, _ = _mint(client)
    r = _signed(client, {"id": "evt_ok", "type": "checkout.session.completed",
                         "data": {"object": {"id": "cs_ok", "amount_total": 1900,
                                             "customer": "cus_x",
                                             "client_reference_id": ws,
                                             "customer_details": {"email": "a@example.com"}}}})
    assert r.status_code == 200
    assert _funnel()["checkout_completed"]["workspaces"] == 1


def test_claiming_an_agent_is_recorded_as_activation(client):
    """A minted workspace that never claims an agent is not a signup. This is
    the step that separates the two."""
    ws, html = _mint(client)
    key = re.search(r"(wk_live_[A-Za-z0-9_\-]+)", html).group(1)
    r = client.post("/v1/track", headers={"AL-API-Version": AL_VERSION},
                    json={"agent_id": "act-agent", "rail": "manual",
                          "amount_cents": 100, "service": "s",
                          "workspace_key": key})
    assert r.status_code == 200, r.text
    f = _funnel()
    assert f["agent_claimed"]["workspaces"] == 1
    assert f["track_written"]["workspaces"] == 1


def test_the_post_mint_page_keeps_the_money_path_and_softens_the_upsell(client):
    """Two invariants on the page whose layout is a conversion decision:
    the upgrade link MUST carry client_reference_id (that is the whole
    fulfillment mechanism), and the $19 CTA must not be the primary button on
    a page whose message is 'you are done and it is free'."""
    ws, html = _mint(client)
    assert f"client_reference_id={ws}" in html
    assert 'class="btn"' not in html, "the gold primary CTA is back on the post-mint page"
    assert "Nothing to pay" in html
    assert "start_checkout_click" in html


def test_metrics_exposes_the_onboarding_block(client):
    ws, _ = _mint(client)
    r = client.get("/v1/metrics", headers={"x-al-admin": "test-admin"})
    assert r.status_code == 200
    body = r.json()
    assert "onboarding" in body
    steps = {s["step"]: s for s in body["onboarding"]["steps"]}
    assert steps["workspace_minted"]["workspaces"] == 1
    # the pre-existing blocks must survive
    assert "funnel" in body and "reach" in body and "checkout_funnel" in body


def test_reach_survives_a_restart(client):
    """Reach is the number used to judge whether distribution is working, so it
    must not reset when the service redeploys. The in-memory unique-ip set is
    process-lifetime — after the D-1163 deploy it fell from 17 to 2 and read as
    "the traffic stopped" when nothing had. This pins the file-backed path."""
    import importlib
    import metrics
    metrics.record_event("http", path="/", method="GET", status=200,
                         ip_hash="aaaa1111", ip_bucket="path:/")
    metrics.record_event("http", path="/", method="GET", status=200,
                         ip_hash="bbbb2222", ip_bucket="path:/")
    importlib.reload(metrics)          # simulate a redeploy: fresh in-memory state
    assert metrics.reach_from_file().get("path:/") == 2


def test_reach_reconstructs_the_bucket_for_events_written_before_the_fix(client):
    """The reach counter restarted at zero after the D-1163 deploy partly
    because older lines carried an ip_hash and a path but no ip_bucket. Rather
    than throw that history away, the bucket is derived from the path — which is
    exactly the rule the middleware used."""
    import importlib
    import metrics
    metrics.record_event("http", path="/start", method="GET", status=200,
                         ip_hash="cccc3333")          # no ip_bucket, like the old lines
    importlib.reload(metrics)
    assert metrics.reach_from_file().get("path:/start") == 1
