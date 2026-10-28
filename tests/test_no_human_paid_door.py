# tests/test_no_human_paid_door.py
"""D-1645 — zero Stripe checkout hrefs anywhere on the domain (eval #1).

Blueprint v2: POST /v1/billing/x402 is the ONLY paid path. This crawl asserts
three invariants over every public surface the app serves:

1. No Stripe checkout href or form action exists on any page.
2. No page SELLS the retired $19/$79 human tiers. Two pages are exempted
   from the dollar-amount check because they quote OTHER vendors' prices
   in comparison blocks (/compare, /agent-ledger's "why not a trace viewer")
   — naming a competitor's $79/mo is honest context, not a door.
3. The retired endpoint answers 410 endpoint_retired and steers to x402.
"""
import importlib
import os
import re
import sys

import pytest
from fastapi.testclient import TestClient

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

PAGES = [
    "/", "/products", "/pricing", "/upgrade", "/agent-ledger",
    "/workbench", "/agent-watch", "/trust-scan", "/perimeter-watch",
    "/cited", "/start", "/demo", "/dashboard", "/status", "/terms",
    "/privacy", "/quickstart", "/security", "/about", "/developers",
    "/compare", "/llms.txt", "/pricing.md",
    "/okf/index.md", "/skill.md", "/auth.md", "/.well-known/x402.json",
    "/v1/products", "/.well-known/agent.json",
]

# Pages allowed to carry dollar amounts in comparison context only.
COMPETITOR_PRICE_PAGES = {"/compare", "/agent-ledger"}

# A DOOR is a link/form field a caller could actually use to pay a human —
# a plain-text mention of the retired endpoint (e.g. the changelog's "now
# returns 410") is honest history, not a door, and does not match.
STRIPE_DOOR = re.compile(
    r"buy\.stripe\.com|checkout\.stripe\.com|stripe\.com/checkout"
    r"|href=[\"'][^\"']*checkout|action=[\"'][^\"']*checkout"
    r"|plan=(starter|team|pro)\b|checkout_url",
    re.IGNORECASE,
)
TIER_PRICE = re.compile(r"\$\s*(19|79)\b")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LEDGER_DATA", str(tmp_path))
    monkeypatch.setenv("AL_ADMIN_SECRET", "test-admin")
    import ledger_engine
    import workspace_engine
    import api_server
    for m in (ledger_engine, workspace_engine, api_server):
        importlib.reload(m)
    return TestClient(api_server.app)


@pytest.mark.parametrize("path", PAGES)
def test_no_stripe_checkout_href_exists(client, path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    hit = STRIPE_DOOR.search(r.text)
    assert not hit, f"{path} still carries a Stripe/checkout door: {hit.group(0)!r}"


@pytest.mark.parametrize("path", PAGES)
def test_no_page_sells_the_retired_human_tiers(client, path):
    if path in COMPETITOR_PRICE_PAGES:
        return
    body = client.get(path).text
    hit = TIER_PRICE.search(body)
    assert not hit, f"{path} still quotes the retired tier price: {hit.group(0)!r}"


def test_the_retired_checkout_endpoint_steers_to_x402(client):
    r = client.post("/v1/billing/checkout", json={})
    assert r.status_code == 410
    err = r.json()["error"]
    assert err["code"] == "endpoint_retired"
    assert "/v1/billing/x402" in err["message"]


def test_x402_paid_path_is_reachable_not_shadowed(client):
    # Unpaid request must NOT 404 or silently succeed — it answers with the
    # payment challenge (402) or, on an unconfigured host, an explicit
    # x402_unavailable. Anything else means the door drifted.
    r = client.post("/v1/billing/x402")
    assert r.status_code in (402, 503), f"x402 door returned {r.status_code}"
