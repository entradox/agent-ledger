#!/usr/bin/env python3
"""Smoke tests for the owner telemetry layer (metrics.py + api_server.py wiring).

Run with: /opt/miniconda3/bin/python3 -m pytest -q test_telemetry_smoke.py
"""
import importlib
import os
import shutil
import sys
import tempfile

import pytest


@pytest.fixture()
def client(monkeypatch):
    tmp_dir = tempfile.mkdtemp(prefix="agent-ledger-test-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp_dir)
    monkeypatch.setenv("AL_ADMIN_SECRET", "test-admin-secret")
    monkeypatch.delenv("AL_PRO_ACTIVE", raising=False)

    # Fresh module state per test — these modules cache DATA_DIR and in-memory
    # counters at import time, so drop any previously-imported copies.
    for mod in ("api_server", "ledger_engine", "metrics", "al_mcp_http"):
        sys.modules.pop(mod, None)

    import metrics as metrics_mod
    import api_server as api_server_mod
    from fastapi.testclient import TestClient

    tc = TestClient(api_server_mod.app)
    yield tc, api_server_mod, metrics_mod

    shutil.rmtree(tmp_dir, ignore_errors=True)


def test_middleware_records_http_events(client):
    tc, api_server_mod, metrics_mod = client
    r1 = tc.get("/health")
    assert r1.status_code == 200
    r2 = tc.get("/stats")
    assert r2.status_code == 200
    snap = metrics_mod.snapshot()
    assert snap["totals"].get("http", 0) >= 2


def test_wrong_secret_records_auth_fail_and_401(client):
    tc, api_server_mod, metrics_mod = client
    body = {"agent_id": "auth-test-agent", "rail": "manual", "amount_cents": 100,
            "service": "svc"}
    first = tc.post("/v1/track", json=body)
    assert first.status_code == 200

    body_bad = dict(body, agent_secret="totally-wrong-secret")
    second = tc.post("/v1/track", json=body_bad)
    assert second.status_code == 401
    snap = metrics_mod.snapshot()
    assert snap["totals"].get("auth_fail", 0) >= 1


def test_fourth_new_agent_over_cap_records_cap_blocked(client):
    tc, api_server_mod, metrics_mod = client
    from ledger_engine import BETA_AGENT_CAP

    for i in range(BETA_AGENT_CAP):
        body = {"agent_id": f"cap-agent-{i}", "rail": "manual", "amount_cents": 10,
                "service": "svc"}
        r = tc.post("/v1/track", json=body)
        assert r.status_code == 200

    body_over = {"agent_id": "cap-agent-over", "rail": "manual", "amount_cents": 10,
                 "service": "svc"}
    r_over = tc.post("/v1/track", json=body_over)
    assert r_over.status_code == 402
    snap = metrics_mod.snapshot()
    assert snap["totals"].get("cap_blocked", 0) >= 1


def test_v1_metrics_requires_admin_header(client):
    tc, api_server_mod, metrics_mod = client
    no_auth = tc.get("/v1/metrics")
    assert no_auth.status_code == 401

    with_auth = tc.get("/v1/metrics", headers={"x-al-admin": "test-admin-secret"})
    assert with_auth.status_code == 200
    data = with_auth.json()
    assert "funnel" in data
    for key in ("track_ok", "auth_fail", "cap_blocked", "validation_fail",
                "budget_set", "mcp_call"):
        assert key in data["funnel"]
    assert "checkout_funnel" in data
    assert "reach" in data
    assert "agents" in data


def test_squatted_slot_appears_with_has_data_false(client):
    tc, api_server_mod, metrics_mod = client
    from ledger_engine import _agent_dir

    agent_dir = _agent_dir("squatted-agent")
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "secret.txt").write_text("fake-secret-value")

    r = tc.get("/v1/agents", headers={"x-al-admin": "test-admin-secret"})
    assert r.status_code == 200
    agents = {a["agent_id"]: a for a in r.json()["agents"]}
    assert "squatted-agent" in agents
    assert agents["squatted-agent"]["has_data"] is False
