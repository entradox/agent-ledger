# tests/test_workspace_dashboard.py
"""D-1226 — the workspace summary, the CSV exports, and /dashboard.

This is a NEW credential-bearing surface on a live product, so the tests lead
with isolation rather than with the happy path: a workspace key that can read
another workspace's ledger is a data breach, and the dashboard is the first
thing in the product that returns MANY agents at once, which is exactly the
shape where scoping bugs hide.
"""
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

AL_VERSION = "2026-09-01"


def _write_ledger(data_dir: Path, agent_id: str, workspace_id: str, rows: list):
    """Claim an agent the way the engine does (secret.txt + workspace_id.txt),
    then write ledger rows with the timestamps the test needs."""
    d = data_dir / "agents" / agent_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "secret.txt").write_text(f"as_test_{agent_id}")
    (d / "workspace_id.txt").write_text(workspace_id)
    with open(d / "ledger.jsonl", "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return d


def _row(agent_id, cents, service, days_ago, rail="api_key", tokens=None):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    row = {"agent_id": agent_id, "rail": rail, "amount_cents": cents,
           "service": service, "timestamp": ts}
    if tokens:
        row.update({"tokens_in": tokens[0], "tokens_out": tokens[1],
                    "model": "gpt-4o"})
    return row


@pytest.fixture
def env(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setenv("AGENT_LEDGER_DATA", str(tmp))
    import importlib
    import ledger_engine, workspace_engine, identity
    import routes_agents, routes_workspace, api_server
    for m in (ledger_engine, workspace_engine, identity, routes_agents,
              routes_workspace, api_server):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    tc = TestClient(api_server.app)
    _, key_a = workspace_engine.create_workspace(owner_email="a@example.com")
    _, key_b = workspace_engine.create_workspace(owner_email="b@example.com")
    import identity as _id
    ws_a = _id.resolve_workspace_key(key_a)
    ws_b = _id.resolve_workspace_key(key_b)

    _write_ledger(tmp, "agent-a1", ws_a, [
        _row("agent-a1", 100, "openai", 1),
        _row("agent-a1", 250, "openai", 0),
        _row("agent-a1", 0, "openai", 0, rail="tokens", tokens=(5000, 700)),
    ])
    _write_ledger(tmp, "agent-a2", ws_a, [_row("agent-a2", 40, "anthropic", 2)])
    _write_ledger(tmp, "agent-b1", ws_b, [_row("agent-b1", 99999, "openai", 0)])

    yield tc, key_a, key_b, ws_a, ws_b, tmp
    shutil.rmtree(tmp, ignore_errors=True)


def _summary(tc, key, days=30):
    return tc.get(f"/v1/workspace/summary?days={days}", headers={"X-Workspace-Key": key})


# ── isolation: the reason this surface needs tests before it ships ─────────

def test_a_workspace_sees_only_its_own_agents(env):
    tc, key_a, key_b, _, _, _ = env
    a = _summary(tc, key_a).json()
    b = _summary(tc, key_b).json()
    assert {x["agent_id"] for x in a["agents"]} == {"agent-a1", "agent-a2"}
    assert {x["agent_id"] for x in b["agents"]} == {"agent-b1"}
    # and the other workspace's money is nowhere in the payload
    assert "agent-b1" not in json.dumps(a)
    assert a["totals"]["spend_cents_30d"] == 390
    assert b["totals"]["spend_cents_30d"] == 99999


def test_the_csv_export_is_scoped_to_the_workspace(env):
    tc, key_a, key_b, _, _, _ = env
    a = tc.get("/v1/workspace/export.csv", headers={"X-Workspace-Key": key_a}).text
    b = tc.get("/v1/workspace/export.csv", headers={"X-Workspace-Key": key_b}).text
    assert "agent-b1" not in a
    assert "agent-a1" not in b
    assert "agent-b1" in b


def test_no_key_is_401_on_every_new_route(env):
    tc, *_ = env
    for path in ("/v1/workspace/summary", "/v1/workspace/export.csv"):
        assert tc.get(path).status_code == 401
    assert tc.get("/v1/workspace/summary",
                  headers={"X-Workspace-Key": "wk_live_not_a_real_key"}).status_code == 401


def test_a_bad_key_cannot_read_a_specific_agents_csv(env):
    tc, key_a, *_ = env
    assert tc.get("/v1/report/agent-a1/csv",
                  headers={"X-Workspace-Key": key_a}).status_code == 200
    assert tc.get("/v1/report/agent-a1/csv").status_code == 401
    assert tc.get("/v1/report/agent-a1/csv",
                  headers={"X-Agent-Secret": "as_wrong"}).status_code == 401


def test_admin_endpoints_are_still_owner_only(env):
    """The dashboard must not have opened a door to the operator surface."""
    tc, key_a, *_ = env
    for path in ("/v1/dashboard", "/v1/agents", "/v1/metrics"):
        r = tc.get(path, headers={"X-Workspace-Key": key_a})
        assert r.status_code in (401, 403), f"{path} answered {r.status_code} to a workspace key"


# ── the data itself ────────────────────────────────────────────────────────

def test_the_daily_series_spans_the_days_that_have_spend(env):
    tc, key_a, *_ = env
    d = _summary(tc, key_a).json()
    series = {p["date"]: p["spend_cents"] for p in d["daily_series"]}
    today = datetime.now(timezone.utc).date().isoformat()
    yday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    assert series[today] == 250          # agent-a1 only, today
    assert series[yday] == 100
    # oldest first, so a chart can be drawn straight from it
    dates = [p["date"] for p in d["daily_series"]]
    assert dates == sorted(dates)


def test_token_rows_are_burn_not_spend(env):
    """A rail='tokens' row is 0 cents by construction. If one ever counted as
    spend the dashboard would disagree with the report."""
    tc, key_a, *_ = env
    d = _summary(tc, key_a).json()
    assert d["totals"]["spend_cents_30d"] == 390
    assert d["totals"]["tokens_in_30d"] == 5000
    assert d["totals"]["tokens_out_30d"] == 700


def test_the_window_is_respected(env):
    """The window is "the last N days" measured back from NOW, not from midnight
    — so days=1 is the last 24 hours and a row exactly 24h old falls outside it.
    Worth pinning: a calendar-day window would quietly include different data."""
    tc, key_a, *_ = env
    assert _summary(tc, key_a, days=1).json()["totals"]["spend_cents_30d"] == 250  # today only
    assert _summary(tc, key_a, days=2).json()["totals"]["spend_cents_30d"] == 350  # + the 1d row
    assert _summary(tc, key_a, days=3).json()["totals"]["spend_cents_30d"] == 390  # + the 2d row
    assert tc.get("/v1/workspace/summary?days=0",
                  headers={"X-Workspace-Key": key_a}).status_code == 422
    assert tc.get("/v1/workspace/summary?days=99999",
                  headers={"X-Workspace-Key": key_a}).status_code == 422


def test_budget_status_reflects_a_real_cap(env):
    tc, key_a, *_ = env
    import ledger_engine
    ledger_engine.set_budget("agent-a1", monthly_cents=390)
    d = _summary(tc, key_a).json()
    a1 = [x for x in d["agents"] if x["agent_id"] == "agent-a1"][0]
    assert a1["budget"]["monthly_cents"] == 390
    assert a1["budget"]["used_pct"] == 89.7
    assert a1["budget"]["status"] == "warning"


# ── CSV shape ──────────────────────────────────────────────────────────────

def test_the_csv_has_a_header_and_one_row_per_entry(env):
    tc, key_a, *_ = env
    r = tc.get("/v1/workspace/export.csv", headers={"X-Workspace-Key": key_a})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    lines = [l for l in r.text.splitlines() if l.strip()]
    assert lines[0].split(",")[:5] == ["timestamp", "agent_id", "rail", "service",
                                       "amount_cents"]
    assert len(lines) == 5  # header + 4 entries (3 for a1, 1 for a2)
    assert any("tokens" == l.split(",")[2] for l in lines[1:])


# ── the page ───────────────────────────────────────────────────────────────

def test_the_dashboard_forbids_every_external_resource(env):
    """A page that holds a workspace key must not be able to load a third-party
    script, so no CDN and no vendored chart library — inline SVG from the JSON.

    This scans the SERVED BYTES, not the source template, so a URL that only
    ever reaches the page through a JSON body (a webhook the user registered)
    is not a false positive. What must never appear is a reference the browser
    would resolve on its own: a scheme, a CDN host, or an external origin.
    """
    tc, *_ = env
    r = tc.get("/dashboard")
    assert r.status_code == 200
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp
    assert "connect-src 'self'" in csp
    body = r.text
    for external in ("cdn.", "unpkg", "jsdelivr", "googleapis",
                     "http://", "https://", "//hooks.", "integrity="):
        assert external not in body, f"the page references {external!r}"
    # The webhook section is part of the page, and it must carry no scheme at
    # all until the user types one — a placeholder is where this leaked before.
    assert "whurl" in body and "Register webhook" in body


def test_the_page_itself_contains_no_secret_and_no_data(env):
    """The HTML is static. Nothing to leak from a cache, a log, or a URL."""
    tc, key_a, *_ = env
    import re
    body = tc.get("/dashboard").text
    assert key_a not in body
    assert "agent-a1" not in body
    # "wk_live_" appears as a bare prefix in the helper text and the placeholder.
    # What must never appear is a full key, so assert against the key's shape.
    assert not re.search(r"wk_live_[A-Za-z0-9_-]{20,}", body), "a real key is in the page"
    assert tc.get("/dashboard").headers.get("cache-control") == "no-store"
