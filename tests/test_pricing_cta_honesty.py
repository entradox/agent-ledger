"""The pricing page must never advertise a price nobody can pay.

Regression guard for the defect class that kept aiagentscity.com at zero
revenue: a card showing a price whose CTA could not transact. Blueprint v2
(2026-10-11) removed the human paid tiers entirely — the catalog carries free
tiers plus, where a real agent-paid rail exists, the rail derived from the
live x402 config. The honesty contract is now:

  * no tier ever advertises a human price or links to a checkout that does
    not exist (there is no human checkout)
  * every CTA href is non-empty and points at a real surface
  * the rail card renders only where a rail actually exists (agent-ledger),
    and its price/duration come from x402_verify, never a literal
  * a stale Stripe env var can never leak a buy.stripe.com link onto the page
"""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import pricing  # noqa: E402


@pytest.fixture(autouse=True)
def _no_stripe_env(monkeypatch):
    """Stale link env vars must not resurrect a paid door anywhere."""
    for var in ("AL_STRIPE_PAYMENT_LINK", "AL_STRIPE_TEAM_LINK",
                "AW_STRIPE_WATCH_LINK", "PW_STRIPE_LITE_LINK",
                "TS_STRIPE_TEAM_LINK", "CT_STRIPE_REPORT_LINK"):
        monkeypatch.delenv(var, raising=False)


def test_no_product_advertises_a_paid_tier():
    """There is no human checkout, so no card may show a monthly/annual price."""
    for pid, prod in pricing.PRODUCTS.items():
        for t in prod["tiers"]:
            assert t["kind"] == "free", (
                f"{pid}/{t['name']} is a {t['kind']} tier — there is no human "
                "checkout it could be paid through")
            for banned in ("price_mo", "price_yr", "stripe_env"):
                assert banned not in t, (
                    f"{pid}/{t['name']} still carries {banned}")


def test_every_cta_href_is_nonempty_and_points_at_a_real_surface():
    """The original contract, kept: never a dead button."""
    for pid, prod in pricing.PRODUCTS.items():
        for t in prod["tiers"]:
            href = t.get("href") or prod["free_entry"]
            assert href, f"{pid}/{t['name']} produced an empty href"
            assert t["cta"], f"{pid}/{t['name']} produced an empty label"


def test_no_stripe_link_can_appear_even_with_stale_env(monkeypatch):
    """The env vars are dead config now — pricing.py must not read them at all."""
    monkeypatch.setenv("AL_STRIPE_PAYMENT_LINK", "https://buy.stripe.com/zombie")
    monkeypatch.setenv("AW_STRIPE_WATCH_LINK", "https://buy.stripe.com/zombie2")
    for pid in pricing.PRODUCTS:
        html = pricing.pricing_section(pid)
        assert "buy.stripe.com" not in html, (
            f"{pid}: a stale Stripe env var leaked a payment link onto the page")
        assert "checkout.stripe" not in html


def test_the_rail_card_only_renders_where_a_rail_exists():
    """Only agent-ledger has a live paid rail; no other product may pretend."""
    for pid in pricing.PRODUCTS:
        html = pricing.pricing_section(pid)
        if pid == "agent-ledger":
            assert "/v1/billing/x402" in html, (
                "the rail card is missing the payable endpoint")
        else:
            assert "/v1/billing/x402" not in html, (
                f"{pid} advertises a paid rail it does not have")


def test_the_rail_price_comes_from_live_config_not_a_literal():
    """The advertised price must be the configured one — drift here means the
    page charges one price and promises another."""
    import x402_verify
    html = pricing.pricing_section("agent-ledger")
    assert x402_verify.X402_MINT_PRICE in html
    hours = round(x402_verify.X402_PRO_PASS_SECONDS / 3600)
    assert f"{hours}h" in html


def test_pricing_md_names_no_human_tier_or_stripe_link():
    body = pricing.pricing_md()
    for banned in ("$19", "$79", "$190", "$790", "buy.stripe", "Starter — $",
                   "Team — $", "per month", "/mo"):
        assert banned not in body, f"/pricing.md still advertises {banned!r}"
    import x402_verify
    assert x402_verify.X402_MINT_PRICE in body
    assert "/v1/billing/x402" in body
