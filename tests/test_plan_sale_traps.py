"""The two money-path honesty defects found live on 2026-09-24.

Both were MEASURED against production, not theorised:

ITEM 2 — `POST /v1/billing/checkout?plan=team` returned HTTP 200 carrying the $19
Starter link with `plan: "starter"`. The label was honest but the CALLER had asked
for Team; a machine receiving 200 reads it as "Team bought" and then delivers a $79
entitlement nobody paid for. Worse, the old code passed through when
AL_STRIPE_TEAM_LINK was unset — the exact live state.

ITEM 3 — `POST /start?plan=starter` with `Accept: application/json` returned 200 and
silently minted a FREE workspace. An agent asked for a paid plan, got 200, and ended
up owning a 3-agent free workspace — while llms.txt advertised that exact URL as how
to subscribe.

What these tests hold:
  * asking for Team without a Team link is REFUSED, never substituted downward
  * a machine asking /start for a paid plan is REFUSED before anything is minted
  * the happy paths (human form, machine free mint, real Team link) still work
  * llms.txt/pricing copy no longer tells machines to subscribe via /start
"""
import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

STARTER = "https://buy.stripe.com/14AbJ0clUeoE9QN3Nl2400e"
TEAM = "https://buy.stripe.com/team_placeholder_link"


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET_AL", "whsec_plan_trap")
    monkeypatch.delenv("AL_PRO_ACTIVE", raising=False)
    monkeypatch.delenv("AL_STRIPE_TEAM_LINK", raising=False)
    import ledger_engine, workspace_engine, identity, metrics, routes_billing, api_server
    for m in (ledger_engine, workspace_engine, identity, metrics, routes_billing, api_server):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    yield TestClient(api_server.app), workspace_engine
    shutil.rmtree(tmp, ignore_errors=True)


def _fresh_creds(we):
    """A workspace whose key we hold, like any real buyer."""
    ws_id, raw_key = we.create_workspace(grant_scarcity=False)
    return ws_id, raw_key


# ── ITEM 2: the plan=team trap ──────────────────────────────────────────────

def test_team_request_is_refused_when_team_link_is_unset(env):
    """THE defect. Was: 200 + the $19 Starter link under a Team request."""
    client, we = env
    ws_id, raw_key = _fresh_creds(we)

    r = client.post("/v1/billing/checkout?plan=team",
                    headers={"X-Workspace-Id": ws_id, "X-Workspace-Key": raw_key})

    assert r.status_code == 409, (
        f"a Team request must not be answered with a cheaper plan; got {r.status_code} "
        f"{r.text[:200]}")
    body = r.text
    assert "plan_not_available" in body, "the refusal must be typed"
    # The fatal failure mode: silently handing back the Starter link.
    assert STARTER not in body, (
        "the $19 Starter link was returned for a Team request — that charges the "
        "wrong amount and sells a $79 promise")


def test_team_request_is_refused_before_any_charge(env):
    """409, not 402: no payment fixes a missing Team link on this deployment."""
    client, we = env
    ws_id, raw_key = _fresh_creds(we)
    r = client.post("/v1/billing/checkout?plan=team",
                    headers={"X-Workspace-Id": ws_id, "X-Workspace-Key": raw_key})
    assert r.status_code != 402, (
        "402 means 'pay me' and an agent would retry payment forever; the Team link "
        "cannot be fixed by paying")


def test_starter_request_still_returns_the_starter_link(env):
    """Canary: the refusal must not break the plan that DOES exist."""
    client, we = env
    ws_id, raw_key = _fresh_creds(we)
    r = client.post("/v1/billing/checkout?plan=starter",
                    headers={"X-Workspace-Id": ws_id, "X-Workspace-Key": raw_key})
    assert r.status_code == 200, r.text[:200]
    j = r.json()
    assert j["plan"] == "starter"
    assert j["checkout_url"].startswith(STARTER)
    assert ws_id in j["checkout_url"], "the link must carry client_reference_id"


def test_a_real_team_link_is_used_when_configured(env, monkeypatch):
    """When the operator sets the link, plan=team must sell TEAM."""
    client, we = env
    monkeypatch.setenv("AL_STRIPE_TEAM_LINK", TEAM)
    import routes_billing
    importlib.reload(routes_billing)
    import api_server
    importlib.reload(api_server)
    from fastapi.testclient import TestClient
    client = TestClient(api_server.app)

    ws_id, raw_key = _fresh_creds(we)
    r = client.post("/v1/billing/checkout?plan=team",
                    headers={"X-Workspace-Id": ws_id, "X-Workspace-Key": raw_key})
    assert r.status_code == 200, r.text[:200]
    j = r.json()
    assert j["plan"] == "team", "a configured Team link must be labelled team"
    assert j["checkout_url"].startswith(TEAM), "the Team link was not used"
    assert STARTER not in j["checkout_url"], "fell back to Starter with Team set"


# ── ITEM 3: the /start plan trap ────────────────────────────────────────────

def test_machine_asking_start_for_a_paid_plan_is_refused(env):
    """THE defect. Was: 200 + a silently minted FREE workspace."""
    client, _ = env
    r = client.post("/start?plan=starter", headers={"Accept": "application/json"})

    assert r.status_code == 409, (
        f"a machine asking /start for a paid plan must not get a free mint; got "
        f"{r.status_code}")
    body = r.text
    assert "plan_requires_human_checkout" in body, "the refusal must be typed"
    assert "workspace_key" not in body, (
        "a free WORKSPACE WAS MINTED and its key returned for a paid plan request — "
        "the agent now owns a free workspace believing it subscribed")


def test_machine_refusal_mints_no_workspace(env):
    """Refuse BEFORE minting: no orphan workspace, no launch slot burned."""
    client, we = env
    before = len(list(we._workspaces_dir().glob("*"))) if hasattr(we, "_workspaces_dir") else None
    r = client.post("/start?plan=starter", headers={"Accept": "application/json"})
    assert r.status_code == 409
    # The response must not carry a credential of any kind.
    assert "wk_live_" not in r.text, "a workspace key was issued despite the refusal"
    assert "ws_" not in r.text or "workspace_id" not in r.text, (
        "a workspace id was returned despite the refusal")


def test_machine_free_mint_still_works(env):
    """Canary: the plain machine path (no plan) must still mint."""
    client, _ = env
    r = client.post("/start", headers={"Accept": "application/json"})
    assert r.status_code == 200, f"the free machine mint broke: {r.status_code}"
    j = r.json()
    assert j["plan"] == "free"
    assert j["workspace_key"].startswith("wk_")


def test_human_form_for_a_paid_plan_is_still_rendered(env):
    """Canary: a HUMAN asking for Starter must still get the form, not a 409."""
    client, _ = env
    r = client.get("/start?plan=starter", headers={"Accept": "text/html"})
    assert r.status_code == 200, "the human door was closed by the machine guard"
    assert "Create my workspace" in r.text or "form" in r.text.lower()


# ── the copy must match the shipped behaviour (item 3's second half) ────────

def test_llms_txt_no_longer_sends_machines_to_start_to_subscribe(env):
    """`subscribe at /start?plan=starter` was false — /start cannot take money."""
    client, _ = env
    txt = client.get("/llms.txt").text

    assert "subscribe at /start" not in txt, (
        "llms.txt still tells agents to subscribe via /start, which mints a free "
        "workspace and returns 200")
    assert "/v1/billing/checkout" in txt, (
        "the human-checkout route is no longer discoverable from llms.txt")


def test_llms_txt_says_monthly_plans_need_a_human(env):
    """The honest version: agent-purchasable is x402; monthly needs a person."""
    client, _ = env
    txt = client.get("/llms.txt").text
    assert "human" in txt.lower(), (
        "llms.txt must say the monthly plans require a human checkout")


def test_checkout_route_is_discoverable_as_the_monthly_path(env):
    """The paid path an agent is pointed at must itself behave honestly."""
    import x402_verify
    assert x402_verify.X402_TRIAL_PAID_PATH == "/v1/billing/checkout", (
        "the documented paid path drifted off the route that can actually transact")
