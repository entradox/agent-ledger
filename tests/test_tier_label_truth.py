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
FORBIDDEN_PRICE_LABELS = [
    ("routes_workspace.py", r"Upgrade to Pro — \$19"),
    ("api_server.py", r"\$19/mo, unlimited, no expiry\)"),
    ("site_pages.py", r"flat \$19/workspace, not per-seat"),
]


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
