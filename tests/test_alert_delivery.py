# tests/test_alert_delivery.py
"""D-1218 — alerts that actually arrive.

Before this, budget warnings and anomalies existed only inside
`GET /v1/alerts/{agent_id}` — a feed you had to remember to poll. The audit's
line: "a spike alert you must remember to check is not an alert."

These tests run a real local HTTP sink and assert that the event physically
arrives, that a dead endpoint is recorded as failed rather than dropped, and
that the payload carries cost metadata and nothing else.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

AL_VERSION = "2026-09-01"
AGENT = "alert-agent"


class _Sink(BaseHTTPRequestHandler):
    received: list = []
    status_to_return = 200

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        try:
            _Sink.received.append(json.loads(body))
        except Exception:
            _Sink.received.append({"_raw": body})
        self.send_response(_Sink.status_to_return)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):
        pass


@pytest.fixture
def sink(monkeypatch):
    _Sink.received = []
    _Sink.status_to_return = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Sink)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # Webhook targets are SSRF-guarded; tests deliberately allow loopback.
    monkeypatch.setenv("AL_ALLOW_PRIVATE_WEBHOOKS", "1")
    yield f"http://127.0.0.1:{server.server_address[1]}/hook"
    server.shutdown()


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    monkeypatch.setenv("AL_ALLOW_PRIVATE_WEBHOOKS", "1")
    import importlib
    import ledger_engine, workspace_engine, identity, alert_delivery, routes_agents, api_server
    for m in (alert_delivery, ledger_engine, workspace_engine, identity,
              routes_agents, api_server):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    tc = TestClient(api_server.app)
    _, key = workspace_engine.create_workspace(owner_email="a@example.com")
    r = tc.post("/v1/track", headers={"AL-API-Version": AL_VERSION},
                json={"agent_id": AGENT, "rail": "api_key", "amount_cents": 100,
                      "service": "openai", "workspace_key": key})
    secret = r.json()["agent_secret"]
    yield tc, key, secret
    shutil.rmtree(tmp, ignore_errors=True)


def _track(tc, secret, cents):
    return tc.post("/v1/track", headers={"AL-API-Version": AL_VERSION},
                   json={"agent_id": AGENT, "rail": "api_key", "amount_cents": cents,
                         "service": "openai", "agent_secret": secret})


def _budget(tc, secret, monthly):
    return tc.post("/v1/budget", headers={"AL-API-Version": AL_VERSION},
                   json={"agent_id": AGENT, "monthly_cents": monthly,
                         "agent_secret": secret})


def _wait_for(predicate, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _register(tc, key, url, events=None):
    body = {"url": url}
    if events is not None:
        body["events"] = events
    return tc.post("/v1/webhooks", headers={"X-Workspace-Key": key}, json=body)


# ── the acceptance criterion: it physically arrives ────────────────────────

def test_crossing_80_percent_pushes_to_a_registered_webhook(env, sink):
    tc, key, secret = env
    assert _register(tc, key, sink).status_code == 200
    assert _budget(tc, secret, 5000).status_code == 200

    _track(tc, secret, 2000)         # $21 of $50 = 42% — nothing yet
    assert _Sink.received == []
    _track(tc, secret, 2000)         # $41 of $50 = 82% — crosses the threshold

    assert _wait_for(lambda: len(_Sink.received) > 0), "no delivery arrived"
    payload = _Sink.received[0]
    assert payload["event"] == "budget.warning"
    assert payload["agent_id"] == AGENT
    assert "%" in payload["message"]
    assert payload["timestamp"]


def test_a_blocked_write_pushes_budget_exceeded(env, sink):
    """The only moment "budget exceeded" is ever real: the rejected write.
    The entry that would cross 100% never lands, so it can never be noticed
    after the fact by a post-write check."""
    tc, key, secret = env
    _register(tc, key, sink, events=["budget.exceeded"])
    _budget(tc, secret, 5000)
    _track(tc, secret, 4000)                # $41 of $50 = 82%, under the cap
    r = _track(tc, secret, 2000)            # would be $61 -> blocked
    assert r.status_code == 402

    assert _wait_for(lambda: any(p.get("event") == "budget.exceeded"
                                 for p in _Sink.received)), "no exceeded event arrived"
    ev = [p for p in _Sink.received if p["event"] == "budget.exceeded"][0]
    assert "blocked" in ev["message"]


def test_the_events_filter_is_respected(env, sink):
    tc, key, secret = env
    _register(tc, key, sink, events=["budget.exceeded"])   # NOT warnings
    _budget(tc, secret, 5000)
    _track(tc, secret, 4000)                               # fires a warning
    time.sleep(0.4)
    assert [p for p in _Sink.received if p["event"] == "budget.warning"] == []


# ── failure must be visible, not silent ────────────────────────────────────

def test_a_dead_endpoint_is_recorded_as_failed(env, monkeypatch):
    tc, key, secret = env
    import alert_delivery
    monkeypatch.setattr(alert_delivery, "RETRY_DELAYS", (0.05, 0.05))
    # port 9 (discard) refuses connections quickly on loopback
    assert _register(tc, key, "http://127.0.0.1:9/nothing").status_code == 200
    _budget(tc, secret, 5000)
    _track(tc, secret, 4000)

    r = tc.get("/v1/webhooks/deliveries", headers={"X-Workspace-Key": key})
    assert _wait_for(lambda: tc.get("/v1/webhooks/deliveries",
                                    headers={"X-Workspace-Key": key}).json()["failed"] >= 1)
    body = tc.get("/v1/webhooks/deliveries", headers={"X-Workspace-Key": key}).json()
    failed = [d for d in body["deliveries"] if d["status"] != "delivered"]
    assert failed and failed[0]["attempts"] == 3      # tried, then gave up loudly
    assert failed[0]["error"]


def test_a_non_2xx_response_counts_as_failure(env, sink, monkeypatch):
    tc, key, secret = env
    import alert_delivery
    monkeypatch.setattr(alert_delivery, "RETRY_DELAYS", (0.05, 0.05))
    _Sink.status_to_return = 500
    _register(tc, key, sink)
    _budget(tc, secret, 5000)
    _track(tc, secret, 4000)
    assert _wait_for(lambda: tc.get("/v1/webhooks/deliveries",
                                    headers={"X-Workspace-Key": key}).json()["failed"] >= 1)


# ── payload hygiene and SSRF posture ───────────────────────────────────────

def test_payloads_carry_cost_metadata_and_never_a_secret(env, sink):
    tc, key, secret = env
    _register(tc, key, sink)
    _budget(tc, secret, 5000)
    _track(tc, secret, 4000)
    assert _wait_for(lambda: len(_Sink.received) > 0)
    raw = json.dumps(_Sink.received)
    assert secret not in raw
    for banned in ("agent_secret", "workspace_key", "prompt", "completion", "messages"):
        assert banned not in raw


def test_private_addresses_are_refused_by_default(env, monkeypatch):
    tc, key, _ = env
    monkeypatch.delenv("AL_ALLOW_PRIVATE_WEBHOOKS", raising=False)
    r = _register(tc, key, "http://127.0.0.1:8000/steal")
    assert r.status_code == 422
    assert "public address" in r.text


def test_email_destinations_are_not_accepted(env):
    """Removed 2026-09-13 by principal directive. The product does not send
    mail to arbitrary addresses on a user's behalf — the operator's SMTP rail
    exists to mail the operator about checkout, not to be a relaying surface.
    An address-shaped destination must be refused, not silently accepted."""
    tc, key, _ = env
    r = _register(tc, key, "alerts@example.com")
    assert r.status_code == 422
    assert "http" in r.text


def test_the_email_rail_is_actually_gone_from_the_module():
    """A pin, not a preference: if someone re-adds SMTP here, this fails."""
    src = (Path(__file__).resolve().parent.parent / "alert_delivery.py").read_text()
    assert "smtplib" not in src
    assert "MIMEText" not in src
    assert "ICLOUD_SMTP" not in src


def test_non_http_schemes_are_refused(env):
    tc, key, _ = env
    assert _register(tc, key, "file:///etc/passwd").status_code == 422
    assert _register(tc, key, "gopher://example.com/").status_code == 422


# ── registry lifecycle ─────────────────────────────────────────────────────

def test_registration_requires_a_workspace_key(env, sink):
    tc, _, _ = env
    r = tc.post("/v1/webhooks", json={"url": sink})
    assert r.status_code == 401
    assert tc.get("/v1/webhooks").status_code == 401
    assert tc.get("/v1/webhooks/deliveries").status_code == 401


def test_the_401_says_what_the_caller_was_actually_doing(env, sink):
    """The shared workspace-key gate was written for rotate/revoke. Reusing it
    verbatim told webhook callers they were 'rotating or revoking an
    agent_secret' — a machine-readable false instruction to an agent, which is
    the defect class this codebase keeps having to fix."""
    tc, _, _ = env
    body = tc.get("/v1/webhooks").text
    assert "alert delivery" in body
    assert "rotating or revoking" not in body


def test_list_then_delete(env, sink):
    tc, key, _ = env
    created = _register(tc, key, sink).json()["webhook"]
    listed = tc.get("/v1/webhooks", headers={"X-Workspace-Key": key}).json()
    assert listed["count"] == 1 and listed["webhooks"][0]["id"] == created["id"]
    assert created["events"] == ["alert.raised", "budget.warning",
                                 "budget.exceeded", "anomaly.detected"]
    assert tc.delete(f"/v1/webhooks/{created['id']}",
                     headers={"X-Workspace-Key": key}).status_code == 200
    assert tc.get("/v1/webhooks", headers={"X-Workspace-Key": key}).json()["count"] == 0
    assert tc.delete(f"/v1/webhooks/{created['id']}",
                     headers={"X-Workspace-Key": key}).status_code == 404


def test_another_workspace_cannot_see_or_delete_your_webhooks(env, sink):
    tc, key, _ = env
    import workspace_engine
    _, other_key = workspace_engine.create_workspace(owner_email="b@example.com")
    created = _register(tc, key, sink).json()["webhook"]
    assert tc.get("/v1/webhooks", headers={"X-Workspace-Key": other_key}).json()["count"] == 0
    assert tc.delete(f"/v1/webhooks/{created['id']}",
                     headers={"X-Workspace-Key": other_key}).status_code == 404


def test_event_mapping_is_total(env):
    """An unmapped alert type becomes alert.raised rather than being dropped —
    subscribing to the generic event must never silently miss something."""
    import alert_delivery
    assert alert_delivery.event_for("budget_warning") == "budget.warning"
    assert alert_delivery.event_for("token_budget_exceeded") == "budget.exceeded"
    assert alert_delivery.event_for("spending_spike") == "anomaly.detected"
    assert alert_delivery.event_for("something_brand_new") == "alert.raised"
