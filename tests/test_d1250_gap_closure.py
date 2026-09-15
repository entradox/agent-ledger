# tests/test_d1250_gap_closure.py
"""D-1250 — closing the gaps against the Muse SWOT + Executor Playbook docs:
the interactive blocked-call simulator, /compare's missing rivals, real
measured proxy latency on /reliability, and the new funnel events."""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest


@pytest.fixture(autouse=True)
def data_dir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="agent-ledger-d1250-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, workspace_engine, identity, api_server, metrics
    for m in (ledger_engine, workspace_engine, identity, api_server, metrics):
        importlib.reload(m)
    yield
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def page():
    from fastapi.testclient import TestClient
    import api_server
    return TestClient(api_server.app)


def _ledger_files(tmp_dir: str) -> set:
    """Everything under the ledger data dir EXCEPT metrics.jsonl — the
    simulator is allowed to log an event, it must never create an agent,
    budget, or spend record."""
    return {p for p in Path(tmp_dir).rglob("*")
            if p.is_file() and p.name != "metrics.jsonl"}


# ── the simulator ────────────────────────────────────────────────────────

def test_simulate_matches_real_block_logic(page):
    r = page.post("/v1/demo/simulate", json={
        "monthly_cap_cents": 1000, "already_spent_cents": 900,
        "estimated_call_cents": 500})
    assert r.status_code == 200
    body = r.json()
    assert body["blocked"] is True
    assert body["status_code"] == 402
    assert body["code"] == "budget_exceeded"
    assert "blocked before the provider" in body["message"]
    assert "Nothing was sent upstream" in body["message"]
    assert "500 cents" in body["message"]
    assert "900 of 1000 cents" in body["message"]


def test_simulate_reports_within_budget(page):
    r = page.post("/v1/demo/simulate", json={
        "monthly_cap_cents": 1000, "already_spent_cents": 100,
        "estimated_call_cents": 50})
    body = r.json()
    assert body["blocked"] is False
    assert body["status_code"] == 200


def test_simulate_writes_no_agent_or_ledger_data(page, monkeypatch):
    import os
    tmp_dir = os.environ["AGENT_LEDGER_DATA"]
    before = _ledger_files(tmp_dir)
    page.post("/v1/demo/simulate", json={
        "monthly_cap_cents": 1000, "already_spent_cents": 900,
        "estimated_call_cents": 500})
    after = _ledger_files(tmp_dir)
    assert before == after, "the simulator created or modified a ledger file"


def test_simulate_requires_no_auth(page):
    """No X-AL-Agent, no X-AL-Secret, no workspace_key anywhere in the request
    — this is the whole point (Muse: 'proof before signup')."""
    r = page.post("/v1/demo/simulate", json={
        "monthly_cap_cents": 500, "already_spent_cents": 0,
        "estimated_call_cents": 10})
    assert r.status_code == 200


# ── /compare — the missing rivals ────────────────────────────────────────

def test_compare_lists_pre_provider_blocking_rivals(page):
    body = page.get("/compare").text
    for name in ("LiteLLM", "Portkey", "LangDB", "Revenium", "LangSmith"):
        assert name in body, f"{name} still missing from /compare"


def test_compare_still_covers_trace_viewers(page):
    """The fix is additive — the original trace-viewer section must survive."""
    body = page.get("/compare").text
    for name in ("LangSmith", "Helicone", "Langfuse"):
        assert name in body


# ── /reliability — real measured latency, never invented ────────────────

def test_reliability_reports_zero_honestly_on_fresh_instance(page):
    body = page.get("/reliability").text
    assert page.get("/reliability").status_code == 200
    assert "No proxied calls have been measured" in body


def test_reliability_reports_real_measured_latency(page):
    import metrics
    for ms in (12.0, 34.0, 56.0, 78.0, 90.0):
        metrics.record_latency("pre_call_check", ms)
    body = page.get("/reliability").text
    assert "No proxied calls have been measured" not in body
    assert "5 measured calls" in body


def test_latency_percentiles_move_with_new_samples(page):
    import metrics
    first = metrics.latency_percentiles("pre_call_check")
    assert first["sample_count"] == 0
    metrics.record_latency("pre_call_check", 100.0)
    metrics.record_latency("pre_call_check", 200.0)
    second = metrics.latency_percentiles("pre_call_check")
    assert second["sample_count"] == 2
    assert second["p50_ms"] in (100.0, 150.0, 200.0)  # exact index depends on method; just non-null
    assert second["p50_ms"] is not None


# ── funnel events (D-1250's answer to Muse's "no analytics" gap) ────────

def test_demo_view_is_tracked(page):
    import metrics
    page.get("/demo")
    assert metrics.snapshot()["totals"].get("demo_view", 0) >= 1


def test_simulator_events_are_tracked(page):
    import metrics
    page.post("/v1/demo/simulate", json={
        "monthly_cap_cents": 100, "already_spent_cents": 90,
        "estimated_call_cents": 50})
    totals = metrics.snapshot()["totals"]
    assert totals.get("simulator_run", 0) >= 1
    assert totals.get("simulator_blocked", 0) >= 1


def test_quickstart_copy_beacon_is_tracked(page):
    import metrics
    r = page.post("/v1/demo/quickstart-copy")
    assert r.status_code == 200
    assert metrics.snapshot()["totals"].get("quickstart_copy", 0) >= 1


# ── the stale PyPI/npm docstring claim ───────────────────────────────────

def test_no_page_claims_pypi_release_is_missing(page):
    """The install line has been live on PyPI/npm since 2026-09-14 — no page
    may claim otherwise (verified live: pypi.org/pypi/aiagentscity-ledger and
    registry.npmjs.org both report v0.4.1)."""
    for route in ("/agent-ledger", "/quickstart"):
        body = page.get(route).text
        assert "no PyPI or npm" not in body.lower()
        assert "nothing is published on pypi" not in body.lower()
