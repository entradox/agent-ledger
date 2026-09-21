"""A demo run must not be counted as demand.

`agent-ledger demo` exercises REAL enforcement — it mints a real workspace, claims a
real agent, sets a real cap and gets a real 402. That is the point. The risk is that
every demonstration run then lands in the onboarding funnel and inflates the launch
numbers we make go/no-go decisions from.

These tests pin both halves:
  - a workspace minted with ?demo=1 is tagged and EXCLUDED from onboarding, and
  - a normal workspace is NOT excluded (the guard cannot silently hide real demand).

Regression note: the first live run of this demo against production moved
workspace_minted 68 -> 70, because the exclusion ships with the server code and the
run happened before deploy. The mechanism below is what stops that repeating.
"""

import json
import os
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


@pytest.fixture()
def isolated_data(tmp_path, monkeypatch):
    """Point both the workspace store and the metrics store at a temp dir."""
    monkeypatch.setenv("AGENT_LEDGER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("AGENT_LEDGER_METRICS", str(tmp_path / "metrics.jsonl"))
    for mod in ("workspace_engine", "metrics", "ledger_engine"):
        sys.modules.pop(mod, None)
    import workspace_engine
    import metrics
    return workspace_engine, metrics


def test_demo_workspace_is_tagged(isolated_data):
    we, _ = isolated_data
    ws_id, _key = we.create_workspace(grant_scarcity=False, is_demo=True)
    rec = we.get_workspace(ws_id)
    assert rec is not None
    assert rec.get("demo") is True, "a demo workspace must carry the demo tag"


def test_normal_workspace_is_not_tagged(isolated_data):
    we, _ = isolated_data
    ws_id, _key = we.create_workspace(grant_scarcity=False)
    rec = we.get_workspace(ws_id)
    assert rec is not None
    assert not rec.get("demo"), "a real workspace must never be marked as a demo"


def test_demo_workspace_is_excluded_from_onboarding(isolated_data):
    we, metrics = isolated_data
    ws_id, _key = we.create_workspace(grant_scarcity=False, is_demo=True)
    metrics.record_onboarding("workspace_minted", ws_id, plan="free")
    metrics.record_onboarding("key_revealed", ws_id)
    funnel = metrics.onboarding_funnel()
    steps = {s["step"]: s for s in funnel.get("steps", [])}
    assert steps.get("workspace_minted", {}).get("workspaces", 0) == 0, (
        "a demo run must not add to workspace_minted")
    assert steps.get("key_revealed", {}).get("workspaces", 0) == 0, (
        "a demo run must not add to key_revealed")


def test_real_workspace_still_counts(isolated_data):
    """The exclusion must not swallow real demand."""
    we, metrics = isolated_data
    ws_id, _key = we.create_workspace(grant_scarcity=False)
    metrics.record_onboarding("workspace_minted", ws_id, plan="free")
    funnel = metrics.onboarding_funnel()
    steps = {s["step"]: s for s in funnel.get("steps", [])}
    assert steps.get("workspace_minted", {}).get("workspaces", 0) == 1


def test_demo_run_moves_no_funnel_count(isolated_data):
    """The whole guarantee, in one test: a full demo lifecycle changes nothing."""
    we, metrics = isolated_data
    before = metrics.onboarding_funnel()
    before_steps = {s["step"]: s.get("workspaces", 0) for s in before.get("steps", [])}

    ws_id, _key = we.create_workspace(grant_scarcity=False, is_demo=True)
    for step in ("workspace_minted", "key_revealed", "agent_claimed", "track_written"):
        metrics.record_onboarding(step, ws_id)

    after = metrics.onboarding_funnel()
    after_steps = {s["step"]: s.get("workspaces", 0) for s in after.get("steps", [])}
    assert after_steps == before_steps, "a demo run must leave the funnel unchanged"


def test_unreadable_workspace_is_not_treated_as_demo(isolated_data):
    """A transient read failure must not hide a real workspace's events."""
    _we, metrics = isolated_data
    assert metrics.is_demo_workspace("") is False
    assert metrics.is_demo_workspace("ws_does_not_exist") is False


def test_demo_subcommand_exists():
    """The published command must actually exist in the CLI."""
    import cli
    import argparse
    src = open(os.path.join(REPO, "cli.py")).read()
    assert "cmd_demo" in src
    assert 'sub.add_parser("demo"' in src
