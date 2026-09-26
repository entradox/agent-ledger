"""Every customer-facing surface must name the $19 tier "Starter", not "Pro".

The paid ladder is: free (3 agents) -> $19 "Starter" (10) -> $79 "Team" (50). The x402
pass is the only thing that grants unbounded agents, and it is a 24h pass, not a plan.

The $19 tier was repeatedly mislabelled "Pro" on customer-facing surfaces, and "Pro" in
this product genuinely means the unbounded x402 pass — so the wrong label promised
unlimited agents for a $19 subscription that grants 10. Three separate surfaces carried
it: the dashboard upgrade button, the llms.txt upgrade line, and the pricing explainer.

These tests read the source of truth rather than a rendered page so they run without a
server. They deliberately do NOT forbid the word "Pro" — it is correct for the x402 pass
and for the retired scarcity grant. They forbid it only where it labels the $19 tier.
"""

import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    return open(os.path.join(REPO, name)).read()


# (file, pattern that must NOT appear) — each was a real live defect.
# NOTE: narrow patterns on purpose. `$19/mo, unlimited agents` also appears inside a
# docstring in api_server.py that DOCUMENTS this very bug ("...the way the hardcoded
# '$19/mo, unlimited agents' line did before..."), so a loose pattern flags the
# explanation of the fix as if it were the defect. Match the customer-facing forms.
#
# EXTENDED 2026-09-26: the first version of this list had three entries and missed the
# two surfaces with the HIGHEST reliance weight — the onboarding email sent to a paying
# customer, and the README purchase CTA. Both still said "$19 = Pro = unlimited" while
# routes_billing grants `starter` (10 agents) for a 1900-cent settlement. The email was
# the worse of the two: the subject line interpolates `plan` and read "starter", so the
# email contradicted its own body. A guard that covers the surfaces nobody reads is
# worth less than one that covers the surface that arrives in the customer's inbox.
FORBIDDEN_PRICE_LABELS = [
    ("routes_workspace.py", r"Upgrade to Pro — \$19"),
    ("api_server.py", r"\$19/mo, unlimited, no expiry\)"),
    ("site_pages.py", r"flat \$19/workspace, not per-seat"),
    # The promise emailed to a $19 payer. `plan` is interpolated now, so a hardcoded
    # "Pro" or an unbounded "as many as you want" can only come back as a regression.
    ("send_onboarding_email.py", r"You're on AgentLedger Pro"),
    ("send_onboarding_email.py", r"as many agents as you want"),
    ("send_onboarding_email.py", r'plan: str = "pro"'),
    # The purchase path a developer reads before paying.
    ("README.md", r"\*\*Pro \$19/mo\*\*"),
    ("npm/README.md", r"\*\*Pro \$19/mo\*\*"),
    ("README.md", r"Pro \$19/mo ⇒ unlimited"),
    ("npm/README.md", r"Pro \$19/mo ⇒ unlimited"),
]


def test_the_onboarding_email_never_promises_unlimited():
    """The highest-reliance surface: it lands in a paying customer's inbox.

    Asserts the actual rendered body for a `starter` payer, not just the absence of a
    bad string — so a future edit that re-introduces "unlimited" by another wording
    still fails here.
    """
    import importlib
    import sys
    sys.path.insert(0, REPO)
    mod = importlib.import_module("send_onboarding_email")
    importlib.reload(mod)
    rendered = mod.BODY.format(base=mod.BASE, plan="Starter", agents="10")
    assert "unlimited" not in rendered.lower(), (
        "the $19 payer's onboarding email claims unlimited agents; $19 buys 10")
    assert "10 agents" in rendered, "the email must state the real agent cap"



def test_no_surface_labels_the_19_tier_as_pro():
    problems = []
    for fname, pattern in FORBIDDEN_PRICE_LABELS:
        src = _read(fname)
        if re.search(pattern, src):
            problems.append(f"{fname}: still matches /{pattern}/")
    assert not problems, (
        "the $19 tier is being labelled 'Pro' (which means unlimited) on: "
        + "; ".join(problems))


def test_19_tier_is_called_starter_where_it_is_named():
    """The dashboard button must name the tier the customer is buying."""
    src = _read("routes_workspace.py")
    assert "Upgrade to Starter — $19/mo" in src


def test_llms_upgrade_line_states_the_agent_cap():
    """An agent reading llms.txt must not be told $19 buys unlimited agents."""
    src = _read("api_server.py")
    assert "$19/mo Starter, up to 10 agents" in src, (
        "the llms.txt upgrade path must state the real agent cap")


def test_only_the_x402_pass_is_described_as_unlimited():
    """'unlimited' must never attach to a monthly plan."""
    src = _read("api_server.py")
    # The known-good description of the ladder, which gets this right.
    assert "Only the x402 Pro pass is unlimited" in src
