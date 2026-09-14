#!/usr/bin/env python3
"""Guards the upgrade path the product advertises to a user who hits the free-tier cap.

Why this file exists: the `BetaCapExceededError` message shipped the BARE Stripe link.
`routes_billing.py` only calls `mark_pro()` when the Stripe session carries
`client_reference_id`, and a bare link carries none — so a user who followed the
product's own instructions paid $19 and stayed capped at 3 agents. No test covered
this message, so the suite never caught it (Morgan found it during an article review).

These tests drive the real HTTP surface a customer hits (`POST /v1/track`) and pin the
402 body to a link that CAN upgrade: it must carry the workspace's own
`client_reference_id`.

Run with: /opt/miniconda3/bin/python3 -m pytest tests/test_cap_upgrade_message.py -q
"""
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

AL_VERSION = "2026-09-01"


@pytest.fixture()
def client(monkeypatch):
    tmp_dir = tempfile.mkdtemp(prefix="agent-ledger-capmsg-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp_dir)
    monkeypatch.setenv("AL_ADMIN_SECRET", "test-admin-secret")
    monkeypatch.delenv("AL_PRO_ACTIVE", raising=False)
    # pin the payment link so the assertion does not depend on a real Stripe URL
    monkeypatch.setenv("AL_STRIPE_PAYMENT_LINK", "https://buy.stripe.com/test_link")

    for mod in ("api_server", "ledger_engine", "metrics", "al_mcp_http", "routes_agents"):
        sys.modules.pop(mod, None)

    import importlib

    import ledger_engine
    import workspace_engine

    importlib.reload(workspace_engine)
    import api_server as api_server_mod

    workspace_id, ws_key = workspace_engine.create_workspace(
        owner_email="capmsg-fixture@example.com", grant_scarcity=False
    )
    from fastapi.testclient import TestClient

    tc = TestClient(api_server_mod.app)
    yield tc, workspace_id, ws_key, workspace_engine

    shutil.rmtree(tmp_dir, ignore_errors=True)


def _headers():
    return {"AL-API-Version": AL_VERSION, "Content-Type": "application/json"}


def _fill_to_cap(tc, ws_key, workspace_engine):
    """Claim exactly the free-tier number of agents."""
    for i in range(workspace_engine.WORKSPACE_FREE_AGENT_CAP):
        body = {"agent_id": f"capmsg-{i}", "rail": "manual", "amount_cents": 10,
                "service": "svc", "workspace_key": ws_key}
        r = tc.post("/v1/track", json=body, headers=_headers())
        assert r.status_code == 200, r.text


def _overflow_message(tc, ws_key):
    body = {"agent_id": "capmsg-overflow", "rail": "manual", "amount_cents": 10,
            "service": "svc", "workspace_key": ws_key}
    r = tc.post("/v1/track", json=body, headers=_headers())
    assert r.status_code == 402, f"expected the free-tier cap to fire, got {r.status_code}"
    return r.json()


def test_cap_402_advertises_a_workspace_bound_link(client):
    """The upgrade link in the 402 body must carry THIS workspace's reference."""
    tc, workspace_id, ws_key, workspace_engine = client
    _fill_to_cap(tc, ws_key, workspace_engine)
    payload = _overflow_message(tc, ws_key)

    message = str(payload)
    assert "client_reference_id=" in message, (
        "the cap response must give a workspace-bound checkout link; without "
        "client_reference_id the webhook skips mark_pro() and the customer pays "
        "without being upgraded"
    )
    assert workspace_id in message, (
        "the link must reference THIS workspace, not an empty or placeholder id"
    )


def test_cap_402_never_ships_a_bare_payment_link(client):
    """Any buy.stripe.com URL in the cap response must be bound to the workspace."""
    tc, _, ws_key, workspace_engine = client
    _fill_to_cap(tc, ws_key, workspace_engine)
    message = str(_overflow_message(tc, ws_key))

    urls = re.findall(r"https://buy\.stripe\.com/\S*", message)
    assert urls, "expected the cap response to point at an upgrade path"
    for url in urls:
        assert "client_reference_id=" in url, (
            f"bare (non-binding) payment link shipped in the cap response: {url}"
        )
