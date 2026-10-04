#!/usr/bin/env python3
"""The RapidAPI marketplace rail: X-RapidAPI-Proxy-Secret proves origin,
X-RapidAPI-User names the subscriber → resolve/mint their workspace and
inject its workspace_key so every existing gate works unchanged.

Canary rules applied: every positive test has a negative proving the
credential actually gates (wrong secret, no secret env, hostile
client-supplied key) — a rail that always lets traffic through is not a
rail.
"""
import json
import shutil
import tempfile
from types import SimpleNamespace

import pytest

PROXY_SECRET = "test-proxy-secret-0123456789abcdef"
API_VERSION_HEADERS = {"AL-API-Version": "2026-09-01"}


def _rapidapi_headers(user="rapid-user-1", subscription=None, secret=PROXY_SECRET):
    h = {"X-RapidAPI-Proxy-Secret": secret, "X-RapidAPI-User": user}
    if subscription is not None:
        h["X-RapidAPI-Subscription"] = subscription
    return h


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    monkeypatch.setenv("AL_RAPIDAPI_PROXY_SECRET", PROXY_SECRET)
    monkeypatch.setenv("AL_RAPIDAPI_PAID_PLANS", "pro,mega")
    import importlib
    import workspace_engine, ledger_engine, identity, routes_agents, routes_billing, api_server
    importlib.reload(workspace_engine)
    importlib.reload(ledger_engine)
    importlib.reload(identity)
    importlib.reload(routes_agents)
    importlib.reload(routes_billing)
    importlib.reload(api_server)
    from fastapi.testclient import TestClient
    yield SimpleNamespace(client=TestClient(api_server.app),
                          workspace_engine=workspace_engine)
    shutil.rmtree(tmp, ignore_errors=True)


def _track(env, agent_id="ra-agent-1", amount=5, extra_headers=None, body=None):
    payload = {"agent_id": agent_id, "rail": "manual", "amount_cents": amount}
    if body:
        payload.update(body)
    return env.client.post("/v1/track", json=payload,
                           headers={**API_VERSION_HEADERS, **(extra_headers or {})})


def test_first_rapidapi_call_mints_workspace_and_the_write_lands(env):
    r = _track(env, extra_headers=_rapidapi_headers("user-a"))
    assert r.status_code == 200, r.text
    ws = env.workspace_engine.get_workspace_by_rapidapi_user("user-a")
    assert ws is not None, "no workspace minted for the rapidapi user"
    assert ws["plan"] == "free"          # no scarcity grant on this rail
    assert ws["agent_cap"] == env.workspace_engine.WORKSPACE_FREE_AGENT_CAP


def test_same_rapidapi_user_lands_in_the_same_workspace(env):
    _track(env, "agent-1", extra_headers=_rapidapi_headers("user-a"))
    _track(env, "agent-2", extra_headers=_rapidapi_headers("user-a"))
    ws = env.workspace_engine.get_workspace_by_rapidapi_user("user-a")
    others = [w for w in env.workspace_engine.iter_workspaces()
              if w["workspace_id"] != ws["workspace_id"]]
    assert others == [], f"second call minted a duplicate: {[w['workspace_id'] for w in others]}"


def test_two_rapidapi_users_get_two_workspaces(env):
    _track(env, "agent-1", extra_headers=_rapidapi_headers("user-a"))
    _track(env, "agent-1", extra_headers=_rapidapi_headers("user-b"))
    a = env.workspace_engine.get_workspace_by_rapidapi_user("user-a")
    b = env.workspace_engine.get_workspace_by_rapidapi_user("user-b")
    assert a["workspace_id"] != b["workspace_id"]


def test_wrong_proxy_secret_is_inert(env):
    r = _track(env, extra_headers=_rapidapi_headers("user-a", secret="wrong"))
    assert r.status_code == 401, r.text
    assert env.workspace_engine.get_workspace_by_rapidapi_user("user-a") is None


def test_no_secret_env_means_headers_do_nothing(env, monkeypatch):
    monkeypatch.delenv("AL_RAPIDAPI_PROXY_SECRET")
    r = _track(env, extra_headers=_rapidapi_headers("user-a"))
    assert r.status_code == 401, r.text


def test_client_supplied_workspace_key_is_replaced_not_trusted(env):
    # A hostile (or merely curious) subscriber pasting someone else's key in
    # must not reach another workspace: the proxy secret is the authority.
    r = _track(env, extra_headers={
        **_rapidapi_headers("user-a"), "X-Workspace-Key": "wk_live_forged"})
    assert r.status_code == 200, r.text
    ws = env.workspace_engine.get_workspace_by_rapidapi_user("user-a")
    from ledger_engine import DATA_DIR
    assert (DATA_DIR / "agents" / "ra-agent-1" / "workspace_id.txt").read_text().strip() \
        == ws["workspace_id"]


def test_paid_subscription_header_grants_starter(env):
    _track(env, extra_headers=_rapidapi_headers("user-a", subscription="PRO"))
    ws = env.workspace_engine.get_workspace_by_rapidapi_user("user-a")
    assert ws["plan"] == "starter"
    assert ws["agent_cap"] == env.workspace_engine.STARTER_AGENT_CAP
    assert ws["stripe_customer_id"] == "rapidapi:user-a"
    assert ws["pro_period_source"] == "rapidapi"


def test_cancelled_subscription_drops_only_the_rapidapi_granted_tier(env):
    _track(env, extra_headers=_rapidapi_headers("user-a", subscription="PRO"))
    ws = env.workspace_engine.get_workspace_by_rapidapi_user("user-a")
    assert ws["plan"] == "starter"
    _track(env, "agent-2",
           extra_headers=_rapidapi_headers("user-a", subscription="BASIC"))
    ws = env.workspace_engine.get_workspace_by_rapidapi_user("user-a")
    assert ws["plan"] == "free"
    assert ws["agent_cap"] == env.workspace_engine.WORKSPACE_FREE_AGENT_CAP


def test_stripe_paid_workspace_is_never_demoted_by_a_marketplace_header(env):
    workspace_id, _ = env.workspace_engine.create_workspace(
        rapidapi_user="user-stripe", grant_scarcity=False)
    env.workspace_engine.mark_pro(workspace_id, stripe_customer_id="cus_real",
                                  period_source="stripe", tier="team")
    _track(env, extra_headers=_rapidapi_headers("user-stripe", subscription="BASIC"))
    ws = env.workspace_engine.get_workspace(workspace_id)
    assert ws["plan"] == "team"  # a settled payment may only raise a tier


def test_injected_key_reaches_reads_and_budget_writes(env):
    r = _track(env, "agent-1", extra_headers=_rapidapi_headers("user-a"))
    assert r.status_code == 200, r.text
    agent_secret = r.json().get("agent_secret")
    assert agent_secret, "claim response did not return the agent_secret"
    # The injected workspace_key authorizes the READ path by itself...
    r = env.client.get("/v1/report/agent-1", headers=_rapidapi_headers("user-a"))
    assert r.status_code == 200, r.text
    # ...while writes to an already-claimed agent still need its agent_secret
    # (the product's own rule, unchanged by this rail): the caller carries it
    # in the body exactly like a native caller does.
    r = env.client.post("/v1/budget",
                        json={"agent_id": "agent-1", "monthly_cents": 500,
                              "agent_secret": agent_secret},
                        headers={**API_VERSION_HEADERS, **_rapidapi_headers("user-a")})
    assert r.status_code == 200, r.text


def test_missing_user_header_falls_through_unmodified(env):
    r = _track(env, extra_headers={"X-RapidAPI-Proxy-Secret": PROXY_SECRET})
    assert r.status_code == 401, r.text  # valid secret, no user → no rail


def test_verified_gateway_call_needs_no_al_api_version(env):
    # The Rapid Runtime never sends AL-API-Version and a marketplace
    # subscriber cannot be asked to. Once the proxy secret verifies, the
    # middleware pins the version itself — without this, every subscriber
    # call dies on version_header and the rail is dead on arrival.
    r = env.client.post("/v1/track",
                        json={"agent_id": "no-version-agent",
                              "rail": "manual", "amount_cents": 5},
                        headers=_rapidapi_headers("user-nov"))
    assert r.status_code == 200, r.text
    assert env.workspace_engine.get_workspace_by_rapidapi_user("user-nov") is not None


def test_unverified_caller_still_needs_al_api_version(env):
    r = env.client.post("/v1/track",
                        json={"agent_id": "plain-agent",
                              "rail": "manual", "amount_cents": 5})
    assert r.status_code == 400, r.text
