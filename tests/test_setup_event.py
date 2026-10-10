#!/usr/bin/env python3
"""POST /v1/setup-event — the sink for `npx @aiagentscity/setup` (D-1626).

The installer runs before the caller has any credential, so this endpoint is
unauthenticated by necessity. What keeps that honest is pinned here: the
schema is closed (extra fields are a 422, not a shrug), every field is an enum
or a bounded format, it is rate-limited, only the enumerated fields are
persisted, and the only credential-adjacent input is a SHA-256 lookup hash —
a raw key that lands here by mistake is never stored.
"""
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

SETUP_RATE_LIMIT = 5


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="agent-ledger-setup-event-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    monkeypatch.setenv("AL_ADMIN_SECRET", "test-admin")
    # Generous by default — the enum-coverage test fires ~18 events; the rate
    # limit itself is exercised via monkeypatch in its own test.
    monkeypatch.setenv("SETUP_EVENT_RATE_LIMIT", "100")
    import importlib
    # ORDER MATTERS: api_server binds router objects at import time, so the
    # modules they came from must be reloaded BEFORE api_server or the app
    # keeps stale function objects and new code never runs.
    import metrics, ledger_engine, workspace_engine, identity
    import routes_agents, routes_billing, routes_setup
    importlib.reload(metrics)
    importlib.reload(ledger_engine)
    importlib.reload(workspace_engine)
    importlib.reload(identity)
    importlib.reload(routes_agents)
    import routes_setup as rs
    importlib.reload(rs)
    import api_server
    importlib.reload(api_server)
    from fastapi.testclient import TestClient
    return TestClient(api_server.app)


def _valid_event(**over):
    ev = {
        "product": "agent-ledger",
        "outcome": "registered",
        "harness": "claude-code",
        "is_human_initiated": True,
        "connect_attempt_id": str(uuid.uuid4()),
        "cli_version": "0.1.0",
        "runtime_platform": "darwin",
    }
    ev.update(over)
    return ev


def _post(client, ev, headers=None):
    return client.post("/v1/setup-event", json=ev, headers=headers or {})


def _metrics_lines(tmp_data):
    p = Path(tmp_data) / "metrics.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


# ── contract: closed schema, enumerated values ──────────────────────────────

def test_valid_event_accepted(client):
    r = _post(client, _valid_event())
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}


def test_every_harness_outcome_and_reason_accepted(client):
    harnesses = ["claude-code", "codex", "cursor", "opencode", "hermes", "openclaw"]
    outcomes = ["started", "registered", "already_registered", "manual",
                "failed", "dry_run", "skill_installed"]
    reasons = ["harness_not_supported", "command_not_found", "spawn_failed",
               "config_corrupt", "write_failed", "declined"]
    for i, h in enumerate(harnesses):
        assert _post(client, _valid_event(harness=h)).status_code == 200, h
    for o in outcomes:
        assert _post(client, _valid_event(outcome=o, harness=None)).status_code == 200, o
    for fr in reasons:
        assert _post(client, _valid_event(outcome="failed",
                                          failure_reason=fr)).status_code == 200, fr


def test_extra_field_rejected_422(client):
    r = _post(client, _valid_event(ssh_key="sk-notallowed"))
    assert r.status_code == 422


def test_unknown_harness_and_outcome_rejected(client):
    assert _post(client, _valid_event(harness="windsurf")).status_code == 422
    assert _post(client, _valid_event(outcome="exploded")).status_code == 422
    assert _post(client, _valid_event(product="other-product")).status_code == 422
    assert _post(client, _valid_event(failure_reason="arbitrary text")).status_code == 422


def test_attempt_id_must_be_uuid(client):
    assert _post(client, _valid_event(connect_attempt_id="not-a-uuid")).status_code == 422
    assert _post(client, _valid_event(connect_attempt_id=None)).status_code == 200


def test_freeform_fields_bounded(client):
    assert _post(client, _valid_event(cli_version="x" * 33)).status_code == 422
    assert _post(client, _valid_event(cli_version="v0.1.0<script>")).status_code == 422
    assert _post(client, _valid_event(runtime_platform="Darwin 23.5")).status_code == 422


def test_missing_required_field_rejected(client):
    for field in ("product", "outcome", "is_human_initiated"):
        ev = _valid_event()
        del ev[field]
        assert _post(client, ev).status_code == 422, field


# ── durability + enumeration discipline ─────────────────────────────────────

def test_persists_only_enumerated_fields(client, monkeypatch):
    tmp = tempfile.mkdtemp(prefix="al-se-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib, metrics
    importlib.reload(metrics)
    ev = _valid_event(outcome="failed", failure_reason="spawn_failed")
    assert _post(client, ev).status_code == 200
    recs = [r for r in _metrics_lines(tmp) if r.get("kind") == "setup_event"]
    assert len(recs) == 1
    allowed = {"kind", "ts", "product", "outcome", "harness",
               "is_human_initiated", "connect_attempt_id", "failure_reason",
               "cli_version", "runtime_platform"}
    assert set(recs[0].keys()) <= allowed
    assert recs[0]["outcome"] == "failed"
    assert recs[0]["failure_reason"] == "spawn_failed"


def test_credential_header_never_stored(client, monkeypatch):
    """`x-al-key-lookup` is accepted-and-dropped for BOTH shapes: a raw key
    is not stored, and a well-formed 64-hex digest is not stored either —
    nothing on this service reads the field, so nothing persists it."""
    tmp = tempfile.mkdtemp(prefix="al-se-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib, metrics
    importlib.reload(metrics)
    raw = "sk" + "_live_" + "51abcdefghijklmnopqrstuv"  # fixture: no literal in source
    hexshape = "deadbeef" * 8   # 64-hex — a raw secret could look like this
    for header_value in (raw, hexshape):
        assert _post(client, _valid_event(),
                     headers={"x-al-key-lookup": header_value}).status_code == 200
    text = (Path(tmp) / "metrics.jsonl").read_text()
    assert raw not in text
    assert hexshape not in text
    recs = [r for r in _metrics_lines(tmp) if r.get("kind") == "setup_event"]
    assert all("key_lookup" not in r for r in recs)


# ── rate limit ──────────────────────────────────────────────────────────────

def test_rate_limited(client, monkeypatch):
    import routes_setup
    monkeypatch.setattr(routes_setup, "SETUP_EVENT_RATE_LIMIT", SETUP_RATE_LIMIT)
    routes_setup._SETUP_RATE.clear()
    for _ in range(SETUP_RATE_LIMIT):
        assert _post(client, _valid_event()).status_code == 200
    assert _post(client, _valid_event()).status_code == 429


def test_rejected_traffic_counts_toward_limit(client, monkeypatch):
    """The guard runs before body validation, so a flood of malformed bodies
    is throttled, not just answered politely."""
    import routes_setup
    monkeypatch.setattr(routes_setup, "SETUP_EVENT_RATE_LIMIT", SETUP_RATE_LIMIT)
    routes_setup._SETUP_RATE.clear()
    for _ in range(SETUP_RATE_LIMIT):
        assert _post(client, _valid_event(evil="x")).status_code == 422
    # the (limit+1)th malformed request is a 429, not another 422
    assert _post(client, _valid_event(evil="x")).status_code == 429


def test_malformed_json_counts_toward_limit(client, monkeypatch):
    """Round-2 finding: Depends() sits downstream of FastAPI's JSON decode, so
    malformed-JSON floods bypassed the per-IP cap entirely. The route-class
    guard runs before the body is touched."""
    import routes_setup
    monkeypatch.setattr(routes_setup, "SETUP_EVENT_RATE_LIMIT", SETUP_RATE_LIMIT)
    routes_setup._SETUP_RATE.clear()
    for _ in range(SETUP_RATE_LIMIT):
        r = client.post("/v1/setup-event", content=b'{"product":',
                        headers={"content-type": "application/json"})
        assert r.status_code == 422
    r = client.post("/v1/setup-event", content=b'{"product":',
                    headers={"content-type": "application/json"})
    assert r.status_code == 429


def test_global_backstop_defeats_xff_rotation(client, monkeypatch):
    """Per-IP keying trusts the edge; the global bucket is what remains true
    even when every request claims a different source."""
    import routes_setup
    monkeypatch.setattr(routes_setup, "SETUP_EVENT_RATE_LIMIT", 1000)
    monkeypatch.setattr(routes_setup, "SETUP_EVENT_GLOBAL_LIMIT", SETUP_RATE_LIMIT)
    routes_setup._SETUP_RATE.clear()
    for i in range(SETUP_RATE_LIMIT):
        r = client.post("/v1/setup-event", json=_valid_event(),
                        headers={"x-forwarded-for": f"9.9.9.{i}"})
        assert r.status_code == 200
    r = client.post("/v1/setup-event", json=_valid_event(),
                    headers={"x-forwarded-for": "9.9.9.250"})
    assert r.status_code == 429


def test_no_ip_digest_persisted_for_setup_event_path(client, monkeypatch):
    """The per-request http metric records status for every path — but this
    endpoint is called pre-relationship, so it never lands an ip_hash."""
    tmp = tempfile.mkdtemp(prefix="al-se-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    monkeypatch.setenv("AL_METRICS_SALT", "test-salt")
    import importlib, metrics
    importlib.reload(metrics)
    assert _post(client, _valid_event()).status_code == 200
    recs = [r for r in _metrics_lines(tmp)
            if r.get("kind") == "http" and r.get("path") == "/v1/setup-event"]
    assert recs, "the http record should exist — status counts are the abuse signal"
    assert all(r.get("ip_hash") is None for r in recs)


def test_no_ip_digest_on_path_variants(client, monkeypatch):
    """The exemption is normalized: the trailing-slash redirect and case
    variants of the path must not carry an ip_hash either."""
    tmp = tempfile.mkdtemp(prefix="al-se-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    monkeypatch.setenv("AL_METRICS_SALT", "test-salt")
    import importlib, metrics
    importlib.reload(metrics)
    client.post("/v1/setup-event/", json=_valid_event(), follow_redirects=False)
    recs = [r for r in _metrics_lines(tmp)
            if r.get("kind") == "http" and "setup-event" in str(r.get("path", "")).lower()]
    assert recs
    assert all(r.get("ip_hash") is None for r in recs)


# ── the funnel read side ────────────────────────────────────────────────────

def _mint_via_page(client):
    """Render the page and pull the attempt id it minted — the only honest
    way to make an id the funnel will trust."""
    import re
    html = client.get("/").text
    ids = re.findall(r"--attempt ([0-9a-f-]{36})", html)
    assert ids, "home page should mint an attempt id into the npx command"
    return ids[0]


def test_setup_connect_funnel_visible_in_metrics(client, monkeypatch):
    tmp = tempfile.mkdtemp(prefix="al-se-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib, metrics
    importlib.reload(metrics)

    attempt = _mint_via_page(client)
    assert _post(client, _valid_event(outcome="started", harness=None,
                                      connect_attempt_id=attempt)).status_code == 200
    assert _post(client, _valid_event(outcome="registered",
                                      connect_attempt_id=attempt)).status_code == 200
    assert _post(client, _valid_event(outcome="failed", harness="codex",
                                      failure_reason="spawn_failed",
                                      connect_attempt_id=None)).status_code == 200

    r = client.get("/v1/metrics", headers={"X-AL-Admin": "test-admin"})
    assert r.status_code == 200, r.text
    funnel = r.json()["setup_connect_funnel"]
    assert funnel["by_outcome"] == {"failed": 1, "registered": 1, "started": 1}
    assert funnel["by_harness"] == {"claude-code": 1, "codex": 1}
    assert funnel["by_product"] == {"agent-ledger": 3}
    assert funnel["failure_reasons"] == {"spawn_failed": 1}
    assert funnel["attempts"]["minted"] == 1
    assert funnel["attempts"]["reported"] == 1
    assert funnel["attempts"]["succeeded"] == 1
    assert funnel["attempts"]["success_rate"] == 1.0
    assert funnel["attempts"]["unmatched"] == 0


def test_fabricated_attempt_id_counts_as_unmatched_not_success(client, monkeypatch):
    """An id no page minted cannot inflate the funnel — it is visible as
    unmatched traffic, which is the forgery signal."""
    tmp = tempfile.mkdtemp(prefix="al-se-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib, metrics
    importlib.reload(metrics)

    forged = str(uuid.uuid4())
    assert _post(client, _valid_event(outcome="started",
                                      connect_attempt_id=forged)).status_code == 200
    assert _post(client, _valid_event(outcome="registered",
                                      connect_attempt_id=forged)).status_code == 200

    funnel = client.get("/v1/metrics", headers={"X-AL-Admin": "test-admin"}
                        ).json()["setup_connect_funnel"]
    assert funnel["attempts"]["minted"] == 0
    assert funnel["attempts"]["reported"] == 0
    assert funnel["attempts"]["succeeded"] == 0
    assert funnel["attempts"]["unmatched"] == 1
    # the raw event counts still record the traffic — just never as conversion
    assert funnel["by_outcome"]["registered"] == 1


def test_metrics_endpoint_requires_admin(client):
    assert client.get("/v1/metrics").status_code == 401


# ── site attribution surface ────────────────────────────────────────────────

def test_connect_page_mints_distinct_attempt_ids(client):
    """The token is replaced per render — two page loads must not share an
    attempt id or the funnel joins them together."""
    a = client.get("/developers")
    b = client.get("/developers")
    assert a.status_code == 200 and b.status_code == 200
    import re
    ids_a = set(re.findall(r"--attempt ([0-9a-f-]{36})", a.text))
    ids_b = set(re.findall(r"--attempt ([0-9a-f-]{36})", b.text))
    assert ids_a and ids_b
    assert not ids_a & ids_b
    assert "__SETUP_ATTEMPT__" not in a.text


def test_home_and_llms_surface_installer(client):
    home = client.get("/")
    assert home.status_code == 200
    assert "npx @aiagentscity/setup ledger --attempt " in home.text
    llms = client.get("/llms.txt")
    assert llms.status_code == 200
    assert "npx @aiagentscity/setup ledger" in llms.text
    # the manual fallback stays visible — the installer is additive
    assert "claude mcp add --transport http agent-ledger" in llms.text


def test_head_render_does_not_mint_trusted_id(client, monkeypatch):
    """A headers-only probe must not inflate the funnel's denominator. The
    page still renders a usable --attempt uuid, but no setup_attempt_minted
    record is written for a HEAD request."""
    tmp = tempfile.mkdtemp(prefix="al-se-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    monkeypatch.setenv("AL_METRICS_SALT", "test-salt")
    import importlib, metrics
    importlib.reload(metrics)
    before = len([r for r in _metrics_lines(tmp) if r.get("kind") == "setup_attempt_minted"])
    r = client.head("/")
    assert r.status_code == 200
    mid = len([r for r in _metrics_lines(tmp) if r.get("kind") == "setup_attempt_minted"])
    r = client.get("/")
    assert r.status_code == 200
    after = [r for r in _metrics_lines(tmp) if r.get("kind") == "setup_attempt_minted"]
    assert mid == before, "HEAD minted a trusted id"
    assert len(after) == mid + 1, "GET minted exactly one"
    assert after[-1].get("method") == "GET"
