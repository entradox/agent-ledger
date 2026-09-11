#!/usr/bin/env python3
"""Tests for token-volume budget caps (2026-09-10).

Dollar budget caps (monthly_cap_cents/daily_cap_cents) were always a no-op
for rail="tokens" rows, since those rows are always 0-cent bookkeeping — a
token-metered agent (flat-rate/subscription billing, no per-call dollar
amount) had no hard-cap mechanism at all. This adds an independent
token-volume cap dimension (monthly_token_cap/daily_token_cap) enforced
against tokens_in+tokens_out on rail="tokens" entries.

Also covers the /v1/track duplicate-row fix: a request whose primary rail
IS "tokens" must not additionally get a second, metadata-less "tokens" row
written behind it.

Run with: python3 -m pytest -q
"""
import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

AL_VERSION = "2026-09-01"


@pytest.fixture()
def engine(monkeypatch):
    tmp_dir = tempfile.mkdtemp(prefix="agent-ledger-test-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp_dir)
    monkeypatch.delenv("AL_PRO_ACTIVE", raising=False)

    for mod in ("ledger_engine", "metrics"):
        sys.modules.pop(mod, None)

    import ledger_engine as ledger_engine_mod
    import workspace_engine
    importlib.reload(workspace_engine)
    _, ws_key = workspace_engine.create_workspace(owner_email='engine-fixture@example.com')

    yield ledger_engine_mod, ws_key

    shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.fixture()
def client(monkeypatch):
    tmp_dir = tempfile.mkdtemp(prefix="agent-ledger-test-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp_dir)
    monkeypatch.setenv("AL_ADMIN_SECRET", "test-admin-secret")
    monkeypatch.delenv("AL_PRO_ACTIVE", raising=False)

    for mod in ("api_server", "ledger_engine", "metrics", "al_mcp_http", "routes_agents"):
        sys.modules.pop(mod, None)

    import ledger_engine as ledger_engine_mod
    import api_server as api_server_mod
    import workspace_engine
    importlib.reload(workspace_engine)
    _, ws_key = workspace_engine.create_workspace(owner_email='client-fixture@example.com')
    from fastapi.testclient import TestClient

    tc = TestClient(api_server_mod.app)
    yield tc, api_server_mod, ledger_engine_mod, ws_key

    shutil.rmtree(tmp_dir, ignore_errors=True)


def _headers(idem_key=None, version=AL_VERSION):
    h = {}
    if version is not None:
        h["AL-API-Version"] = version
    if idem_key is not None:
        h["Idempotency-Key"] = idem_key
    return h


# ── engine-level ─────────────────────────────────────────────────────────

def test_dollar_cap_does_not_block_token_rows(engine):
    """Regression for the original gap: a dollar cap must never block
    rail="tokens" bookkeeping (0-cent, exempt from the dollar check)."""
    engine, ws_key = engine
    engine.ensure_agent_secret("agent-a", workspace_key=ws_key)
    engine.set_budget("agent-a", monthly_cents=100)  # $1 dollar cap
    entry = engine.track("agent-a", "tokens", 0, "gpt-4o",
                         tokens_in=1_000_000, tokens_out=1_000_000)
    assert entry.meta["tokens_in"] == 1_000_000


def test_monthly_token_cap_blocks_over_cap(engine):
    engine, ws_key = engine
    engine.ensure_agent_secret("agent-b", workspace_key=ws_key)
    engine.set_budget("agent-b", monthly_cents=0, monthly_tokens=1000)
    engine.track("agent-b", "tokens", 0, "gpt-4o", tokens_in=600, tokens_out=0)
    with pytest.raises(engine.BudgetExceededError):
        engine.track("agent-b", "tokens", 0, "gpt-4o", tokens_in=500, tokens_out=0)


def test_daily_token_cap_blocks_over_cap(engine):
    engine, ws_key = engine
    engine.ensure_agent_secret("agent-c", workspace_key=ws_key)
    engine.set_budget("agent-c", monthly_cents=0, daily_tokens=1000)
    engine.track("agent-c", "tokens", 0, "gpt-4o", tokens_in=700, tokens_out=0)
    with pytest.raises(engine.BudgetExceededError):
        engine.track("agent-c", "tokens", 0, "gpt-4o", tokens_in=400, tokens_out=0)


def test_token_cap_exactly_at_limit_is_allowed(engine):
    engine, ws_key = engine
    engine.ensure_agent_secret("agent-d", workspace_key=ws_key)
    engine.set_budget("agent-d", monthly_cents=0, monthly_tokens=1000)
    entry = engine.track("agent-d", "tokens", 0, "gpt-4o", tokens_in=1000, tokens_out=0)
    assert entry.meta["tokens_in"] == 1000


def test_token_budget_alert_logged_on_exceed(engine):
    engine, ws_key = engine
    engine.ensure_agent_secret("agent-e", workspace_key=ws_key)
    engine.set_budget("agent-e", monthly_cents=0, monthly_tokens=1000)
    engine.track("agent-e", "tokens", 0, "gpt-4o", tokens_in=1000, tokens_out=0)
    alerts_path = engine._alerts_path("agent-e")
    assert alerts_path.exists()
    import json
    types = {json.loads(l)["type"] for l in open(alerts_path)}
    assert "token_budget_exceeded" in types


def test_report_surfaces_token_budget_status(engine):
    engine, ws_key = engine
    engine.ensure_agent_secret("agent-f", workspace_key=ws_key)
    engine.set_budget("agent-f", monthly_cents=0, monthly_tokens=1000)
    engine.track("agent-f", "tokens", 0, "gpt-4o", tokens_in=300, tokens_out=0)
    r = engine.report("agent-f")
    assert r.budget_status["monthly_token_cap"] == 1000
    assert r.budget_status["monthly_tokens_used"] == 300
    assert r.budget_status["token_exceeded"] is False


def test_no_token_cap_set_omits_token_fields_from_report(engine):
    engine, ws_key = engine
    engine.ensure_agent_secret("agent-g", workspace_key=ws_key)
    engine.set_budget("agent-g", monthly_cents=500)
    r = engine.report("agent-g")
    assert "monthly_token_cap" not in r.budget_status


def test_budget_to_dict_and_get_budget_roundtrip_token_caps(engine):
    engine, ws_key = engine
    engine.ensure_agent_secret("agent-h", workspace_key=ws_key)
    engine.set_budget("agent-h", monthly_cents=0, monthly_tokens=5000, daily_tokens=1000)
    b = engine.get_budget("agent-h")
    assert b.monthly_token_cap == 5000
    assert b.daily_token_cap == 1000


def test_old_budget_json_without_token_fields_still_loads(engine):
    """A budget.json written before this change has no monthly_token_cap/
    daily_token_cap keys — Budget(**d) must fall back to the dataclass
    defaults (0) rather than raising."""
    engine, ws_key = engine
    import json
    engine.ensure_agent_secret("agent-i", workspace_key=ws_key)
    engine._agent_dir("agent-i").mkdir(parents=True, exist_ok=True)
    old_shape = {"agent_id": "agent-i", "monthly_cap_cents": 500,
                 "daily_cap_cents": 0, "alert_threshold_pct": 80}
    json.dump(old_shape, open(engine._budget_path("agent-i"), "w"))
    b = engine.get_budget("agent-i")
    assert b.monthly_token_cap == 0
    assert b.daily_token_cap == 0


# ── API-level ────────────────────────────────────────────────────────────

def test_budget_endpoint_accepts_and_returns_token_caps(client):
    tc, _, _, ws_key = client
    r = tc.post("/v1/budget", json={"agent_id": "api-agent-1", "monthly_cents": 0,
                                    "monthly_tokens": 2000, "daily_tokens": 500,
                                    "workspace_key": ws_key},
               headers=_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["monthly_token_cap"] == 2000
    assert body["daily_token_cap"] == 500


def test_track_tokens_rail_blocked_with_402_over_cap(client):
    tc, _, _, ws_key = client
    budget = tc.post("/v1/budget", json={"agent_id": "api-agent-2", "monthly_cents": 0,
                                         "monthly_tokens": 1000,
                                         "workspace_key": ws_key}, headers=_headers())
    secret = budget.json()["agent_secret"]

    ok = tc.post("/v1/track", json={"agent_id": "api-agent-2", "rail": "tokens",
                                    "amount_cents": 0, "service": "gpt-4o",
                                    "tokens_in": 900, "tokens_out": 0,
                                    "agent_secret": secret}, headers=_headers())
    assert ok.status_code == 200

    blocked = tc.post("/v1/track", json={"agent_id": "api-agent-2", "rail": "tokens",
                                         "amount_cents": 0, "service": "gpt-4o",
                                         "tokens_in": 200, "tokens_out": 0,
                                         "agent_secret": secret}, headers=_headers())
    assert blocked.status_code == 402
    assert blocked.json()["error"]["type"] == "budget_error"


def test_tokens_rail_primary_write_does_not_duplicate_row(client):
    """/v1/track with rail="tokens" directly must write exactly one ledger
    row carrying the tokens_in/tokens_out metadata — not a metadata-less
    bookkeeping row plus a second real one behind it."""
    tc, api_server_mod, ledger_engine_mod, ws_key = client
    r = tc.post("/v1/track", json={"agent_id": "api-agent-3", "rail": "tokens",
                                   "amount_cents": 0, "service": "gpt-4o",
                                   "tokens_in": 123, "tokens_out": 45,
                                   "workspace_key": ws_key},
               headers=_headers())
    assert r.status_code == 200

    ledger_path = ledger_engine_mod._ledger_path("api-agent-3")
    lines = [l for l in open(ledger_path) if l.strip()]
    assert len(lines) == 1
    import json
    row = json.loads(lines[0])
    assert row["tokens_in"] == 123
    assert row["tokens_out"] == 45


def test_dollar_rail_with_tokens_writes_two_rows(client):
    """A dollar-rail write that also reports token burn still gets the
    dollar row plus a separate 0-cent tokens bookkeeping row (unchanged
    behavior for the non-tokens-primary case)."""
    tc, api_server_mod, ledger_engine_mod, ws_key = client
    r = tc.post("/v1/track", json={"agent_id": "api-agent-4", "rail": "manual",
                                   "amount_cents": 250, "service": "search",
                                   "tokens_in": 10, "tokens_out": 20,
                                   "workspace_key": ws_key},
               headers=_headers())
    assert r.status_code == 200

    ledger_path = ledger_engine_mod._ledger_path("api-agent-4")
    lines = [l for l in open(ledger_path) if l.strip()]
    assert len(lines) == 2
    import json
    rails = [json.loads(l)["rail"] for l in lines]
    assert rails == ["manual", "tokens"]


def test_report_endpoint_exposes_token_budget_status(client):
    tc, _, _, ws_key = client
    budget = tc.post("/v1/budget", json={"agent_id": "api-agent-5", "monthly_cents": 0,
                                         "monthly_tokens": 1000,
                                         "workspace_key": ws_key}, headers=_headers())
    secret = budget.json()["agent_secret"]
    tc.post("/v1/track", json={"agent_id": "api-agent-5", "rail": "tokens",
                               "amount_cents": 0, "service": "gpt-4o",
                               "tokens_in": 300, "tokens_out": 0,
                               "agent_secret": secret}, headers=_headers())
    r = tc.get("/v1/report/api-agent-5")
    assert r.status_code == 200
    assert r.json()["budget_status"]["monthly_tokens_used"] == 300
