#!/usr/bin/env python3
"""The human buy path (D-1162).

Before this, a human's only route to a workspace was a Google sign-in the
deployment could not serve (`/login` → 503 login_not_configured), so the
product had a live $19 payment link and no way for anyone to reach it. POST
/start is that route now, and these tests pin both halves of it:

  - GET /start renders the page and does NOT mint — a crawler, a link-preview
    bot or an accidental reload must not burn one of the 50 launch-window
    workspaces and orphan a key nobody ever saw.
  - POST /start mints a workspace and its payment link carries that
    workspace's id as client_reference_id, which is the field the Stripe
    webhook reads to mark it Pro. Without it a real card payment charges the
    card and upgrades nothing.
"""
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="agent-ledger-start-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, workspace_engine
    importlib.reload(ledger_engine)
    importlib.reload(workspace_engine)
    import api_server
    importlib.reload(api_server)
    from fastapi.testclient import TestClient
    return TestClient(api_server.app)


def test_front_door_is_the_status_page_not_a_404(client):
    """The front door 404'd until D-1162: `api_server.py` never had a root
    route, so the only human page was /status."""
    r = client.get("/")
    assert r.status_code == 200
    assert 'href="/start"' in r.text
    assert client.get("/status").text == r.text


def test_get_start_does_not_mint_a_workspace(client):
    import workspace_engine
    before = workspace_engine.workspace_count()
    for _ in range(3):
        assert client.get("/start").status_code == 200
    assert workspace_engine.workspace_count() == before, (
        "GET /start minted a workspace — a crawler would eat the launch window")


def test_post_start_mints_and_shows_the_key_once(client):
    import workspace_engine
    r = client.post("/start")
    assert r.status_code == 200
    assert "wk_live_" in r.text
    assert "shown once" in r.text
    assert workspace_engine.workspace_count() == 1
    # A second POST is a second visitor, not a re-reveal of the first key.
    r2 = client.post("/start")
    assert r2.status_code == 200
    assert workspace_engine.workspace_count() == 2
    assert re.search(r"wk_live_[A-Za-z0-9_\-]+", r2.text).group(0) != \
        re.search(r"wk_live_[A-Za-z0-9_\-]+", r.text).group(0)


def test_post_start_payment_link_targets_the_workspace_it_just_minted(client, monkeypatch):
    monkeypatch.setenv("AL_STRIPE_PAYMENT_LINK", "https://buy.stripe.com/test_link")
    import importlib
    import api_server
    importlib.reload(api_server)
    from fastapi.testclient import TestClient
    c = TestClient(api_server.app)
    r = c.post("/start")
    ws_id = re.search(r"workspace_id: (ws_[A-Za-z0-9_\-]+)", r.text).group(1)
    assert f"https://buy.stripe.com/test_link?client_reference_id={ws_id}" in r.text


@pytest.mark.parametrize("path", ("/login", "/logout", "/dashboard",
                                  "/auth/google/callback"))
def test_google_auth_routes_are_gone_not_just_broken(client, path):
    """Vanished, not 503: an agent-first product has no human login, and a
    typed 503 on a route that still exists implies the feature is merely
    unconfigured. Google OAuth returns when a real reason for it exists."""
    assert client.get(path).status_code == 404


def test_post_start_creates_the_free_tier_not_the_paid_one(client):
    """Morgan's finding, verified before it was fixed: the launch-window grant
    hands over the ENTIRE paid tier — agent_cap None, one year, free — so
    minting every anonymous visitor into it made the $19 upgrade button
    decorative and put the whole promotion one loop away from anyone who
    wanted it. /start now creates exactly the free tier the page advertises."""
    import workspace_engine
    r = client.post("/start")
    ws_id = re.search(r"workspace_id: (ws_[A-Za-z0-9_\-]+)", r.text).group(1)
    ws = workspace_engine.get_workspace(ws_id)
    assert ws["plan"] == "free"
    assert ws["pro_scarcity"] is False
    assert ws["pro_until"] is None
    assert workspace_engine.effective_agent_cap(ws) == 3
    assert "$19" in r.text, "the upgrade offer must still be on the page"


def test_the_launch_grant_is_still_reachable_by_the_agent_path(client):
    """grant_scarcity=False is scoped to the human self-serve route. A real
    wallet payment is a commitment signal, not an anonymous HTTP request, and
    still earns the grant."""
    import workspace_engine
    ws_id, _ = workspace_engine.create_workspace(wallet_address="0x" + "ab" * 20)
    assert workspace_engine.get_workspace(ws_id)["plan"] == "pro"


def test_start_mint_is_rate_limited_per_ip(client):
    """POST /start is an unauthenticated write on a public route. Without a
    cap, one loop mints unlimited workspaces."""
    codes = [client.post("/start").status_code for _ in range(4)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429


def test_key_reveal_response_is_never_cached(client):
    """The workspace_key is shown exactly once and cannot be reissued, so a
    cached copy would strand a credential we cannot recover."""
    r = client.post("/start")
    assert r.headers.get("cache-control") == "no-store"
    assert r.headers.get("referrer-policy") == "no-referrer"
