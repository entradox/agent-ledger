# tests/test_adversarial_fixes.py
"""Regressions for five defects found by an adversarial review, 2026-09-13.

Every one of these was invisible to a 365-test suite that passed. The reason
matters more than the fixes:

  * The streaming tests faked an OPENAI-shaped stream, where usage arrives in a
    single final chunk. Anthropic splits it — input and cache in message_start,
    the final output count in message_delta. A fixture that only ever produced
    the shape that works cannot fail on the shape that does not.
  * The estimate tests asserted the estimate on payloads containing only
    `messages`. The fields that were missing (tools, system, n) were the fields
    the test never sent.
  * The budget tests asserted that an over-cap write is REFUSED. None asserted
    what happens to a charge that was already spent before the write.

Each test below fails against the code as it was.
"""
import json
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

AL_VERSION = "2026-09-01"
AGENT = "adv-agent"

# The two events Anthropic actually sends, in order. message_start carries the
# input and cache counts; message_delta carries ONLY the final output count.
MSG_START = {"type": "message_start", "message": {"usage": {
    "input_tokens": 2, "cache_read_input_tokens": 900000,
    "cache_creation_input_tokens": 100000, "output_tokens": 1}}}
MSG_DELTA = {"type": "message_delta", "usage": {"output_tokens": 500}}


class _Upstream(BaseHTTPRequestHandler):
    """A fake provider that speaks the shapes the real ones do."""

    seen: list = []
    json_usage = {"prompt_tokens": 1000, "completion_tokens": 1000}

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        _Upstream.seen.append({"path": self.path, "body": body})

        # The proxy forwards to the provider's OWN path, so base_url overrides
        # are invisible here: Anthropic arrives as /v1/messages, OpenAI as
        # /v1/chat/completions. Branching on "anthropic" in the path never
        # matched and quietly served the OpenAI shape instead.
        if self.path.rstrip("/").endswith("/v1/messages"):
            lines = [
                "event: message_start", "data: " + json.dumps(MSG_START), "",
                "event: content_block_delta",
                'data: {"type":"content_block_delta","delta":{"text":"hi"}}', "",
                "event: message_delta", "data: " + json.dumps(MSG_DELTA), "",
                "event: message_stop", 'data: {"type":"message_stop"}', "",
            ]
            payload = ("\n".join(lines)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        payload = json.dumps({
            "id": "cmpl-1", "object": "chat.completion", "model": "gpt-4o",
            "usage": _Upstream.json_usage,
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    _Upstream.seen = []
    _Upstream.json_usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("AL_PROXY_OPENAI_BASE", base)
    monkeypatch.setenv("AL_PROXY_ANTHROPIC_BASE", base)

    import importlib
    import proxy, alert_delivery, ledger_engine, workspace_engine, identity
    import routes_proxy, routes_agents, api_server
    for module in (proxy, alert_delivery, ledger_engine, workspace_engine, identity,
                   routes_proxy, routes_agents, api_server):
        importlib.reload(module)
    from fastapi.testclient import TestClient
    tc = TestClient(api_server.app)
    _, key_a = workspace_engine.create_workspace(owner_email="a@example.com")
    _, key_b = workspace_engine.create_workspace(owner_email="b@example.com")
    r = tc.post("/v1/track", headers={"AL-API-Version": AL_VERSION},
                json={"agent_id": AGENT, "rail": "api_key", "amount_cents": 0,
                      "service": "seed", "workspace_key": key_a})
    assert r.status_code == 200, r.text
    yield tc, key_a, key_b, r.json()["agent_secret"], Path(tmp)
    server.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)


def _call(tc, secret, body, path="/proxy/openai/v1/chat/completions"):
    # The credential HEADER is provider-specific (OpenAI takes Authorization,
    # Anthropic takes x-api-key). Sending the wrong one is a 400 before the
    # request goes anywhere, which is what a first version of this test did.
    headers = {"Content-Type": "application/json",
               "X-AL-Agent": AGENT, "X-AL-Secret": secret}
    if "anthropic" in path:
        headers["x-api-key"] = "sk-ant-fake"
    else:
        headers["Authorization"] = "Bearer sk-fake"
    return tc.post(path, headers=headers, json=body)


# ── 1. Critical: a streamed Claude call metered ~1% of its cost ────────────

def test_a_streamed_anthropic_call_meters_its_input_tokens(env):
    """Anthropic's message_delta carries no input. Overwriting the captured
    usage with it recorded input tokens as ZERO on every streamed call — the
    dominant shape for the Claude integration, at about 1% of true cost."""
    tc, _, _, secret, _ = env
    r = _call(tc, secret, {"model": "claude-sonnet-5", "stream": True,
                           "max_tokens": 100,
                           "messages": [{"role": "user", "content": "hi"}]},
              path="/proxy/anthropic/v1/messages")
    assert r.status_code == 200
    assert "message_stop" in r.text

    tokens = tc.get(f"/v1/tokens/{AGENT}", headers={"X-Agent-Secret": secret}).json()
    assert tokens["tokens_in"] == 1000002, (
        "input + cache tokens from message_start were lost — the stream's final "
        "event overwrote them")
    assert tokens["tokens_out"] == 500


def test_a_streamed_anthropic_charge_is_not_near_zero(env):
    tc, _, _, secret, _ = env
    _call(tc, secret, {"model": "claude-sonnet-5", "stream": True, "max_tokens": 100,
                       "messages": [{"role": "user", "content": "hi"}]},
          path="/proxy/anthropic/v1/messages")
    rep = tc.get(f"/v1/report/{AGENT}", headers={"X-Agent-Secret": secret}).json()
    # 900k cache reads at $0.20/M + 100k writes at $2.50/M + 500 out ≈ 43c.
    # The bug produced 0-1c, which is the signature of input tokens being lost.
    assert rep["total_spend_cents"] >= 40, f"metered only {rep['total_spend_cents']}c"


def test_the_usage_of_a_stream_must_be_merged_not_overwritten():
    """The trap in isolation: the later event reports zero input, so any
    'last event wins' assignment loses the input count."""
    import proxy
    first = proxy.usage_tokens("anthropic", MSG_START)
    second = proxy.usage_tokens("anthropic", MSG_DELTA)
    assert first["tokens_in"] == 1000002
    assert second["tokens_in"] == 0, "if this ever changes, the merge is still correct"
    merged = proxy.merge_usage(first, second)
    assert merged["tokens_in"] == 1000002 and merged["tokens_out"] == 500
    # order must not matter
    assert proxy.merge_usage(second, first)["tokens_in"] == 1000002


# ── 2. High: the estimate ignored most of what drives cost ─────────────────

def test_the_estimate_counts_tools_and_system():
    """50 tool definitions and a top-level system prompt were completely
    invisible to the estimate, so the 402 gate could be walked straight past."""
    import proxy
    tools = [{"type": "function",
              "function": {"name": f"f{i}", "description": "x" * 500,
                           "parameters": {"type": "object", "properties": {}}}}
             for i in range(50)]
    assert proxy.estimate_prompt_tokens({"messages": [], "tools": tools}) > 1000
    assert proxy.estimate_prompt_tokens({"messages": [{"role": "user", "content": "hi"}]}) < 20
    assert proxy.estimate_prompt_tokens({"system": "y" * 40000, "messages": []}) > 1000


def test_n_multiplies_the_estimated_output():
    """n=128 completions of max_tokens each is 128x the output, and the gate
    read max_tokens alone."""
    import proxy
    assert proxy.requested_max_tokens({"max_tokens": 4096}) == 4096
    assert proxy.requested_max_tokens({"max_tokens": 4096, "n": 128}) == 4096 * 128
    one = proxy.call_estimate_cents("gpt-4o", {"messages": [{"role": "user", "content": "hi"}],
                                               "max_tokens": 4096})
    many = proxy.call_estimate_cents("gpt-4o", {"messages": [{"role": "user", "content": "hi"}],
                                                "max_tokens": 4096, "n": 128})
    assert many > one * 100


# ── 3. High: a charge that already happened vanished from the ledger ───────

def test_a_charge_that_already_happened_is_recorded_even_over_cap(env):
    """The provider charged; the write was refused; the cost disappeared from
    the totals the cap, the report and the alerts all read. Under-reporting the
    overspend it exists to surface is worse than not having the cap."""
    tc, _, _, secret, _ = env
    tc.post("/v1/budget", headers={"Content-Type": "application/json",
                                   "X-Agent-Secret": secret},
            json={"agent_id": AGENT, "monthly_cents": 20})
    # A small max_tokens keeps the ESTIMATE under the cap so the call goes out;
    # the provider then returns 128x more output than the caller asked for.
    _Upstream.json_usage = {"prompt_tokens": 1000, "completion_tokens": 524288}
    r = _call(tc, secret, {"model": "gpt-4o", "max_tokens": 10,
                           "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    rep = tc.get(f"/v1/report/{AGENT}", headers={"X-Agent-Secret": secret}).json()
    assert rep["total_spend_cents"] >= 500, (
        f"the ~524c charge was dropped: ledger shows {rep['total_spend_cents']}c")


def test_the_engine_can_record_an_already_spent_charge():
    """force=True is a reconciliation tool for money that has left the account."""
    import ledger_engine
    entry = ledger_engine.track(AGENT, "api_key", 999999, "manual", force=True)
    assert entry.amount_cents == 999999


def test_force_cannot_be_reached_from_a_request(env):
    """Anything a client can set defeats the cap, so no request model may carry
    it. This is the guard on the escape hatch."""
    import routes_agents
    for model in (routes_agents.TrackRequest, getattr(routes_agents, "BudgetRequest", None)):
        if model is None:
            continue
        assert "force" not in model.model_fields, (
            f"{model.__name__} exposes force — a caller could bypass its own cap")


# ── 4. High: the 403-vs-404 tenant existence oracle ────────────────────────

def test_a_foreign_agent_and_an_unknown_agent_are_indistinguishable(env):
    """403 for 'exists but is not yours' beside 404 for 'never existed' let any
    workspace_key enumerate other tenants' agent ids."""
    tc, _, key_b, _, _ = env
    for route in ("rotate-secret", "revoke-secret"):
        foreign = tc.post(f"/v1/agents/{AGENT}/{route}", headers={"X-Workspace-Key": key_b})
        unknown = tc.post("/v1/agents/nothing-here-ever/rotate-secret",
                          headers={"X-Workspace-Key": key_b})
        assert foreign.status_code == 404, f"{route} leaked existence via {foreign.status_code}"
        assert foreign.status_code == unknown.status_code
        # The error CODE and TYPE must match; the message echoes the id the
        # caller already supplied, so echoing it back reveals nothing.
        assert foreign.json()["error"]["code"] == unknown.json()["error"]["code"]
        assert foreign.json()["error"]["type"] == unknown.json()["error"]["type"]


# ── 5. Medium: unauthenticated writes into another tenant's state ──────────

def test_an_unauthenticated_write_creates_no_state_for_a_foreign_agent(env):
    """Pricing ran before the auth check, and pricing writes per-agent residue —
    so a wrong secret could create files under someone else's agent id."""
    tc, _, _, _, data_dir = env
    r = tc.post("/v1/track", headers={"AL-API-Version": AL_VERSION},
                json={"agent_id": "someone-elses-agent", "rail": "api_key",
                      "service": "openai", "model": "gpt-4o",
                      "tokens_in": 1000, "tokens_out": 10,
                      "agent_secret": "as_wrong_secret"})
    assert r.status_code in (401, 403, 404), r.text
    residue = data_dir / "agents" / "someone-elses-agent" / "proxy_residue.json"
    assert not residue.exists(), "an unauthenticated caller wrote per-agent state"
