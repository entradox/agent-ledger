#!/usr/bin/env python3
"""Guards the upgrade path the product advertises to a user who hits the free-tier cap.

Why this file exists: the `BetaCapExceededError` message once shipped a Stripe link
that could charge a card and upgrade nothing — a user who followed the product's own
instructions paid $19 and stayed capped at 3 agents. Blueprint v2 (2026-10-11) then
removed the human checkout entirely, so the contract this file pins is now:

  * the 402 names the only paid path that exists — POST /v1/billing/x402 —
    and the honest caveat that the pass binds to the wallet's workspace
  * no buy.stripe.com link can appear: the human door is retired, so any
    Stripe URL here is a charge-nobody-can-collect lie

These tests drive the real HTTP surface a customer hits (`POST /v1/track`).

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


def test_cap_402_names_the_agent_rail_and_the_wallet_caveat(client):
    """The 402 must hand the agent a machine-payable path (D-1270) — and be
    honest that the pass binds to the paying wallet's workspace, which is a
    DIFFERENT workspace from a free one minted via /start."""
    tc, _, ws_key, workspace_engine = client
    _fill_to_cap(tc, ws_key, workspace_engine)
    message = str(_overflow_message(tc, ws_key))

    assert "/v1/billing/x402" in message, (
        "the cap response must hand the agent the only paid path that exists")
    assert "wallet" in message.lower(), (
        "the caveat that the pass binds to the wallet's workspace — possibly a "
        "different one — must be stated, not implied")
    import x402_verify
    assert x402_verify.X402_MINT_PRICE in message


def test_cap_402_never_ships_a_stripe_link(client):
    """The human door is retired: any buy.stripe.com URL here is a link that
    charges a card and upgrades nothing."""
    tc, _, ws_key, workspace_engine = client
    _fill_to_cap(tc, ws_key, workspace_engine)
    message = str(_overflow_message(tc, ws_key))

    urls = re.findall(r"https://buy\.stripe\.com/\S*", message)
    assert not urls, (
        f"Stripe payment link(s) shipped in the cap response on a domain with "
        f"no human checkout: {urls}")
