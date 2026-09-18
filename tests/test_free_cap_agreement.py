"""The free-tier agent cap is stated in FOUR places. This pins them together.

`BETA_AGENT_CAP` (ledger_engine) and `WORKSPACE_FREE_AGENT_CAP`
(workspace_engine) are two constants that mean one thing: the number of agents
a free workspace may claim before the 402. They drifted once already — the
ledger_engine name used to be the *enforcement* cap site-wide, and after the
2026-09-10 move to per-workspace identity it survived only as a *display*
value while workspace_engine became the real gate.

That split is fine. Silent divergence is not: `/stats` would advertise one cap
while `POST /v1/track` enforced another, and an agent that read the number off
the discovery surface would be wrong about the door in front of it.

Both constants are compared to the cap the ENGINE actually enforces — not just
to each other — so this cannot pass while the product is wrong.
"""

import importlib
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_CLAIM_PROBE_LIMIT = 12  # > any plausible free cap, so the loop terminates


@pytest.fixture
def mod(monkeypatch):
    """Point storage at a throwaway dir so nothing reaches the real store.

    DATA_DIR is resolved at import time, so setting the env var is not enough
    on its own — the modules have to be reloaded for the temp dir to take. The
    claim loop below writes one dir per claimed agent_id; without this fixture
    it writes them into the developer's real ~/.agent-ledger and the test stops
    being re-runnable.
    """
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import ledger_engine
    import workspace_engine

    importlib.reload(workspace_engine)
    importlib.reload(ledger_engine)
    yield ledger_engine, workspace_engine
    shutil.rmtree(tmp, ignore_errors=True)


def _enforced_cap(ledger_engine, workspace_engine, monkeypatch):
    """The cap a FREE workspace really hits, measured by claiming into it.

    Derived from a live claim loop rather than read off a constant: if the
    enforcement path ever stops consulting `effective_agent_cap` and grows its
    own literal, this returns that literal and the assertions below catch it.
    """
    monkeypatch.setattr(workspace_engine, "WORKSPACE_SCARCITY_CAP", 0)
    _, workspace_key = workspace_engine.create_workspace(grant_scarcity=False)

    claimed = 0
    for i in range(_CLAIM_PROBE_LIMIT):
        try:
            ledger_engine.ensure_agent_secret(
                f"cap-agreement-{i}", workspace_key=workspace_key)
        except ledger_engine.BetaCapExceededError:
            break
        claimed += 1
    return claimed


def test_free_cap_constants_agree_with_each_other(mod):
    """Display and enforcement constants must hold the same number."""
    ledger_engine, workspace_engine = mod
    assert ledger_engine.BETA_AGENT_CAP == workspace_engine.WORKSPACE_FREE_AGENT_CAP, (
        "BETA_AGENT_CAP (what /stats and skill.md SHOW) and "
        "WORKSPACE_FREE_AGENT_CAP (what /v1/track ENFORCES) have diverged. "
        "The number an arriving agent is told is no longer the cap it meets."
    )


def test_enforced_cap_equals_the_advertised_cap(mod, monkeypatch):
    """The cap a free workspace actually hits equals the advertised number."""
    ledger_engine, workspace_engine = mod
    enforced = _enforced_cap(ledger_engine, workspace_engine, monkeypatch)

    assert enforced == workspace_engine.WORKSPACE_FREE_AGENT_CAP, (
        f"a free workspace really admitted {enforced} agents while the product "
        f"enforces {workspace_engine.WORKSPACE_FREE_AGENT_CAP}"
    )
    assert enforced == ledger_engine.BETA_AGENT_CAP, (
        f"a free workspace really admitted {enforced} agents while /stats "
        f"reports {ledger_engine.BETA_AGENT_CAP}"
    )


def test_workspace_free_agent_cap_is_the_effective_cap_for_a_free_workspace(mod):
    """`effective_agent_cap` is the single reader — a free record yields the cap."""
    ledger_engine, workspace_engine = mod
    record = {"agent_cap": workspace_engine.WORKSPACE_FREE_AGENT_CAP}
    assert workspace_engine.effective_agent_cap(record) == ledger_engine.BETA_AGENT_CAP
