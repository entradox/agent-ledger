# tests/test_proxy.py
"""D-1222 — the proxy. The first component here where a bug is
traffic-affecting rather than bookkeeping-affecting.

A fake upstream stands in for the provider. The test that matters most is
`test_a_blocked_call_never_reaches_the_provider`: every other cap in this
product blocks a ledger write after the money is gone, and the whole point of
the proxy is the opposite.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

AL_VERSION = "2026-09-01"
AGENT = "proxied-agent"
PROVIDER_KEY = "sk-test-provider-key"


class _Upstream(BaseHTTPRequestHandler):
    seen: list = []
    usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
    status = 200
    stream = False

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _Upstream.seen.append({
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body.decode(),
        })
        wants_stream = _Upstream.stream and '"stream": true' in body.decode()
        if wants_stream:
            lines = [
                'data: {"choices":[{"delta":{"content":"h"}}]}',
                "",
                'data: ' + json.dumps({"choices": [], "usage": {"prompt_tokens": 1000,
                                                               "completion_tokens": 500}}),
                "",
                "data: [DONE]",
                "",
            ]
            payload = ("\n".join(lines)).encode()
            self.send_response(_Upstream.status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = json.dumps({
            "id": "cmpl-1", "object": "chat.completion", "model": "gpt-4o-mini",
            "usage": _Upstream.usage,
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        }).encode()
        self.send_response(_Upstream.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def upstream(monkeypatch):
    _Upstream.seen = []
    _Upstream.usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
    _Upstream.status = 200
    _Upstream.stream = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("AL_PROXY_OPENAI_BASE", f"http://127.0.0.1:{server.server_address[1]}")
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def env(monkeypatch, upstream):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, workspace_engine, identity, alert_delivery
    import proxy, routes_proxy, routes_agents, api_server
    for module in (proxy, alert_delivery, ledger_engine, workspace_engine, identity,
                   routes_proxy, routes_agents, api_server):
        importlib.reload(module)
    from fastapi.testclient import TestClient
    tc = TestClient(api_server.app)
    _, key = workspace_engine.create_workspace(owner_email="a@example.com")
    r = tc.post("/v1/track", headers={"AL-API-Version": AL_VERSION},
                json={"agent_id": AGENT, "rail": "api_key", "amount_cents": 0,
                      "service": "seed", "workspace_key": key})
    assert r.status_code == 200, r.text
    yield tc, key, r.json()["agent_secret"]
    shutil.rmtree(tmp, ignore_errors=True)


def _call(tc, secret, body=None, headers=None,
          path="/proxy/openai/v1/chat/completions"):
    merged = {"Content-Type": "application/json",
              "Authorization": f"Bearer {PROVIDER_KEY}",
              "X-AL-Agent": AGENT, "X-AL-Secret": secret}
    merged.update(headers or {})
    return tc.post(path, headers=merged,
                   json=body or {"model": "gpt-4o-mini",
                                 "messages": [{"role": "user", "content": "hi"}]})


def _budget(tc, secret, cents):
    return tc.post("/v1/budget", headers={"AL-API-Version": AL_VERSION},
                   json={"agent_id": AGENT, "monthly_cents": cents,
                         "agent_secret": secret})


def _report(tc, secret):
    return tc.get(f"/v1/report/{AGENT}", headers={"X-Agent-Secret": secret}).json()


# ── THE property: a blocked call costs nothing ─────────────────────────────

def test_a_blocked_call_never_reaches_the_provider(env):
    """Every other cap here blocks a ledger write after the charge. This one
    must stop the money, which is only true if the upstream never sees it."""
    tc, key, secret = env
    _budget(tc, secret, 10000)                       # headroom to seed spend
    _Upstream.usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    assert _call(tc, secret).status_code == 200      # 75 cents
    _budget(tc, secret, 75)                          # cap now exactly consumed

    before = len(_Upstream.seen)
    r = _call(tc, secret)
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "budget_exceeded"
    assert len(_Upstream.seen) == before, "a blocked call reached the provider"


def test_a_blocked_call_does_not_change_the_ledger(env):
    tc, key, secret = env
    _budget(tc, secret, 10000)
    _Upstream.usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    _call(tc, secret)
    _budget(tc, secret, 75)
    total_before = _report(tc, secret)["total_spend_cents"]
    assert _call(tc, secret).status_code == 402
    assert _report(tc, secret)["total_spend_cents"] == total_before


def test_a_call_under_the_cap_is_forwarded(env):
    tc, key, secret = env
    _budget(tc, secret, 10000)
    r = _call(tc, secret)
    assert r.status_code == 200
    assert len(_Upstream.seen) == 1
    assert _Upstream.seen[0]["path"] == "/v1/chat/completions"


# ── metering comes from the provider, not the caller ───────────────────────

def test_cost_is_derived_from_the_provider_usage_block(env):
    """gpt-4o-mini: 0.15 in / 0.60 out per 1M → 1M + 1M = 75 cents."""
    tc, key, secret = env
    _Upstream.usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    assert _call(tc, secret).status_code == 200
    assert _report(tc, secret)["total_spend_cents"] == 75


def test_the_caller_cannot_under_report_what_it_owes(env):
    """Usage is read from the RESPONSE. Anything the caller puts in the request
    body is ignored, so a spend cap can't be dodged by lying on the way in."""
    tc, key, secret = env
    _Upstream.usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    r = _call(tc, secret, body={"model": "gpt-4o-mini",
                                "messages": [{"role": "user", "content": "hi"}],
                                "usage": {"prompt_tokens": 0, "completion_tokens": 0}})
    assert r.status_code == 200
    assert _report(tc, secret)["total_spend_cents"] == 15


def test_sub_cent_calls_accumulate_rather_than_rounding_away(env):
    """A dollar cap must not be dodgeable by making thousands of tiny calls.
    20 calls at ~0.075c each must record at least one whole cent."""
    tc, key, secret = env
    _Upstream.usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
    for _ in range(20):
        assert _call(tc, secret).status_code == 200
    assert _report(tc, secret)["total_spend_cents"] >= 1


# ── an unpriced model must be loud, never silently free ────────────────────

def test_an_unpriced_model_passes_through_and_raises_an_alert(env):
    tc, key, secret = env
    r = _call(tc, secret, body={"model": "model-that-does-not-exist-2099",
                                "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, "an unpriced model must never be blocked"
    alerts = tc.get(f"/v1/alerts/{AGENT}",
                    headers={"X-Agent-Secret": secret}).json()["alerts"]
    assert any(a["type"] == "unpriced_model" for a in alerts)
    # the real token burn is still recorded, even though the dollars are zero
    tokens = tc.get(f"/v1/tokens/{AGENT}",
                    headers={"X-Agent-Secret": secret}).json()
    assert tokens["total_tokens"] == 2000


def test_pricing_endpoint_shows_the_table_and_its_provenance(env):
    tc, key, secret = env
    body = tc.get("/v1/pricing").json()
    assert body["count"] > 0
    entry = body["models"]["gpt-4o-mini"]
    assert entry["in"] > 0 and entry["out"] > 0
    assert "check them against their provider" in body["_note"]
    assert body["verified_count"] == 0   # placeholders until an operator confirms


# ── identity: the agent is billed, the provider key is only carried ────────

def test_calls_without_an_agent_identity_are_refused(env):
    tc, key, secret = env
    r = tc.post("/proxy/openai/v1/chat/completions",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {PROVIDER_KEY}"},
                json={"model": "gpt-4o-mini", "messages": []})
    assert r.status_code == 401
    assert "X-AL-Agent" in r.text
    assert _Upstream.seen == []


def test_a_wrong_agent_secret_is_refused(env):
    tc, key, secret = env
    assert _call(tc, secret, headers={"X-AL-Secret": "nope"}).status_code == 401
    assert _Upstream.seen == []


def test_the_provider_credential_is_required(env):
    tc, key, secret = env
    r = tc.post("/proxy/openai/v1/chat/completions",
                headers={"Content-Type": "application/json",
                         "X-AL-Agent": AGENT, "X-AL-Secret": secret},
                json={"model": "gpt-4o-mini", "messages": []})
    assert r.status_code == 400
    assert "never stores it" in r.text


def test_the_provider_credential_is_forwarded_and_never_persisted(env):
    """Option A: pass-through means the key rides along and is not kept. If
    this ever fails, the product became a credential custodian by accident."""
    tc, key, secret = env
    assert _call(tc, secret).status_code == 200
    assert _Upstream.seen[0]["headers"]["authorization"] == f"Bearer {PROVIDER_KEY}"
    root = Path(os.environ["AGENT_LEDGER_DATA"])
    blob = "".join(p.read_text(errors="ignore")
                   for p in root.rglob("*") if p.is_file())
    assert PROVIDER_KEY not in blob


def test_unknown_provider_is_a_404(env):
    tc, key, secret = env
    r = _call(tc, secret, path="/proxy/gemini/v1/models")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_provider"


# ── streaming ──────────────────────────────────────────────────────────────

def test_a_streamed_call_passes_through_and_still_meters(env):
    tc, key, secret = env
    r = _call(tc, secret, body={"model": "gpt-4o-mini", "stream": True,
                                "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert "[DONE]" in r.text, "the stream was not passed through"
    # 1000 in + 500 out on gpt-4o-mini = 0.015 + 0.03 = 0.045c → rounds into residue
    # the tokens are the honest signal that it was metered at all
    tokens = tc.get(f"/v1/tokens/{AGENT}",
                    headers={"X-Agent-Secret": secret}).json()
    assert tokens["tokens_in"] == 1000 and tokens["tokens_out"] == 500


def test_a_streamed_call_asks_openai_for_usage(env):
    """Without stream_options.include_usage OpenAI never reports usage on a
    stream, so the call would be unmeterable. The proxy must request it."""
    tc, key, secret = env
    _call(tc, secret, body={"model": "gpt-4o-mini", "stream": True,
                            "messages": [{"role": "user", "content": "hi"}]})
    sent = json.loads(_Upstream.seen[0]["body"])
    assert sent.get("stream_options", {}).get("include_usage") is True
