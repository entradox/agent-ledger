"""The pricing page must never advertise a price nobody can pay.

Regression guard for the defect that kept aiagentscity.com at zero revenue: every
paid CTA resolved to the free door (`/start`) with the label "Start free", because
`pricing.py::_cta()` returned `free_entry` whenever the tier's Stripe env var was
unset. The page read "$19/mo" above a button that gave the product away.

Two further traps this pins, both found while fixing the first:

1. A bare Stripe link carries no `?client_reference_id`. The webhook reads exactly
   that field to decide which workspace to grant (routes_billing.py:120-124), so
   sending a visitor to a bare link charges them and credits nobody. Worse than
   the free door.
2. The Team tier has no link configured. Pointing a $79 button at the Starter link
   would charge $19 while promising $79 — a second lie on the same card.
"""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import pricing  # noqa: E402

PROD = pricing.PRODUCTS["agent-ledger"]
FREE_ENTRY = PROD["free_entry"]


@pytest.fixture(autouse=True)
def _no_stripe_env(monkeypatch):
    """Pin the unconfigured state, which is what production actually runs."""
    for var in ("AL_STRIPE_PAYMENT_LINK", "AL_STRIPE_TEAM_LINK"):
        monkeypatch.delenv(var, raising=False)


def _paid_tiers():
    return [t for t in PROD["tiers"] if t["kind"] == "sub"]


def test_no_paid_tier_sends_a_visitor_to_the_free_door():
    """A tier showing a price must not link to the free entry."""
    for t in _paid_tiers():
        href, label, _ = pricing._cta(t, FREE_ENTRY)
        assert href != FREE_ENTRY, (
            f"{t['name']} advertises ${t['price_mo']}/mo but links to the free door")


def test_no_paid_tier_is_labelled_start_free():
    for t in _paid_tiers():
        _, label, _ = pricing._cta(t, FREE_ENTRY)
        assert label.lower() != "start free", (
            f"{t['name']} shows ${t['price_mo']}/mo under a 'Start free' button")


def test_paid_ctas_route_through_workspace_minting():
    """Paid CTAs must reach a route that attaches client_reference_id."""
    for t in _paid_tiers():
        href, _, _ = pricing._cta(t, FREE_ENTRY)
        if href.startswith("/about"):
            continue  # honest human door, nothing charged
        assert href.startswith(FREE_ENTRY), (
            f"{t['name']} must route through {FREE_ENTRY} so the webhook can credit "
            f"the payer (got {href})")
        assert "plan=" in href, f"{t['name']} CTA lost its ?plan= selector: {href}"


def test_no_paid_cta_is_a_bare_stripe_link():
    """A bare payment link charges the buyer and grants nothing."""
    for t in _paid_tiers():
        href, _, _ = pricing._cta(t, FREE_ENTRY)
        assert not href.startswith("http"), (
            f"{t['name']} links straight at Stripe ({href}); without "
            f"client_reference_id the payer is charged and credited nothing")


def test_team_never_charges_the_starter_amount():
    """With no Team link, a $79 tier must not fall through to the $19 link."""
    team = [t for t in PROD["tiers"] if t["name"] == "Team"][0]
    href, _, configured = pricing._cta(team, FREE_ENTRY)
    assert configured is False, "Team reported configured with no AL_STRIPE_TEAM_LINK"
    assert href == "/about", (
        f"Team ($79) resolved to {href}; with no Team link it must not take $19")


def test_cta_never_returns_empty_href():
    """The original contract, kept: never hand the page an empty href."""
    for t in PROD["tiers"]:
        href, label, _ = pricing._cta(t, FREE_ENTRY)
        assert href, f"{t['name']} produced an empty href"
        assert label, f"{t['name']} produced an empty label"


def test_starter_cta_is_configured_and_charges():
    """Starter is the tier we can actually take money for today."""
    starter = [t for t in PROD["tiers"] if t["name"] == "Starter"][0]
    href, label, configured = pricing._cta(starter, FREE_ENTRY)
    assert configured is True
    assert href == f"{FREE_ENTRY}?plan=starter"
    assert "$" not in label  # the price lives on the card, not inside the button
