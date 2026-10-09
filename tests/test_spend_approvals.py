#!/usr/bin/env python3
"""Spend-approval primitive tests (D-1575).

The chain under test: an agent asks for a one-shot permit -> the workspace
owner approves or denies -> an approved permit lets exactly one over-cap
ledger_track through and is consumed. The security property being pinned:
the agent's own agent_secret can REQUEST but can never DECIDE — a credential
that approves its own requests is not a control.
"""
import importlib
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="agent-ledger-approvals-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import workspace_engine, ledger_engine, identity, spend_policy, al_mcp_http
    for m in (workspace_engine, ledger_engine, identity, spend_policy,
              al_mcp_http):
        importlib.reload(m)
    yield al_mcp_http, workspace_engine, ledger_engine, identity, Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def rest_env(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="agent-ledger-approvals-rest-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import workspace_engine, ledger_engine, identity, routes_agents, api_server
    for m in (workspace_engine, ledger_engine, identity, routes_agents,
              api_server):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    yield TestClient(api_server.app), workspace_engine
    shutil.rmtree(tmp, ignore_errors=True)


VERSION = {"AL-API-Version": "2026-09-01"}


def _rest_claim(client, workspace_engine, agent_id="rest-agent"):
    """Mint workspace + agent via POST /v1/track; return (raw_key, secret)."""
    _, raw_key = workspace_engine.create_workspace(owner_email="r@example.com")
    r = client.post("/v1/track", json={
        "agent_id": agent_id, "rail": "api_key", "amount_cents": 1,
        "service": "setup", "workspace_key": raw_key}, headers=VERSION)
    assert r.status_code == 200, r.text
    return raw_key, r.json()["agent_secret"]


def test_rest_approval_roundtrip(rest_env):
    client, workspace_engine = rest_env
    raw_key, secret = _rest_claim(client, workspace_engine)
    r = client.post("/v1/approvals", json={
        "agent_id": "rest-agent", "amount_cents": 700,
        "service": "openai batch", "reason": "nightly eval",
        "agent_secret": secret}, headers=VERSION)
    assert r.status_code == 200, r.text
    apr = r.json()["approval_id"]
    # owner sees it pending in the workspace-wide list
    l = client.get("/v1/approvals", headers={"x-workspace-key": raw_key})
    assert any(p["approval_id"] == apr for p in l.json()["approvals"])
    # owner approves
    d = client.post(f"/v1/approvals/{apr}/decide",
                    json={"agent_id": "rest-agent", "decision": "approve"},
                    headers={**VERSION, "x-workspace-key": raw_key})
    assert d.status_code == 200, d.text
    assert d.json()["state"] == "approved"


def test_rest_decide_rejects_agent_secret(rest_env):
    client, workspace_engine = rest_env
    raw_key, secret = _rest_claim(client, workspace_engine)
    r = client.post("/v1/approvals", json={
        "agent_id": "rest-agent", "amount_cents": 700, "service": "svc",
        "agent_secret": secret}, headers=VERSION)
    apr = r.json()["approval_id"]
    # agent's own secret as the decision credential — refused
    d = client.post(f"/v1/approvals/{apr}/decide",
                    json={"agent_id": "rest-agent", "decision": "approve",
                          "workspace_key": secret},
                    headers=VERSION)
    assert d.status_code == 401


def test_rest_approvals_requires_version_header(rest_env):
    client, workspace_engine = rest_env
    raw_key, _ = _rest_claim(client, workspace_engine)
    r = client.post("/v1/approvals", json={
        "agent_id": "rest-agent", "amount_cents": 700, "service": "svc",
        "workspace_key": raw_key})
    assert r.status_code == 400


def _setup(env, agent_id="agent-a"):
    """Mint a workspace + claimed agent; return (raw_key, agent_secret)."""
    al_mcp_http, workspace_engine, *_ = env
    _, raw_key = workspace_engine.create_workspace(owner_email="o@example.com")
    r = al_mcp_http.ledger_track(agent_id=agent_id, rail="api_key",
                                 amount_cents=1, service="setup",
                                 workspace_key=raw_key)
    assert "error" not in r
    return raw_key, r["agent_secret"]


# --- request ---------------------------------------------------------------

def test_request_approval_creates_pending_permit(env):
    al_mcp_http, *_ = env
    raw_key, secret = _setup(env)
    r = al_mcp_http.ledger_request_approval(
        agent_id="agent-a", amount_cents=500, service="openai:gpt-5 batch",
        reason="eval run", agent_secret=secret)
    assert r["state"] == "pending"
    assert r["approval_id"].startswith("apr_")
    assert r["amount_cents"] == 500
    assert r["workspace_id"]


def test_request_approval_requires_agent_credential(env):
    al_mcp_http, *_ = env
    _setup(env)
    r = al_mcp_http.ledger_request_approval(
        agent_id="agent-a", amount_cents=500, service="svc")
    assert r.get("error_code") == "agent_secret_mismatch"


def test_request_approval_rejects_bad_amount(env):
    al_mcp_http, *_ = env
    _, secret = _setup(env)
    r = al_mcp_http.ledger_request_approval(
        agent_id="agent-a", amount_cents=0, service="svc", agent_secret=secret)
    assert r.get("error_code") == "invalid_request"


# --- listing / scoping -----------------------------------------------------

def test_owner_lists_pending_across_workspace(env):
    al_mcp_http, *_ = env
    raw_key, secret = _setup(env)
    al_mcp_http.ledger_request_approval(
        agent_id="agent-a", amount_cents=500, service="svc",
        agent_secret=secret)
    r = al_mcp_http.ledger_approvals(workspace_key=raw_key, state="pending")
    assert len(r["approvals"]) == 1
    assert r["approvals"][0]["state"] == "pending"


def test_other_workspace_cannot_list_or_decide(env):
    al_mcp_http, workspace_engine, *_ = env
    raw_key, secret = _setup(env)
    _, other_key = workspace_engine.create_workspace(owner_email="x@example.com")
    r = al_mcp_http.ledger_request_approval(
        agent_id="agent-a", amount_cents=500, service="svc",
        agent_secret=secret)
    # other workspace's key resolves but does not own agent-a
    d = al_mcp_http.ledger_approval_decide(
        agent_id="agent-a", approval_id=r["approval_id"], decision="approve",
        workspace_key=other_key)
    assert d.get("error_code") == "not_your_agent"
    # and its workspace-wide listing is empty of agent-a's permit
    l = al_mcp_http.ledger_approvals(workspace_key=other_key)
    assert l["approvals"] == []


# --- decide: the no-self-approval property ----------------------------------

def test_agent_secret_cannot_decide(env):
    al_mcp_http, *_ = env
    _, secret = _setup(env)
    r = al_mcp_http.ledger_request_approval(
        agent_id="agent-a", amount_cents=500, service="svc",
        agent_secret=secret)
    d = al_mcp_http.ledger_approval_decide(
        agent_id="agent-a", approval_id=r["approval_id"], decision="approve",
        workspace_key="")  # no workspace_key at all
    assert d.get("error_code") == "workspace_key_required"


def test_decide_rejects_bad_decision_and_redecide(env):
    al_mcp_http, *_ = env
    raw_key, secret = _setup(env)
    r = al_mcp_http.ledger_request_approval(
        agent_id="agent-a", amount_cents=500, service="svc",
        agent_secret=secret)
    bad = al_mcp_http.ledger_approval_decide(
        agent_id="agent-a", approval_id=r["approval_id"], decision="maybe",
        workspace_key=raw_key)
    assert bad.get("error_code") == "invalid_request"
    ok = al_mcp_http.ledger_approval_decide(
        agent_id="agent-a", approval_id=r["approval_id"], decision="approve",
        workspace_key=raw_key)
    assert ok["state"] == "approved"
    again = al_mcp_http.ledger_approval_decide(
        agent_id="agent-a", approval_id=r["approval_id"], decision="deny",
        workspace_key=raw_key)
    assert again.get("error_code") == "invalid_request"


# --- enforcement: permit consumes on the write path -------------------------

def test_approved_permit_lets_over_cap_track_through_once(env):
    al_mcp_http, _, ledger_engine, _, _ = env
    raw_key, secret = _setup(env)
    al_mcp_http.ledger_set_budget(agent_id="agent-a", monthly_cents=100,
                                  agent_secret=secret)
    r = al_mcp_http.ledger_request_approval(
        agent_id="agent-a", amount_cents=500, service="svc",
        agent_secret=secret)
    al_mcp_http.ledger_approval_decide(
        agent_id="agent-a", approval_id=r["approval_id"], decision="approve",
        workspace_key=raw_key)
    # $2 spend over a $1 cap — blocked without the permit
    entry = ledger_engine.track("agent-a", "api_key", 200, "svc")
    assert entry.amount_cents == 200
    permits = ledger_engine.list_approvals("agent-a", state="consumed")
    assert len(permits) == 1
    # second over-cap write has no permit left — refused
    with pytest.raises(ledger_engine.BudgetExceededError):
        ledger_engine.track("agent-a", "api_key", 200, "svc")


def test_denied_permit_does_not_rescue(env):
    al_mcp_http, _, ledger_engine, _, _ = env
    raw_key, secret = _setup(env)
    al_mcp_http.ledger_set_budget(agent_id="agent-a", monthly_cents=100,
                                  agent_secret=secret)
    r = al_mcp_http.ledger_request_approval(
        agent_id="agent-a", amount_cents=500, service="svc",
        agent_secret=secret)
    al_mcp_http.ledger_approval_decide(
        agent_id="agent-a", approval_id=r["approval_id"], decision="deny",
        workspace_key=raw_key)
    with pytest.raises(ledger_engine.BudgetExceededError):
        ledger_engine.track("agent-a", "api_key", 200, "svc")


def test_expired_pending_permit_does_not_rescue(env):
    al_mcp_http, _, ledger_engine, _, tmp = env
    raw_key, secret = _setup(env)
    al_mcp_http.ledger_set_budget(agent_id="agent-a", monthly_cents=100,
                                  agent_secret=secret)
    permit = ledger_engine.request_approval("agent-a", 500, "svc")
    # age it past the TTL by rewriting expires_at
    permit["expires_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)
                            ).isoformat()
    ledger_engine._write_permit("agent-a", permit)
    with pytest.raises(ledger_engine.BudgetExceededError):
        ledger_engine.track("agent-a", "api_key", 200, "svc")
    assert ledger_engine.list_approvals("agent-a", state="expired")


def test_permit_does_not_cover_larger_amount(env):
    al_mcp_http, _, ledger_engine, _, _ = env
    raw_key, secret = _setup(env)
    al_mcp_http.ledger_set_budget(agent_id="agent-a", monthly_cents=100,
                                  agent_secret=secret)
    r = al_mcp_http.ledger_request_approval(
        agent_id="agent-a", amount_cents=150, service="svc",
        agent_secret=secret)
    al_mcp_http.ledger_approval_decide(
        agent_id="agent-a", approval_id=r["approval_id"], decision="approve",
        workspace_key=raw_key)
    # $2 > the approved $1.50 — refused, and the permit is NOT consumed
    with pytest.raises(ledger_engine.BudgetExceededError):
        ledger_engine.track("agent-a", "api_key", 200, "svc")
    assert ledger_engine.list_approvals("agent-a", state="approved")


def test_under_cap_track_does_not_consume_permit(env):
    al_mcp_http, _, ledger_engine, _, _ = env
    raw_key, secret = _setup(env)
    al_mcp_http.ledger_set_budget(agent_id="agent-a", monthly_cents=10_000,
                                  agent_secret=secret)
    r = al_mcp_http.ledger_request_approval(
        agent_id="agent-a", amount_cents=500, service="svc",
        agent_secret=secret)
    al_mcp_http.ledger_approval_decide(
        agent_id="agent-a", approval_id=r["approval_id"], decision="approve",
        workspace_key=raw_key)
    ledger_engine.track("agent-a", "api_key", 50, "svc")  # under cap
    # permit must survive — it was never needed
    assert ledger_engine.list_approvals("agent-a", state="approved")


# --- the owner hears about it: alert fan-out --------------------------------

def test_request_and_decide_emit_alerts(env):
    al_mcp_http, _, ledger_engine, _, _ = env
    raw_key, secret = _setup(env)
    r = al_mcp_http.ledger_request_approval(
        agent_id="agent-a", amount_cents=500, service="svc",
        reason="eval", agent_secret=secret)
    feed = ledger_engine._alerts_path("agent-a").read_text()
    assert '"type": "approval_requested"' in feed
    assert r["approval_id"] in feed
    al_mcp_http.ledger_approval_decide(
        agent_id="agent-a", approval_id=r["approval_id"], decision="approve",
        workspace_key=raw_key)
    feed = ledger_engine._alerts_path("agent-a").read_text()
    assert '"type": "approval_approved"' in feed


def test_approval_alert_types_map_to_events(env):
    import alert_delivery
    assert alert_delivery.event_for("approval_requested") == "approval.requested"
    assert alert_delivery.event_for("approval_approved") == "approval.decided"
    assert alert_delivery.event_for("approval_denied") == "approval.decided"
    assert "approval.requested" in alert_delivery.EVENTS
    assert "approval.decided" in alert_delivery.EVENTS


# --- check_spend surfaces the lane ------------------------------------------

def test_check_spend_reports_approved_permit_and_hint(env):
    _, _, _, _, _ = env
    al_mcp_http, workspace_engine, ledger_engine, identity, tmp = env
    raw_key, secret = _setup(env)
    al_mcp_http.ledger_set_budget(agent_id="agent-a", monthly_cents=100,
                                  agent_secret=secret)
    # denied check carries the escape-hatch hint
    import spend_policy
    denied = spend_policy.check_spend("agent-a", amount_cents=500)
    assert denied["allowed"] is False
    assert "ledger_request_approval" in denied["message"]
    r = al_mcp_http.ledger_request_approval(
        agent_id="agent-a", amount_cents=500, service="svc",
        agent_secret=secret)
    al_mcp_http.ledger_approval_decide(
        agent_id="agent-a", approval_id=r["approval_id"], decision="approve",
        workspace_key=raw_key)
    covering = spend_policy.check_spend("agent-a", amount_cents=500)
    assert covering["allowed"] is True
    assert covering["reason"] == "approved_permit"
    assert covering["approvals"]["approved_covering"] == r["approval_id"]
