# tests/test_workspace_dashboard.py
"""GAP-1/2 (2026-09-13) — the workspace dashboard surface.

Until this phase the workspace owner's only views were per-agent reports and
an owner-only /v1/dashboard. These tests pin the new workspace-scoped surface:

- /v1/workspace/summary: one workspace, every agent, totals + daily series
- /v1/workspace/export.csv: every entry as CSV
- /v1/report/{agent_id}/csv: one agent's entries as CSV
- /dashboard: the self-contained HTML page

Security invariants pinned here:
- 401 before any data: no key -> 401, wrong key -> 401, and no response body
  ever contains another workspace's agent_id
- scoping: workspace A's key can never enumerate workspace B's agents
- the dashboard page references no external origin (a third-party script
  would see the workspace key the browser holds)
"""
import csv as _csv
import io
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

AL_VERSION = "2026-09-01"
AGENT_A = "alpha-agent"
AGENT_B = "beta-agent"
FOREIGN = "foreign-agent"


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, workspace_engine, identity, share_tokens, \
        routes_agents, routes_workspace, api_server
    for m in (ledger_engine, workspace_engine, identity, share_tokens,
              routes_agents, routes_workspace, api_server):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    tc = TestClient(api_server.app)

    _, key_a = workspace_engine.create_workspace(owner_email="a@example.com")
    _, key_b = workspace_engine.create_workspace(owner_email="b@example.com")

    def track(agent, key, **body):
        r = tc.post("/v1/track", headers={"AL-API-Version": AL_VERSION},
                    json={"agent_id": agent, "workspace_key": key, **body})
        assert r.status_code == 200, r.text
        return r

    r = track(AGENT_A, key_a, rail="api_key", amount_cents=250,
              service="openai", tokens_in=100, tokens_out=50, model="gpt-4o-mini")
    secret_a = r.json().get("agent_secret")
    track(AGENT_B, key_a, rail="manual", amount_cents=750, service="anthropic")
    track(FOREIGN, key_b, rail="api_key", amount_cents=9999, service="openai")

    yield tc, key_a, key_b, secret_a
    shutil.rmtree(tmp, ignore_errors=True)


def _backdate(agent_id, days):
    """Append a backdated entry directly to the ledger so the daily series has
    a second non-zero bucket — the API always stamps 'now', so multi-day data
    can only exist via a file write (which is exactly how real old data looks)."""
    import ledger_engine
    p = ledger_engine._ledger_path(agent_id)
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    entry = {"agent_id": agent_id, "rail": "api_key", "amount_cents": 400,
             "service": "openai", "timestamp": old, "tokens_in": 0, "tokens_out": 0}
    with open(p, "a") as f:
        f.write(json.dumps(entry) + "\n")


# --- summary ---

def test_summary_requires_a_key(env):
    tc, _, _, _ = env
    r = tc.get("/v1/workspace/summary")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "workspace_key_required"


def test_summary_rejects_a_foreign_key(env):
    tc, _, _, _ = env
    r = tc.get("/v1/workspace/summary", headers={"X-Workspace-Key": "wk_live_not-a-key"})
    assert r.status_code == 401


def test_summary_is_scoped_to_one_workspace(env):
    tc, key_a, _, _ = env
    r = tc.get("/v1/workspace/summary", headers={"X-Workspace-Key": key_a})
    assert r.status_code == 200, r.text
    body = r.json()
    ids = [a["agent_id"] for a in body["agents"]]
    assert AGENT_A in ids and AGENT_B in ids
    assert FOREIGN not in ids          # tenant isolation: B's agent invisible to A
    assert body["totals"]["spend_cents"] == 250 + 750


def test_summary_agent_rows_carry_budget_and_activity(env):
    tc, key_a, _, secret_a = env
    r = tc.post("/v1/budget", headers={"AL-API-Version": AL_VERSION},
                json={"agent_id": AGENT_A, "monthly_cents": 1000,
                      "agent_secret": secret_a})
    assert r.status_code == 200, r.text

    r = tc.get("/v1/workspace/summary", headers={"X-Workspace-Key": key_a})
    rows = {a["agent_id"]: a for a in r.json()["agents"]}
    assert rows[AGENT_A]["spend_cents"] == 250
    assert rows[AGENT_A]["budget"]["monthly_cap_cents"] == 1000
    assert rows[AGENT_A]["budget"]["pct_used"] == 25.0
    assert rows[AGENT_A]["last_event_ts"]          # activity signal present
    assert rows[AGENT_B]["budget"] is None          # no budget set -> null, not {}


def test_summary_daily_series_is_zero_filled_and_windowed(env):
    tc, key_a, _, _ = env
    _backdate(AGENT_A, days=3)   # +400 cents three days ago
    r = tc.get("/v1/workspace/summary", params={"days": 7},
               headers={"X-Workspace-Key": key_a})
    series = r.json()["daily_series"]
    assert len(series) == 7                       # one bucket per day, holes filled
    dates = [b["date"] for b in series]
    assert dates == sorted(dates)                 # oldest first
    three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat()
    buckets = {b["date"]: b["spend_cents"] for b in series}
    assert buckets[three_days_ago] == 400         # the backdated spend landed
    today = datetime.now(timezone.utc).date().isoformat()
    assert buckets[today] == 250 + 750            # both agents' entries merged
    zero_days = [d for d, v in buckets.items() if v == 0 and d != three_days_ago and d != today]
    assert len(zero_days) == 5                    # silent days show as zero, not missing


def test_summary_token_totals_follow_the_tokens_rail_rule(env):
    tc, key_a, _, _ = env
    r = tc.get("/v1/workspace/summary", headers={"X-Workspace-Key": key_a})
    totals = r.json()["totals"]
    # AGENT_A's track carried tokens -> a companion rail="tokens" row was written
    assert totals["tokens_in"] == 100
    assert totals["tokens_out"] == 50


# --- CSV exports ---

def test_workspace_export_csv(env):
    tc, key_a, key_b, _ = env
    r = tc.get("/v1/workspace/export.csv", headers={"X-Workspace-Key": key_a})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    rows = list(_csv.DictReader(io.StringIO(r.text)))
    ids = {row["agent_id"] for row in rows}
    assert ids == {AGENT_A, AGENT_B}              # foreign agent never in A's export
    spend = sum(int(row["amount_cents"]) for row in rows
                if row["rail"] != "tokens")
    assert spend == 1000
    # the token bookkeeping row is exported too, as its own row
    tok = [row for row in rows if row["rail"] == "tokens"]
    assert tok and tok[0]["tokens_in"] == "100" and tok[0]["tokens_out"] == "50"


def test_workspace_export_csv_requires_key(env):
    tc, _, _, _ = env
    assert tc.get("/v1/workspace/export.csv").status_code == 401


def test_report_csv_for_one_agent(env):
    tc, key_a, _, _ = env
    r = tc.get(f"/v1/report/{AGENT_A}/csv", headers={"X-Workspace-Key": key_a})
    assert r.status_code == 200
    rows = list(_csv.DictReader(io.StringIO(r.text)))
    assert all(row["agent_id"] == AGENT_A for row in rows)
    assert len(rows) == 2                          # spend row + token bookkeeping row


def test_report_csv_needs_a_credential(env):
    tc, _, key_b, _ = env
    assert tc.get(f"/v1/report/{AGENT_A}/csv").status_code == 401
    # the WRONG workspace's key must not read A's agent either
    r = tc.get(f"/v1/report/{AGENT_A}/csv", headers={"X-Workspace-Key": "unset"})
    assert r.status_code == 401
    # and a VALID foreign workspace key must not either — the constraint that
    # actually matters: an authenticated neighbor cannot read A's entries
    r = tc.get(f"/v1/report/{AGENT_A}/csv", headers={"X-Workspace-Key": key_b})
    assert r.status_code == 401


def test_report_csv_rejects_negative_days(env):
    tc, key_a, _, _ = env
    r = tc.get(f"/v1/report/{AGENT_A}/csv", params={"days": -5},
               headers={"X-Workspace-Key": key_a})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_days"


def test_report_csv_days_filter_excludes_old_entries(env):
    tc, key_a, _, _ = env
    _backdate(AGENT_A, days=10)
    r = tc.get(f"/v1/report/{AGENT_A}/csv", params={"days": 5},
               headers={"X-Workspace-Key": key_a})
    rows = list(_csv.DictReader(io.StringIO(r.text)))
    dates = {row["date"] for row in rows}
    old = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
    assert old not in dates                        # windowed out
    # days=0 (default) is all-time
    r_all = tc.get(f"/v1/report/{AGENT_A}/csv", headers={"X-Workspace-Key": key_a})
    dates_all = {row["date"] for row in _csv.DictReader(io.StringIO(r_all.text))}
    assert old in dates_all


# --- the dashboard page ---

def test_dashboard_page_served(env):
    tc, _, _, _ = env
    r = tc.get("/dashboard")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    html = r.text
    assert "workspace key" in html.lower()         # the key-paste gate
    assert "/v1/workspace/summary" in html         # wired to the real API
    assert "sessionStorage" in html                # key held client-side, not in URLs


def test_dashboard_page_wired_to_real_share_response_shape(env):
    tc, _, _, _ = env
    # caught in browser QA: the share API returns {url: ...}, not
    # {share_url: ...} — a silent no-op if the page reads the wrong field.
    # This static check pins the page against that regression.
    html = tc.get("/dashboard").text
    assert "r.url" in html
    assert "share_url" not in html


def test_dashboard_page_loads_no_third_party_origins(env):
    tc, _, _, _ = env
    r = tc.get("/dashboard")
    html = r.text
    # no external script/style/img/font references — the browser holds a
    # workspace key while running this page; any third-party asset would be
    # in a position to observe it
    assert 'src="http' not in html
    assert "src='http" not in html
    assert 'href="http' not in html
    assert "@import" not in html
    assert "fetch(" in html and "X-Workspace-Key" in html


def test_dashboard_page_sends_hardening_headers(env):
    tc, _, _, _ = env
    r = tc.get("/dashboard")
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp
    assert "connect-src 'self'" in csp
    assert r.headers.get("x-frame-options") == "DENY"


# --- report JSON daily series (GAP-2) ---

def test_report_json_includes_daily_series(env):
    tc, key_a, _, _ = env
    r = tc.get(f"/v1/report/{AGENT_A}", headers={"X-Workspace-Key": key_a})
    series = r.json()["daily_series"]
    assert len(series) == 30                       # default window
    assert all({"date", "spend_cents", "tokens_in", "tokens_out"} == set(b) for b in series)
    today = datetime.now(timezone.utc).date().isoformat()
    today_bucket = [b for b in series if b["date"] == today][0]
    assert today_bucket["spend_cents"] == 250
    assert today_bucket["tokens_in"] == 100
    assert today_bucket["tokens_out"] == 50
