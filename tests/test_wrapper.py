# tests/test_wrapper.py
"""BUILD-3 — the wrappers.

`agentledger.wrap(client, ...)` is meant to be the whole integration: one line
and an existing SDK is talking to the proxy. That only holds if the URL the SDK
actually requests is the URL the proxy expects.

The two SDKs join paths differently — the OpenAI client appends
"chat/completions" to its base, the Anthropic client appends a full
"/v1/messages" — so a wrong PROVIDER_BASE_PATHS entry does not raise, it 404s
silently. These tests call the real installed SDKs against a recording server
and assert the path that came out, because that is the only way to know.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

import agentledger


class _Recorder(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _Recorder.seen.append({
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body.decode(),
        })
        if "/messages" in self.path:      # anthropic shape
            payload = {"id": "msg_1", "type": "message", "role": "assistant",
                       "content": [{"type": "text", "text": "hi"}],
                       "model": "claude-sonnet-4-5",
                       "usage": {"input_tokens": 10, "output_tokens": 5}}
        else:                              # openai shape
            payload = {"id": "cmpl_1", "object": "chat.completion",
                       "model": "gpt-4o-mini",
                       "choices": [{"index": 0, "message": {"role": "assistant",
                                                            "content": "hi"},
                                    "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture
def recorder():
    _Recorder.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


# ── the integration promise: the right path, with the identity attached ────

def test_the_openai_sdk_talks_to_the_proxy_not_the_provider(recorder):
    import openai
    client = openai.OpenAI(api_key="sk-provider-key", base_url=recorder)
    agentledger.wrap(client, agent_id="wrap-agent", agent_secret="as_test",
                     base_url=recorder)
    client.chat.completions.create(model="gpt-4o-mini",
                                   messages=[{"role": "user", "content": "hi"}])
    last = _Recorder.seen[-1]
    assert last["path"] == "/proxy/openai/v1/chat/completions"
    assert last["headers"]["x-al-agent"] == "wrap-agent"
    assert last["headers"]["x-al-secret"] == "as_test"


def test_the_anthropic_sdk_talks_to_the_proxy_not_the_provider(recorder):
    import anthropic
    client = anthropic.Anthropic(api_key="sk-provider-key", base_url=recorder)
    agentledger.wrap(client, agent_id="wrap-agent", agent_secret="as_test",
                     base_url=recorder)
    client.messages.create(model="claude-sonnet-4-5", max_tokens=8,
                           messages=[{"role": "user", "content": "hi"}])
    last = _Recorder.seen[-1]
    assert last["path"] == "/proxy/anthropic/v1/messages"
    assert last["headers"]["x-al-agent"] == "wrap-agent"


def test_the_provider_credential_is_untouched_by_the_wrapper(recorder):
    """Pass-through means the wrapper must not read, move, or replace the key."""
    import openai
    client = openai.OpenAI(api_key="sk-provider-key", base_url=recorder)
    agentledger.wrap(client, agent_id="a", agent_secret="s", base_url=recorder)
    client.chat.completions.create(model="gpt-4o-mini",
                                   messages=[{"role": "user", "content": "hi"}])
    assert _Recorder.seen[-1]["headers"]["authorization"] == "Bearer sk-provider-key"
    # openai>=1.x keeps the key on `client.api_key` and builds the
    # Authorization header per-request — `default_headers` no longer contains
    # it (that shift is what broke the old assertion). The invariant that
    # matters is unchanged: the wrapper left the credential exactly as set.
    assert client.api_key == "sk-provider-key"


def test_wrapping_repoints_the_http_layer_too(recorder):
    """A stale _client.base_url would send traffic to the real provider — the
    silent bypass this wrapper exists to prevent."""
    import openai
    client = openai.OpenAI(api_key="sk-provider-key", base_url=recorder)
    agentledger.wrap(client, agent_id="a", agent_secret="s", base_url=recorder)
    assert str(client.base_url).startswith(recorder + "/proxy/openai")
    assert str(client._client.base_url).startswith(recorder + "/proxy/openai")


# ── refusing to guess ─────────────────────────────────────────────────────

def test_an_unrecognised_client_is_refused_rather_than_guessed(recorder):
    class SomethingElse:
        base_url = recorder
    with pytest.raises(TypeError) as e:
        agentledger.wrap(SomethingElse(), agent_id="a", agent_secret="s")
    assert "provider=" in str(e.value)


def test_an_unknown_provider_is_refused_with_the_reason(recorder):
    import openai
    client = openai.OpenAI(api_key="k", base_url=recorder)
    with pytest.raises(ValueError) as e:
        agentledger.wrap(client, agent_id="a", agent_secret="s", provider="gemini")
    assert "providers.json" in str(e.value)


def test_missing_identity_is_refused(recorder):
    import openai
    client = openai.OpenAI(api_key="k", base_url=recorder)
    with pytest.raises(ValueError):
        agentledger.wrap(client, agent_id="", agent_secret="s")
    with pytest.raises(ValueError):
        agentledger.wrap(client, agent_id="a", agent_secret="")


def test_the_api_base_can_come_from_the_environment(recorder, monkeypatch):
    import openai
    monkeypatch.setenv("AGENT_LEDGER_API_BASE", recorder)
    client = openai.OpenAI(api_key="k", base_url="https://api.openai.com/v1")
    agentledger.wrap(client, agent_id="a", agent_secret="s")
    assert str(client.base_url).startswith(recorder)
