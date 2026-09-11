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
    import importlib
    import workspace_engine
    importlib.reload(workspace_engine)
    _, ws_key = workspace_engine.create_workspace(owner_email='smoke-fixture@example.com')
    from fastapi.testclient import TestClient

    tc = TestClient(api_server_mod.app)
    yield tc, api_server_mod, metrics_mod, ws_key

    shutil.rmtree(tmp_dir, ignore_errors=True)


def test_middleware_records_http_events(client):
    tc, api_server_mod, metrics_mod, ws_key = client
    r1 = tc.get("/health")
    assert r1.status_code == 200
    r2 = tc.get("/stats")
    assert r2.status_code == 200
    snap = metrics_mod.snapshot()
    assert snap["totals"].get("http", 0) >= 2


def test_wrong_secret_records_auth_fail_and_401(client):
    tc, api_server_mod, metrics_mod, ws_key = client
    body = {"agent_id": "auth-test-agent", "rail": "manual", "amount_cents": 100,
            "service": "svc", "workspace_key": ws_key}
    first = tc.post("/v1/track", json=body, headers={"AL-API-Version": "2026-09-01"})
    assert first.status_code == 200

    body_bad = dict(body, agent_secret="totally-wrong-secret")
    second = tc.post("/v1/track", json=body_bad, headers={"AL-API-Version": "2026-09-01"})
    assert second.status_code == 401
    snap = metrics_mod.snapshot()
    assert snap["totals"].get("auth_fail", 0) >= 1


def test_fourth_new_agent_over_cap_records_cap_blocked(client):
    tc, api_server_mod, metrics_mod, ws_key = client
    import workspace_engine

    # a fresh free-tier workspace (agent_cap = WORKSPACE_FREE_AGENT_CAP):
    # WORKSPACE_SCARCITY_CAP = 0 keeps it out of the launch "pro scarcity"
    # window, same as tests/test_workspace_claim_gate.py
    workspace_engine.WORKSPACE_SCARCITY_CAP = 0
    _, cap_ws_key = workspace_engine.create_workspace(owner_email='cap-test@example.com')

    for i in range(workspace_engine.WORKSPACE_FREE_AGENT_CAP):
        body = {"agent_id": f"cap-agent-{i}", "rail": "manual", "amount_cents": 10,
                "service": "svc", "workspace_key": cap_ws_key}
        r = tc.post("/v1/track", json=body, headers={"AL-API-Version": "2026-09-01"})
        assert r.status_code == 200

    body_over = {"agent_id": "cap-agent-over", "rail": "manual", "amount_cents": 10,
                 "service": "svc", "workspace_key": cap_ws_key}
    r_over = tc.post("/v1/track", json=body_over, headers={"AL-API-Version": "2026-09-01"})
    assert r_over.status_code == 402
    snap = metrics_mod.snapshot()
    assert snap["totals"].get("cap_blocked", 0) >= 1


def test_v1_metrics_requires_admin_header(client):
    tc, api_server_mod, metrics_mod, ws_key = client
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
    tc, api_server_mod, metrics_mod, ws_key = client
    from ledger_engine import _agent_dir

    agent_dir = _agent_dir("squatted-agent")
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "secret.txt").write_text("fake-secret-value")

    r = tc.get("/v1/agents", headers={"x-al-admin": "test-admin-secret"})
    assert r.status_code == 200
    agents = {a["agent_id"]: a for a in r.json()["agents"]}
    assert "squatted-agent" in agents
    assert agents["squatted-agent"]["has_data"] is False


def test_billing_path_email_never_reaches_metrics(client):
    """Pins the Morgan-review privacy rule: GET /v1/billing/{email} must never
    leak the raw email into the metrics event log."""
    tc, api_server_mod, metrics_mod, ws_key = client

    r = tc.get("/v1/billing/customer@example.com")
    # endpoint may 404/401/200 depending on billing state — the status doesn't
    # matter; what matters is that no metric event ever contains the email.
    snap = metrics_mod.snapshot()
    assert snap["totals"].get("http", 0) >= 1  # middleware did record the hit

    events_file = os.path.join(os.environ["AGENT_LEDGER_DATA"], "metrics.jsonl")
    with open(events_file) as f:
        raw = f.read()
    assert "customer@example.com" not in raw
    assert "@" not in raw.replace("\\u0040", "")  # no encoded emails either


def test_ip_hash_refuses_unsalted(monkeypatch):
    """No AL_METRICS_SALT → no ip_hash at all (unsalted hash = reversible)."""
    monkeypatch.delenv("AL_METRICS_SALT", raising=False)
    for mod in ("api_server", "ledger_engine", "metrics", "al_mcp_http"):
        sys.modules.pop(mod, None)
    import api_server as api_server_mod

    class FakeRequest:
        class client:
            host = "203.0.113.7"

    assert api_server_mod._ip_hash(FakeRequest()) is None

    monkeypatch.setenv("AL_METRICS_SALT", "some-secret-salt-value")
    sys.modules.pop("api_server", None)
    import importlib
    api_server_mod = importlib.import_module("api_server")
    h = api_server_mod._ip_hash(FakeRequest())
    assert h is not None and len(h) == 12
    sys.modules.pop("api_server", None)
