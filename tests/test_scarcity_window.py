#!/usr/bin/env python3
"""Tests for v0.3.1 item 1 — scarcity window (first 50 claims get Pro free
for 1 year).

Covers: 51st claim gets no pro_until (regression for the first 50), a pro
agent can keep writing after the free-tier beta cap is otherwise exhausted,
and scarcity_claims_left is accurate and PII-free.

Run with: /opt/miniconda3/bin/python3 -m pytest -q
"""
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
    """Fresh ledger_engine module state, isolated data dir — no FastAPI, for
    tests that only need the engine layer."""
    tmp_dir = tempfile.mkdtemp(prefix="agent-ledger-test-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp_dir)
    monkeypatch.delenv("AL_PRO_ACTIVE", raising=False)

    for mod in ("ledger_engine", "metrics"):
        sys.modules.pop(mod, None)

    import ledger_engine as ledger_engine_mod

    yield ledger_engine_mod

    shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.fixture()
def client(monkeypatch):
    tmp_dir = tempfile.mkdtemp(prefix="agent-ledger-test-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp_dir)
    monkeypatch.setenv("AL_ADMIN_SECRET", "test-admin-secret")
    monkeypatch.delenv("AL_PRO_ACTIVE", raising=False)

    for mod in ("api_server", "ledger_engine", "metrics", "al_mcp_http"):
        sys.modules.pop(mod, None)

    import ledger_engine as ledger_engine_mod
    import api_server as api_server_mod
    from fastapi.testclient import TestClient

    tc = TestClient(api_server_mod.app)
    yield tc, api_server_mod, ledger_engine_mod

    shutil.rmtree(tmp_dir, ignore_errors=True)


def _headers(version=AL_VERSION):
    return {"AL-API-Version": version} if version is not None else {}


def test_first_50_claims_get_pro_until(engine):
    le = engine
    for i in range(50):
        agent_id = f"scarcity-agent-{i}"
        secret, created = le.ensure_agent_secret(agent_id, check_cap=False)
        assert created
        info = le.is_pro(agent_id)
        assert info["is_pro"] is True
        assert info["plan"] == "pro_scarcity"
        assert info["pro_until"] is not None
        assert info["pro_until"] > le._time.time()
    assert le.scarcity_claims_left() == 0


def test_51st_claim_gets_no_pro_until(engine):
    le = engine
    for i in range(50):
        le.ensure_agent_secret(f"scarcity-agent-{i}", check_cap=False)
    assert le.scarcity_claims_left() == 0

    secret, created = le.ensure_agent_secret("scarcity-agent-50", check_cap=False)
    assert created
    info = le.is_pro("scarcity-agent-50")
    assert info["is_pro"] is False
    assert info["plan"] == "free"
    assert info["pro_until"] is None
    # exhausting the window doesn't go negative on further claims
    assert le.scarcity_claims_left() == 0


def test_pro_agent_beyond_free_cap_can_still_write(engine, monkeypatch):
    le = engine
    monkeypatch.setattr(le, "BETA_AGENT_CAP", 1)

    secret, created = le.ensure_agent_secret("pro-agent-1")
    assert created
    assert le.is_pro("pro-agent-1")["plan"] == "pro_scarcity"

    # free-tier cap (1) is now reached — a genuinely new agent is rejected
    with pytest.raises(le.BetaCapExceededError):
        le.ensure_agent_secret("brand-new-free-agent")

    # but the already-claimed pro agent can keep authenticating and writing
    secret2, created2 = le.ensure_agent_secret("pro-agent-1", secret)
    assert created2 is False
    assert secret2 == secret
    entry = le.track("pro-agent-1", "manual", 500, "svc")
    assert entry.amount_cents == 500


def test_scarcity_claims_left_accurate(engine):
    le = engine
    assert le.scarcity_claims_left() == 50
    for i in range(5):
        le.ensure_agent_secret(f"agent-{i}", check_cap=False)
    assert le.scarcity_claims_left() == 45
    for i in range(5, 50):
        le.ensure_agent_secret(f"agent-{i}", check_cap=False)
    assert le.scarcity_claims_left() == 0


def test_report_and_agent_listing_expose_plan_no_pii_beyond_agent_id(client):
    tc, api_server_mod, ledger_engine_mod = client
    body = {"agent_id": "plan-report-agent", "rail": "manual", "amount_cents": 100,
            "service": "svc"}
    r = tc.post("/v1/track", json=body, headers=_headers())
    assert r.status_code == 200

    report_resp = tc.get("/v1/report/plan-report-agent")
    assert report_resp.status_code == 200
    payload = report_resp.json()
    assert payload["plan"] == "pro_scarcity"
    assert payload["pro_until"] is not None

    stats_resp = tc.get("/stats")
    assert stats_resp.status_code == 200
    stats_payload = stats_resp.json()
    assert "scarcity_claims_left" in stats_payload
    assert stats_payload["scarcity_claims_left"] == 49
    # aggregate count only — never the agent_id or a secret
    assert "plan-report-agent" not in stats_resp.text
    assert "@" not in stats_resp.text
