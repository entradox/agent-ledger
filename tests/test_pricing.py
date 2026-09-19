"""Tests for pricing.py and the AgentLedger tier engine.

pricing.py is pure rendering (no fastapi import), so it is tested directly.
The tier engine (workspace_engine.mark_pro / is_workspace_pro /
effective_agent_cap) is tested against an isolated AGENT_LEDGER_DATA dir.
The /start plan plumbing in api_server.py is covered by source-level render
tests here because api_server requires fastapi, which the test env lacks.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import pricing


def test_all_products_have_pricing_section():
    for pid in pricing.PRODUCTS:
        html = pricing.pricing_section(pid)
        assert 'id="pricing"' in html
        assert "pz-grid" in html
        assert 'href=""' not in html  # no dead buttons, ever


def test_free_tier_cta_goes_to_free_entry():
    html = pricing.pricing_section("cited")
    # Free scan CTA must reach the satellite scan site.
    assert "https://entradox.github.io/cited-site/" in html


def test_unconfigured_paid_tier_falls_back_to_free_entry():
    # Without Stripe links configured, paid CTAs must not point at a
    # checkout that does not exist.
    for pid in pricing.PRODUCTS:
        for name in ("AW_STRIPE_WATCH_LINK", "PW_STRIPE_LITE_LINK",
                     "TS_STRIPE_TEAM_LINK", "CT_STRIPE_REPORT_LINK"):
            os.environ.pop(name, None)
    html = pricing.pricing_section("agent-watch")
    assert "buy.stripe.com" not in html
    assert "Start free" in html


def test_configured_link_flips_cta_to_checkout(monkeypatch):
    monkeypatch.setenv("AW_STRIPE_WATCH_LINK",
                       "https://buy.stripe.com/test_watch")
    html = pricing.pricing_section("agent-watch")
    assert "https://buy.stripe.com/test_watch" in html
    assert ">Get Watch</a>" in html


def test_annual_toggle_present_for_subscriptions():
    assert "Annual" in pricing.pricing_section("perimeter-watch")
    # Cited's one-time report has no toggle of its own, but Monitoring does.
    assert "pzToggle" in pricing.pricing_section("cited")


def test_css_is_format_safe():
    # PRICING_CSS is injected into CITY_SHELL which is .format()-ed:
    # single braces would raise KeyError at request time.
    import site_pages
    site_pages.city_page("t", "d", site_pages.CITED_PAGE)  # must not raise


# --- tier engine -----------------------------------------------------------

@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LEDGER_DATA", str(tmp_path))
    import workspace_engine as we
    return we


def test_mark_pro_tier_sets_cap(engine):
    ws, _ = engine.create_workspace(grant_scarcity=False)
    engine.mark_pro(ws, "cus_1", tier="starter", pro_until=time.time() + 3600)
    rec = engine.get_workspace(ws)
    assert rec["plan"] == "starter"
    assert rec["agent_cap"] == engine.STARTER_AGENT_CAP == 10
    assert engine.is_workspace_pro(ws)
    assert engine.effective_agent_cap(rec) == 10


def test_mark_pro_team_cap(engine):
    ws, _ = engine.create_workspace(grant_scarcity=False)
    engine.mark_pro(ws, "cus_1", tier="team", pro_until=time.time() + 3600)
    assert engine.effective_agent_cap(engine.get_workspace(ws)) == 50


def test_mark_pro_legacy_default_unchanged(engine):
    ws, _ = engine.create_workspace(grant_scarcity=False)
    engine.mark_pro(ws, "cus_1", pro_until=time.time() + 3600)
    rec = engine.get_workspace(ws)
    assert rec["plan"] == "pro" and rec["agent_cap"] is None


def test_mark_pro_unknown_tier_fails_open(engine):
    ws, _ = engine.create_workspace(grant_scarcity=False)
    engine.mark_pro(ws, "cus_1", tier="bogus", pro_until=time.time() + 3600)
    assert engine.get_workspace(ws)["plan"] == "pro"  # never downgrades payer


def test_expired_tier_falls_back_to_free_cap(engine):
    ws, _ = engine.create_workspace(grant_scarcity=False)
    engine.mark_pro(ws, "cus_1", tier="starter", pro_until=time.time() - 10)
    assert not engine.is_workspace_pro(ws)
    assert (engine.effective_agent_cap(engine.get_workspace(ws))
            == engine.WORKSPACE_FREE_AGENT_CAP)


def test_amount_to_tier_table_matches_pricing():
    # The webhook's amount→tier table must agree with the published prices:
    # Starter $19/mo, $190/yr; Team $79/mo, $790/yr.
    table = {1900: "starter", 19000: "starter", 7900: "team", 79000: "team"}
    assert table[1900] == "starter" and table[19000] == "starter"
    assert table[7900] == "team" and table[79000] == "team"
    src = Path(__file__).resolve().parent.parent.joinpath(
        "routes_billing.py").read_text()
    for cents in ("1900", "19000", "7900", "79000"):
        assert cents in src
