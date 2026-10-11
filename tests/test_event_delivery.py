#!/usr/bin/env python3
"""Tests for D-1625 — agent-facing event delivery.

Covers the wait-with-liveness feed (consumer registry, cursor acks,
redelivery on restart, bounded timeout), REST auth parity with
/v1/alerts, the AL-API-Current response header, the shared event
envelope, and the MCP tool surface.

Run with: /opt/miniconda3/bin/python3 -m pytest tests/test_event_delivery.py -q
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

AL_VERSION = "2026-09-01"


@pytest.fixture()
def env(monkeypatch):
    tmp_dir = tempfile.mkdtemp(prefix="agent-ledger-events-test-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp_dir)
    monkeypatch.setenv("AL_ADMIN_SECRET", "test-admin-secret")
    monkeypatch.delenv("AL_PRO_ACTIVE", raising=False)

    for mod in ("api_server", "ledger_engine", "metrics", "al_mcp_http",
                "routes_agents", "event_delivery", "alert_delivery",
                "workspace_engine", "identity"):
        sys.modules.pop(mod, None)

    import importlib
    import ledger_engine as le
    import workspace_engine
    importlib.reload(workspace_engine)
    _, ws_key = workspace_engine.create_workspace(
        owner_email="events-fixture@example.com")
    import event_delivery as ed
    import api_server as api
    from fastapi.testclient import TestClient
    tc = TestClient(api.app)

    def make_agent(agent_id="evt-agent-1"):
        """Claim an agent via the real write path; return its agent_secret."""
        r = tc.post("/v1/track", json={
            "agent_id": agent_id, "rail": "manual", "amount_cents": 10,
            "service": "fixture", "workspace_key": ws_key},
            headers={"AL-API-Version": AL_VERSION})
        assert r.status_code == 200, r.text
        return r.json()["agent_secret"]

    yield le, ed, tc, ws_key, make_agent
    shutil.rmtree(tmp_dir, ignore_errors=True)


def _alerts_file(env_tuple, agent_id):
    return Path(env_tuple[0].DATA_DIR) / "agents" / agent_id / "alerts.jsonl"


def _log(env_tuple, agent_id, alert_type="budget_warning", message="at 82%"):
    env_tuple[0]._log_alert(agent_id, alert_type, message)


# ── 1. registry / cursor / redelivery ─────────────────────────────────────

def test_new_consumer_gets_backlog_and_cursor(env):
    le, ed, *_ = env
    agent = "evt-agent-a"
    _log(env, agent, "budget_warning", "w1")
    _log(env, agent, "budget_exceeded", "x1")
    out = ed.wait(agent, consumer_id="c1", timeout=0)
    assert len(out["events"]) == 2
    assert out["cursor"] == 2
    assert all(e["kind"] in ("budget.warning", "budget.exceeded")
               for e in out["events"])
    assert all(e["redelivered"] is False for e in out["events"])
    # deterministic ids — same alert, same id
    assert out["events"][0]["id"].startswith("evt_")
    # a second call with no new alerts serves nothing further
    out2 = ed.wait(agent, consumer_id="c1", timeout=0)
    assert out2["events"] == []


def test_cursor_ack_then_new_events_only(env):
    le, ed, *_ = env
    agent = "evt-agent-b"
    _log(env, agent, message="a1")
    _log(env, agent, message="a2")
    first = ed.wait(agent, consumer_id="c2", timeout=0)
    # consumer acks everything it saw
    second = ed.wait(agent, consumer_id="c2", cursor=first["cursor"],
                     timeout=0)
    assert second["events"] == []
    _log(env, agent, message="a3")
    third = ed.wait(agent, consumer_id="c2", timeout=0)
    assert [e["summary"] for e in third["events"]] == ["a3"]


def test_restart_redelivers_unacked(env):
    le, ed, *_ = env
    agent = "evt-agent-c"
    _log(env, agent, message="held1")
    _log(env, agent, message="held2")
    # instance 1 consumes but never acks, then dies
    ed.wait(agent, consumer_id="c3", timeout=0, pid=111,
            started="2026-10-10T00:00:00Z")
    # instance 2: same consumer_id, new liveness
    out = ed.wait(agent, consumer_id="c3", timeout=0, pid=222,
                  started="2026-10-10T01:00:00Z")
    assert [e["summary"] for e in out["events"]] == ["held1", "held2"]
    assert all(e["redelivered"] is True for e in out["events"])


def test_restart_after_partial_ack_reserves_only_unacked(env):
    le, ed, *_ = env
    agent = "evt-agent-d"
    for m in ("m1", "m2", "m3"):
        _log(env, agent, message=m)
    first = ed.wait(agent, consumer_id="c4", timeout=0, pid=1, started="t0")
    # processed m1 only — ack cursor=1
    out = ed.wait(agent, consumer_id="c4", cursor=1, timeout=0,
                  pid=2, started="t1")
    # restart detected (new pid/started): unacked m2, m3 re-serve redelivered
    assert [e["summary"] for e in out["events"]] == ["m2", "m3"]
    assert all(e["redelivered"] for e in out["events"])


def test_same_liveness_no_redelivery(env):
    le, ed, *_ = env
    agent = "evt-agent-e"
    _log(env, agent, message="n1")
    ed.wait(agent, consumer_id="c5", timeout=0, pid=9, started="t9")
    out = ed.wait(agent, consumer_id="c5", timeout=0, pid=9, started="t9")
    assert out["events"] == []


def test_timeout_is_bounded(env):
    _, ed, *_ = env
    t0 = __import__("time").monotonic()
    out = ed.wait("evt-agent-f", consumer_id="c6", timeout=0.3)
    elapsed = __import__("time").monotonic() - t0
    assert elapsed < 1.5
    assert out["events"] == [] and out["waited"] is True
    # above the cap it clamps, not errors
    out2 = ed.wait("evt-agent-f", consumer_id="c6", timeout=9999)
    assert out2["events"] == []


def test_bad_consumer_params_raise_typed_errors(env):
    _, ed, *_ = env
    with pytest.raises(ed.ConsumerError):
        ed.wait("evt-agent-g", consumer_id="", timeout=0)
    with pytest.raises(ed.ConsumerError):
        ed.wait("evt-agent-g", consumer_id="bad id with spaces", timeout=0)
    with pytest.raises(ed.ConsumerError):
        ed.wait("evt-agent-g", consumer_id="ok", cursor=-1, timeout=0)


# ── 2. REST: auth parity + envelope + version header ──────────────────────

def _wait_url(agent):
    return f"/v1/agents/{agent}/events/wait?consumer_id=rest-c&timeout=0"


def test_wait_requires_credentials_like_alerts(env):
    _, _, tc, ws_key, make_agent = env
    secret = make_agent("evt-rest-1")
    r = tc.get(_wait_url("evt-rest-1"))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "agent_secret_mismatch"
    r = tc.get(_wait_url("evt-rest-1"),
               headers={"X-Agent-Secret": "wrong"})
    assert r.status_code == 401
    r = tc.get(_wait_url("evt-rest-1"),
               headers={"X-Agent-Secret": secret})
    assert r.status_code == 200
    r = tc.get(_wait_url("evt-rest-1"),
               headers={"X-Workspace-Key": ws_key})
    assert r.status_code == 200


def test_wait_route_serves_envelopes(env):
    le, _, tc, ws_key, make_agent = env
    secret = make_agent("evt-rest-2")
    le._log_alert("evt-rest-2", "budget_warning", "82% used")
    r = tc.get(_wait_url("evt-rest-2"),
               headers={"X-Agent-Secret": secret})
    assert r.status_code == 200
    body = r.json()
    assert body["consumer_id"] == "rest-c"
    assert len(body["events"]) == 1
    ev = body["events"][0]
    assert ev["kind"] == "budget.warning"
    assert ev["summary"] == "82% used"
    assert ev["context"]["agent_id"] == "evt-rest-2"
    assert ev["context"]["alert_type"] == "budget_warning"
    assert "alerts_since_last_write" in ev
    assert ev["redelivered"] is False


def test_invalid_consumer_gets_422(env):
    _, _, tc, ws_key, make_agent = env
    secret = make_agent("evt-rest-3")
    r = tc.get(f"/v1/agents/evt-rest-3/events/wait?timeout=0",
               headers={"X-Agent-Secret": secret})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_consumer"


def test_al_api_current_header_on_v1(env):
    le, _, tc, ws_key, make_agent = env
    secret = make_agent("evt-rest-4")
    for path in ("/v1/alerts/evt-rest-4",
                 "/v1/agents/evt-rest-4/events/wait?consumer_id=h&timeout=0",
                 "/v1/report/evt-rest-4"):
        r = tc.get(path, headers={"X-Agent-Secret": secret})
        assert r.headers.get("AL-API-Current") == AL_VERSION, \
            f"missing AL-API-Current on {path}"
    # and on a write
    r = tc.post("/v1/track",
                json={"agent_id": "evt-rest-4", "rail": "manual",
                      "amount_cents": 5, "service": "s",
                      "agent_secret": secret},
                headers={"AL-API-Version": AL_VERSION})
    assert r.headers.get("AL-API-Current") == AL_VERSION
    # not stamped on non-agent surfaces
    r = tc.get("/stats")
    assert "AL-API-Current" not in r.headers


# ── 3. webhook payload: additive envelope fields ──────────────────────────

def test_webhook_payload_keeps_legacy_keys_and_gains_envelope(env):
    le, ed, *_ = env
    import alert_delivery as ad
    agent = "evt-wh-1"
    captured = {}

    def fake_deliver_once(entry, payload):
        captured.update(payload)

    orig = ad.deliver_once
    ad.deliver_once = fake_deliver_once
    try:
        os.environ["AL_ALLOW_PRIVATE_WEBHOOKS"] = "1"
        import identity  # noqa: F401 — resolve workspace for register
        # register requires a resolvable public URL normally; private is
        # allowed only under the env flag used by tests
        ad._is_public_target = lambda url: True  # bypass DNS in test
        ws_file_dir = Path(le.DATA_DIR) / "agents" / agent
        ws_file_dir.mkdir(parents=True, exist_ok=True)
        # fabricate the workspace link the way claiming does
        import workspace_engine
        ws_list = [d for d in (le.DATA_DIR / "workspaces").glob("ws_*.json")]
        assert ws_list, "fixture workspace must exist on disk"
        ws_id = ws_list[0].stem
        (ws_file_dir / "workspace_id.txt").write_text(ws_id)
        entry = ad.register(ws_id, "http://127.0.0.1:9/hook")
        le._log_alert(agent, "budget_warning", "payload check")
        # dispatch is fire-and-forget on daemon threads; run the inner call
        payload = captured
        import time as _t
        for _ in range(50):
            if payload:
                break
            _t.sleep(0.05)
    finally:
        ad.deliver_once = orig
        os.environ.pop("AL_ALLOW_PRIVATE_WEBHOOKS", None)
    assert payload, "webhook never delivered"
    # legacy keys intact
    for k in ("event", "agent_id", "message", "timestamp", "report_url"):
        assert k in payload
    assert payload["event"] == "budget.warning"
    # envelope fields added
    for k in ("id", "kind", "summary", "context",
              "alerts_since_last_write", "redelivered"):
        assert k in payload
    assert payload["kind"] == "budget.warning"
    assert payload["summary"] == "payload check"
    assert payload["context"]["alert_type"] == "budget_warning"


# ── 4. MCP surface (in-process — the tools are plain functions) ───────────

def test_mcp_tool_auth_and_round_trip(env):
    le, _, tc, ws_key, make_agent = env
    secret = make_agent("evt-mcp-1")
    import al_mcp_http
    le._log_alert("evt-mcp-1", "spending_spike", "3x daily avg")
    # no credential -> typed error, same as REST's 401
    out = al_mcp_http.ledger_wait_events("evt-mcp-1", consumer_id="mcp-c",
                                         timeout=0)
    assert out["error_code"] == "agent_secret_mismatch"
    # agent_secret works
    out = al_mcp_http.ledger_wait_events("evt-mcp-1", consumer_id="mcp-c",
                                         agent_secret=secret, timeout=0)
    assert "events" in out and len(out["events"]) == 1
    assert out["events"][0]["kind"] == "anomaly.detected"
    # workspace_key works
    out = al_mcp_http.ledger_wait_events("evt-mcp-1", consumer_id="mcp-c2",
                                         workspace_key=ws_key, timeout=0)
    assert "events" in out


def test_ledger_start_carries_api_current(env):
    le, _, tc, ws_key, make_agent = env
    import al_mcp_http
    out = al_mcp_http.ledger_start()
    assert out.get("api_current") == AL_VERSION


def test_stdio_mcp_tool_round_trip(env):
    le, _, tc, ws_key, make_agent = env
    make_agent("evt-stdio-1")
    sys.modules.pop("mcp_server", None)
    import mcp_server
    le._log_alert("evt-stdio-1", "budget_warning", "stdio spike")
    out = mcp_server.ledger_wait_events("evt-stdio-1",
                                        consumer_id="stdio-c", timeout=0)
    assert len(out["events"]) == 1
    assert out["events"][0]["kind"] == "budget.warning"
    # unknown agent -> typed error, not a traceback
    out = mcp_server.ledger_wait_events("bad id!", consumer_id="c", timeout=0)
    assert "error" in out


# ── 5. review-hardening regressions (D-1625 adversarial review) ───────────

def test_feed_beyond_500_lines_still_walks_the_whole_file(env):
    """The cursor is an absolute line offset — a feed past the old 500-line
    scan window must not stall or skip."""
    le, ed, *_ = env
    agent = "evt-big-1"
    path = _alerts_file(env, agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for i in range(700):
            f.write(json.dumps({"type": "budget_warning",
                                "message": f"m{i}",
                                "timestamp": "2026-10-10T00:00:00+00:00"})
                    + "\n")
    seen = []
    cursor = None
    for _ in range(20):
        out = ed.wait(agent, consumer_id="bigc", cursor=cursor, timeout=0)
        seen += [e["summary"] for e in out["events"]]
        cursor = out["cursor"]
        if not out["events"]:
            break
    assert seen == [f"m{i}" for i in range(700)]


def test_same_consumer_concurrent_wait_gets_429(env):
    le, ed, *_ = env
    import threading
    agent = "evt-conc-1"
    t = threading.Thread(target=ed.wait,
                         args=(agent,), kwargs=dict(consumer_id="cc",
                                                    timeout=0.8),
                         daemon=True)
    t.start()
    __import__("time").sleep(0.1)  # let the first wait take its slot
    with pytest.raises(ed.WaitCongestedError):
        ed.wait(agent, consumer_id="cc", timeout=0)
    t.join()


def test_per_agent_wait_cap_does_not_starve_other_agents(env):
    le, ed, *_ = env
    import threading, time as _t

    def hold(agent_id, cid):
        try:
            ed.wait(agent_id, consumer_id=cid, timeout=2.0)
        except ed.WaitCongestedError:
            pass

    threads = [threading.Thread(target=hold, args=("evt-cap-a", f"c{i}"),
                                daemon=True)
               for i in range(ed.MAX_WAITS_PER_AGENT)]
    for t in threads:
        t.start()
    _t.sleep(0.2)
    # 5th wait for the SAME agent is refused — the cap is per-tenant
    with pytest.raises(ed.WaitCongestedError):
        ed.wait("evt-cap-a", consumer_id="overflow", timeout=0)
    # a different tenant still gets through
    out = ed.wait("evt-cap-b", consumer_id="other", timeout=0)
    assert out["events"] == []
    for t in threads:
        t.join()


def test_corrupt_consumers_file_is_quarantined_not_silent(env):
    le, ed, *_ = env
    agent = "evt-cor-1"
    ed.wait(agent, consumer_id="cx", timeout=0)
    cpath = Path(le.DATA_DIR) / "agents" / agent / "consumers.json"
    cpath.write_text('{"cx": {"acked": 0, "served": 3,')
    out = ed.wait(agent, consumer_id="cx", timeout=0)
    assert out["events"] == []
    quarantined = list(cpath.parent.glob("consumers.json.corrupt-*"))
    assert quarantined, "corrupt registry should be renamed aside"
    assert json.loads(cpath.read_text())["cx"]["served"] >= 0


def test_ack_cursor_past_eof_is_clamped_in_registry(env):
    le, ed, *_ = env
    agent = "evt-clamp-1"
    for m in ("m0", "m1", "m2"):
        _log(env, agent, message=m)
    out = ed.wait(agent, consumer_id="cl", cursor=10**9, timeout=0)
    assert out["cursor"] == 3
    cpath = Path(le.DATA_DIR) / "agents" / agent / "consumers.json"
    assert json.loads(cpath.read_text())["cl"]["acked"] == 3


def test_pid_without_started_counts_as_new_instance(env):
    """A reused pid with no `started` must over-deliver, not fake
    continuity — pids are recycled across reboots."""
    le, ed, *_ = env
    agent = "evt-nonce-1"
    _log(env, agent, message="held")
    first = ed.wait(agent, consumer_id="np", timeout=0, pid=4242)
    assert [e["summary"] for e in first["events"]] == ["held"]
    # same pid, still no started -> treated as a restart -> redelivery
    second = ed.wait(agent, consumer_id="np", timeout=0, pid=4242)
    assert [e["summary"] for e in second["events"]] == ["held"]
    assert second["events"][0]["redelivered"] is True


def test_consumer_registry_evicts_lru_at_cap(env):
    le, ed, *_ = env
    agent = "evt-evict-1"
    for i in range(ed.MAX_CONSUMERS_PER_AGENT):
        ed.wait(agent, consumer_id=f"ev-{i:02d}", timeout=0)
    # the 51st consumer must not be locked out forever — oldest goes
    out = ed.wait(agent, consumer_id="ev-new", timeout=0)
    assert out["consumer_id"] == "ev-new"
    cpath = Path(le.DATA_DIR) / "agents" / agent / "consumers.json"
    consumers = json.loads(cpath.read_text())
    assert len(consumers) == ed.MAX_CONSUMERS_PER_AGENT
    assert "ev-new" in consumers and "ev-00" not in consumers


def test_feed_and_webhook_share_one_event_id(env):
    """The same alert must name itself identically on both delivery paths —
    the dedupe contract the envelope id exists for."""
    le, ed, *_ = env
    import alert_delivery as ad
    agent = "evt-id-1"
    captured = {}
    orig_deliver, orig_public = ad.deliver_once, ad._is_public_target
    ad.deliver_once = lambda entry, payload: captured.update(payload)
    ad._is_public_target = lambda url: True
    ws_dir = Path(le.DATA_DIR) / "agents" / agent
    ws_dir.mkdir(parents=True, exist_ok=True)
    ws_id = [d.stem for d in (le.DATA_DIR / "workspaces").glob("ws_*.json")][0]
    (ws_dir / "workspace_id.txt").write_text(ws_id)
    ad.register(ws_id, "http://127.0.0.1:9/hook")
    os.environ["AL_ALLOW_PRIVATE_WEBHOOKS"] = "1"
    try:
        le._log_alert(agent, "budget_warning", "same alert, two paths")
        import time as _t
        for _ in range(50):
            if captured:
                break
            _t.sleep(0.05)
    finally:
        ad.deliver_once = orig_deliver
        ad._is_public_target = orig_public
        os.environ.pop("AL_ALLOW_PRIVATE_WEBHOOKS", None)
    feed = ed.wait(agent, consumer_id="idcheck", timeout=0)
    assert captured and feed["events"], "both paths must have delivered"
    assert feed["events"][0]["id"] == captured["id"]


def test_hosted_mcp_liveness_params_enable_redelivery(env):
    le, _, tc, ws_key, make_agent = env
    secret = make_agent("evt-mcp-live")
    import al_mcp_http
    le._log_alert("evt-mcp-live", "budget_warning", "unacked")
    al_mcp_http.ledger_wait_events(
        "evt-mcp-live", consumer_id="live-c", agent_secret=secret,
        client_instance="inst-1", client_started="t0", timeout=0)
    out = al_mcp_http.ledger_wait_events(
        "evt-mcp-live", consumer_id="live-c", agent_secret=secret,
        client_instance="inst-2", client_started="t1", timeout=0)
    assert out["events"][0]["summary"] == "unacked"
    assert out["events"][0]["redelivered"] is True


def test_stdio_mcp_supplies_process_liveness(env):
    le, _, tc, ws_key, make_agent = env
    make_agent("evt-stdio-live")
    sys.modules.pop("mcp_server", None)
    import mcp_server
    mcp_server.ledger_wait_events("evt-stdio-live",
                                  consumer_id="sv", timeout=0)
    cpath = Path(le.DATA_DIR) / "agents" / "evt-stdio-live" / "consumers.json"
    rec = json.loads(cpath.read_text())["sv"]
    assert rec["pid"] and rec["started"], \
        "stdio MCP must report its own process identity for redelivery"
