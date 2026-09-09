#!/usr/bin/env python3
"""Tests for launch-kit v0.3 item 1 — API hardening.

Covers: Idempotency-Key (cached replay, concurrent in-flight conflict,
oversized key rejection) and the AL-API-Version header gate on POST
endpoints.

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


def _headers(idem_key=None, version=AL_VERSION):
    h = {}
    if version is not None:
        h["AL-API-Version"] = version
    if idem_key is not None:
        h["Idempotency-Key"] = idem_key
    return h


def test_same_idempotency_key_returns_cached_response(client):
    tc, api_server_mod, ledger_engine_mod = client
    body = {"agent_id": "idem-agent-1", "rail": "manual", "amount_cents": 100,
            "service": "svc"}

    first = tc.post("/v1/track", json=body, headers=_headers(idem_key="key-abc"))
    assert first.status_code == 200
    first_json = first.json()

    second = tc.post("/v1/track", json=body, headers=_headers(idem_key="key-abc"))
    assert second.status_code == 200
    assert second.json() == first_json

    # the write was never re-executed — only one ledger line for this agent
    ledger_path = ledger_engine_mod._ledger_path("idem-agent-1")
    lines = ledger_path.read_text().splitlines()
    assert len(lines) == 1


def test_concurrent_same_key_gets_409(client):
    tc, api_server_mod, ledger_engine_mod = client
    agent_id = "idem-agent-2"

    # simulate another in-flight request holding the same key before this
    # one is dispatched — idempotency_begin() inserts a NULL-response row.
    status, _ = ledger_engine_mod.idempotency_begin("key-inflight", agent_id, "track")
    assert status == "proceed"

    body = {"agent_id": agent_id, "rail": "manual", "amount_cents": 50, "service": "svc"}
    resp = tc.post("/v1/track", json=body, headers=_headers(idem_key="key-inflight"))
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["type"] == "conflict_error"
    assert err["code"] == "idempotency_conflict"


def test_idempotency_key_over_255_chars_gets_400(client):
    tc, api_server_mod, ledger_engine_mod = client
    body = {"agent_id": "idem-agent-3", "rail": "manual", "amount_cents": 50, "service": "svc"}
    long_key = "x" * 256

    resp = tc.post("/v1/track", json=body, headers=_headers(idem_key=long_key))
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["type"] == "invalid_request_error"
    assert err["code"] == "idempotency_key_too_long"


def test_missing_version_header_gets_400(client):
    tc, api_server_mod, ledger_engine_mod = client
    body = {"agent_id": "version-agent-1", "rail": "manual", "amount_cents": 50, "service": "svc"}

    resp = tc.post("/v1/track", json=body, headers=_headers(version=None))
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "version_header"


def test_invalid_version_header_gets_400(client):
    tc, api_server_mod, ledger_engine_mod = client
    body = {"agent_id": "version-agent-2", "rail": "manual", "amount_cents": 50, "service": "svc"}

    resp = tc.post("/v1/track", json=body, headers=_headers(version="2020-01-01"))
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "version_header"


def test_valid_version_header_passes(client):
    tc, api_server_mod, ledger_engine_mod = client
    body = {"agent_id": "version-agent-3", "rail": "manual", "amount_cents": 50, "service": "svc"}

    resp = tc.post("/v1/track", json=body, headers=_headers())
    assert resp.status_code == 200


def test_budget_endpoint_also_enforces_version_and_idempotency(client):
    tc, api_server_mod, ledger_engine_mod = client
    body = {"agent_id": "budget-agent-1", "monthly_cents": 1000}

    missing_version = tc.post("/v1/budget", json=body, headers=_headers(version=None))
    assert missing_version.status_code == 400

    first = tc.post("/v1/budget", json=body, headers=_headers(idem_key="budget-key-1"))
    assert first.status_code == 200
    second = tc.post("/v1/budget", json=body, headers=_headers(idem_key="budget-key-1"))
    assert second.status_code == 200
    assert second.json() == first.json()


def test_typed_error_envelope_on_auth_and_cap_paths(client):
    tc, api_server_mod, ledger_engine_mod = client

    # 401 — agent already claimed, wrong secret
    ok_body = {"agent_id": "typed-agent-1", "rail": "manual", "amount_cents": 10, "service": "svc"}
    tc.post("/v1/track", json=ok_body, headers=_headers())
    bad_secret = dict(ok_body, agent_secret="wrong")
    r401 = tc.post("/v1/track", json=bad_secret, headers=_headers())
    assert r401.status_code == 401
    assert r401.json()["error"]["code"] == "agent_secret_mismatch"

    # 402 — beta agent cap
    from ledger_engine import BETA_AGENT_CAP
    for i in range(BETA_AGENT_CAP - 1):
        b = {"agent_id": f"typed-cap-{i}", "rail": "manual", "amount_cents": 10, "service": "svc"}
        assert tc.post("/v1/track", json=b, headers=_headers()).status_code == 200
    over = {"agent_id": "typed-cap-over", "rail": "manual", "amount_cents": 10, "service": "svc"}
    r402 = tc.post("/v1/track", json=over, headers=_headers())
    assert r402.status_code == 402
    assert r402.json()["error"]["code"] == "beta_cap_exceeded"

    # 404 — unrelated existing path, still gets the envelope shape
    r404 = tc.get("/server.json")
    if r404.status_code == 404:
        assert "error" in r404.json()
