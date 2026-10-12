"""The two money-path honesty defects found live on 2026-09-24, and the door
that replaced them (blueprint v2, 2026-10-11).

Both were MEASURED against production, not theorised:

ITEM 2 — `POST /v1/billing/checkout?plan=team` returned HTTP 200 carrying the $19
Starter link with `plan: "starter"`. The label was honest but the CALLER had asked
for Team; a machine receiving 200 reads it as "Team bought" and then delivers a $79
entitlement nobody paid for.

ITEM 3 — `POST /start?plan=starter` with `Accept: application/json` returned 200 and
silently minted a FREE workspace. An agent asked for a paid plan, got 200, and ended
up owning a 3-agent free workspace — while llms.txt advertised that exact URL as how
to subscribe.

Blueprint v2 removed the human checkout entirely: POST /v1/billing/checkout now
answers 410 `endpoint_retired` for every caller, and the only paid path is
POST /v1/billing/x402. What these tests hold now:

  * the retired checkout can NEVER transact — not for Team, not for Starter,
    and not when an operator sets a stale link env var
  * the 410 names the real replacement (x402) rather than stranding the caller
  * a machine asking /start for a paid plan is REFUSED before anything is minted
  * the happy paths (human form, machine free mint) still work
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


# ── the retired checkout door ───────────────────────────────────────────────

def test_checkout_route_is_gone_for_a_team_request(env):
    """The Team trap cannot recur: the route that used to hand back the wrong
    price no longer sells anything at all."""
    client, we = env
    ws_id, raw_key = _fresh_creds(we)

    r = client.post("/v1/billing/checkout?plan=team",
                    headers={"X-Workspace-Id": ws_id, "X-Workspace-Key": raw_key})

    assert r.status_code == 410, (
        f"the retired checkout must answer 410, got {r.status_code} "
        f"{r.text[:200]}")
    body = r.text
    assert "endpoint_retired" in body, "the retirement must be typed"
    # The fatal failure mode: any link it could charge through.
    assert STARTER not in body and "buy.stripe" not in body, (
        "the retired route still emitted a Stripe link a caller could pay")


def test_checkout_route_is_gone_for_a_starter_request_too(env):
    """No half-retirement: the plan that DID work must fail the same way."""
    client, we = env
    ws_id, raw_key = _fresh_creds(we)
    r = client.post("/v1/billing/checkout?plan=starter",
                    headers={"X-Workspace-Id": ws_id, "X-Workspace-Key": raw_key})
    assert r.status_code == 410, r.text[:200]
    assert "checkout_url" not in r.json(), "a checkout URL was still issued"
    env_err = r.json()["error"]
    assert env_err["code"] == "endpoint_retired"
    assert "/v1/billing/x402" in env_err["message"], (
        "the retirement must point the caller at the live paid path")


def test_checkout_stays_retired_even_when_a_link_env_is_set(env, monkeypatch):
    """A stale env var must not resurrect the door — the same class of drift
    that made a $79 request settle $19."""
    client, we = env
    monkeypatch.setenv("AL_STRIPE_TEAM_LINK", TEAM)
    monkeypatch.setenv("AL_STRIPE_PAYMENT_LINK", STARTER)
    import routes_billing
    importlib.reload(routes_billing)
    import api_server
    importlib.reload(api_server)
    from fastapi.testclient import TestClient
    client = TestClient(api_server.app)

    ws_id, raw_key = _fresh_creds(we)
    for plan in ("team", "starter", ""):
        suffix = f"?plan={plan}" if plan else ""
        r = client.post(f"/v1/billing/checkout{suffix}",
                        headers={"X-Workspace-Id": ws_id,
                                 "X-Workspace-Key": raw_key})
        assert r.status_code == 410, (
            f"checkout resurrected by env vars for plan={plan!r}: {r.status_code}")
        assert "buy.stripe" not in r.text


def test_the_retirement_never_answers_pay_me(env):
    """410, not 402: nothing about paying fixes a retired route, and an agent
    must not retry payment against a door that cannot grant."""
    client, we = env
    ws_id, raw_key = _fresh_creds(we)
    r = client.post("/v1/billing/checkout?plan=team",
                    headers={"X-Workspace-Id": ws_id, "X-Workspace-Key": raw_key})
    assert r.status_code != 402, (
        "402 means 'pay me' — an agent would retry payment forever against a "
        "route that can never transact")


# ── ITEM 3: the /start plan trap ────────────────────────────────────────────

def test_machine_asking_start_for_a_paid_plan_is_refused(env):
    """THE defect. Was: 200 + a silently minted FREE workspace."""
    client, _ = env
    r = client.post("/start?plan=starter", headers={"Accept": "application/json"})

    assert r.status_code == 409, (
        f"a machine asking /start for a paid plan must not get a free mint; got "
        f"{r.status_code}")
    body = r.text
    assert "plan_requires_agent_payment" in body, "the refusal must be typed"
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
    assert "/v1/billing/checkout" not in txt, (
        "the retired human-checkout route is still discoverable from llms.txt")
    assert "/v1/billing/x402" in txt, (
        "the only paid path is not discoverable from llms.txt")


def test_llms_txt_says_there_is_no_human_checkout(env):
    """The honest version: the only paid path is the agent's own rail."""
    client, _ = env
    txt = client.get("/llms.txt").text
    assert "no human checkout" in txt.lower(), (
        "llms.txt must say plainly that no human checkout exists")


def test_the_paid_path_constant_points_at_the_rail_that_transacts(env):
    """The paid path an agent is pointed at must itself behave honestly."""
    import x402_verify
    assert x402_verify.X402_PAID_PATH == "/v1/billing/x402", (
        "the documented paid path drifted off the route that can actually transact")
