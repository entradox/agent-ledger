"""Every customer-facing surface must name the paid ladder honestly.

Blueprint v2 (2026-10-11) removed the human paid tiers ($19 Starter / $79 Team)
entirely: the only paid path is the agent's own x402 Pro pass. The label war this
file was written for — "$19 Pro" promising unlimited agents — can no longer exist
because the tier cannot; these tests now pin that no surface resurrects it, and
that the surfaces which used to name the $19 tier name the rail instead.

They deliberately do NOT forbid the word "Pro" — it is correct for the x402 pass
and for the retired scarcity grant. They forbid it only where it labels a human
monthly tier, and they forbid the human tiers themselves on buy surfaces.
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


def test_the_dashboard_names_the_rail_not_a_human_tier():
    """The upgrade control on the dashboard must point at the only paid path."""
    src = _read("routes_workspace.py")
    assert "Upgrade to Starter" not in src, "the $19 human tier is back on the dashboard"
    assert "$19" not in src, "a human monthly price is on the dashboard"
    assert "x402" in src, "the agent rail disappeared from the dashboard pay door"


def test_llms_upgrade_copy_names_the_agent_rail():
    """An agent reading llms.txt must find the rail it can actually pay."""
    src = _read("api_server.py")
    assert "$19/mo Starter" not in src, (
        "the llms.txt upgrade path advertises a monthly plan that cannot be bought")
    assert "/v1/billing/x402" in src


def test_only_the_x402_pass_is_described_as_unlimited():
    """'unlimited' attaches to the pass — and to nothing sold to a human.

    The allowance is DERIVED now (x402_pass_agent_limit — a starter-configured
    deployment must not advertise unlimited), so check the rendered llms.txt,
    not a hardcoded literal."""
    import sys
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    import api_server
    assert "unlimited agents on the workspace" in api_server.LLMS_TXT, (
        "the x402 pass lost its agent-limit description")
    # No human monthly tier may claim anything on this domain.
    for zombie in ("$19/mo Starter", "$79/mo Team", "billed through Stripe"):
        assert zombie not in api_server.LLMS_TXT, (
            f"the retired human tier {zombie!r} is back")
