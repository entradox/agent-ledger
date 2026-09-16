# tests/test_providers.py
"""Provider coverage — "count everything the agent calls, not based on source".

DeepSeek went uncounted for a plain reason: the proxy's provider list was
hard-coded to openai and anthropic, so every other vendor an agent called was
invisible to the ledger. Fixing DeepSeek alone would leave the next vendor
invisible in exactly the same way, so the fix is that providers come from
config (providers.json) and pricing is keyed by MODEL, not by who carries the
request.

The test that matters here is `test_a_provider_no_code_knows_about_still_meters`:
a provider whose name appears nowhere in the codebase works end to end.
"""
import json
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

AL_VERSION = "2026-09-01"
AGENT = "provider-agent"


class _Upstream(BaseHTTPRequestHandler):
    seen: list = []
    usage: dict = {}

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _Upstream.seen.append({"path": self.path, "body": body.decode(),
                               "headers": {k.lower(): v for k, v in self.headers.items()}})
        payload = json.dumps({"id": "x", "object": "chat.completion",
                              "usage": _Upstream.usage, "choices": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def env(monkeypatch):
    _Upstream.seen = []
    _Upstream.usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)

    import importlib
    import ledger_engine, workspace_engine, identity, alert_delivery
    import proxy, routes_proxy, routes_agents, api_server
    for module in (proxy, alert_delivery, ledger_engine, workspace_engine, identity,
                   routes_proxy, routes_agents, api_server):
        importlib.reload(module)

    # A provider file that names a vendor the code has never heard of.
    provider_file = Path(tmp) / "providers.json"
    provider_file.write_text(json.dumps({"providers": {
        "acme-inference": {
            "base_url": f"http://127.0.0.1:{port}",
            "credential_header": "authorization",
            "usage_in": "prompt_tokens",
            "usage_out": "completion_tokens",
            "usage_cache_hit": "prompt_cache_hit_tokens",
            "wire_format": "openai",
        },
        "deepseek": {
            "base_url": f"http://127.0.0.1:{port}",
            "credential_header": "authorization",
            "usage_in": "prompt_tokens",
            "usage_out": "completion_tokens",
            "usage_cache_hit": "prompt_cache_hit_tokens",
            "wire_format": "openai",
        },
    }}))
    monkeypatch.setattr(proxy, "PROVIDER_FILE", provider_file)

    from fastapi.testclient import TestClient
    tc = TestClient(api_server.app)
    _, key = workspace_engine.create_workspace(owner_email="a@example.com")
    r = tc.post("/v1/track", headers={"AL-API-Version": AL_VERSION},
                json={"agent_id": AGENT, "rail": "api_key", "amount_cents": 0,
                      "service": "seed", "workspace_key": key})
    assert r.status_code == 200, r.text
    yield tc, key, r.json()["agent_secret"]
    server.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)


def _proxy_call(tc, secret, provider, path="/v1/chat/completions", body=None):
    return tc.post(f"/proxy/{provider}{path}",
                   headers={"Content-Type": "application/json",
                            "Authorization": "Bearer vendor-key",
                            "X-AL-Agent": AGENT, "X-AL-Secret": secret},
                   json=body or {"model": "deepseek-v4-flash",
                                 "messages": [{"role": "user", "content": "hi"}]})


def _report(tc, secret):
    return tc.get(f"/v1/report/{AGENT}", headers={"X-Agent-Secret": secret}).json()


# ── the property: source-agnostic ──────────────────────────────────────────

def test_a_provider_no_code_knows_about_still_meters(env):
    """'acme-inference' appears in no source file. It works, it is metered, and
    the price came from the model's own entry — not from who carried the
    request. This is the difference between fixing DeepSeek and fixing the
    reason DeepSeek was invisible."""
    tc, key, secret = env
    _Upstream.usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    r = _proxy_call(tc, secret, "acme-inference")
    assert r.status_code == 200, r.text
    assert _report(tc, secret)["total_spend_cents"] == 42     # deepseek-v4-flash: 14c in + 28c out


def test_deepseek_is_now_a_first_class_provider(env):
    tc, key, secret = env
    _Upstream.usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    assert _proxy_call(tc, secret, "deepseek").status_code == 200
    assert _report(tc, secret)["total_spend_cents"] == 14     # $0.14 / 1M input


def test_an_unknown_provider_names_the_ones_that_exist(env):
    tc, key, secret = env
    r = _proxy_call(tc, secret, "not-a-vendor")
    assert r.status_code == 404
    assert "providers.json" in r.text
    assert "acme-inference" in r.text and "deepseek" in r.text


def test_pricing_is_keyed_by_model_not_by_provider(env):
    """The same model costs the same through any carrier — otherwise a caller
    could route around its own price by switching endpoints."""
    tc, key, secret = env
    import proxy
    assert proxy.lookup("deepseek-v4-flash")["in"] == 0.14
    assert proxy.lookup("deepseek-v4-flash")["provider"] == "deepseek"


# ── cache-aware pricing ────────────────────────────────────────────────────

def test_cached_input_is_priced_at_the_cache_rate(env):
    """DeepSeek bills cached input ~50x cheaper. Charging every input token at
    the cache-miss rate would overstate a cache-heavy workload enormously."""
    import proxy
    miss = proxy.cost_cents_exact("deepseek-v4-flash", 1_000_000, 0, cache_hit_in=0)
    hit = proxy.cost_cents_exact("deepseek-v4-flash", 1_000_000, 0, cache_hit_in=1_000_000)
    assert round(miss, 4) == 14.0
    assert round(hit, 4) == 0.28
    assert miss / hit > 40


def test_a_partially_cached_call_is_priced_across_both_rates(env):
    import proxy
    # 250k cached, 750k fresh, on deepseek-v4-flash
    cost = proxy.cost_cents_exact("deepseek-v4-flash", 1_000_000, 0, cache_hit_in=250_000)
    expected = (750_000 / 1_000_000 * 0.14 + 250_000 / 1_000_000 * 0.0028) * 100
    assert abs(cost - expected) < 0.0001


def test_cached_tokens_are_reported_for_a_real_call(env):
    tc, key, secret = env
    _Upstream.usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0,
                       "prompt_cache_hit_tokens": 1_000_000}
    assert _proxy_call(tc, secret, "deepseek").status_code == 200
    # 1M cached input at $0.0028 = 0.28 cents, which the residue carries rather
    # than rounding away
    rep = _report(tc, secret)
    assert rep["total_spend_cents"] == 0
    tokens = tc.get(f"/v1/tokens/{AGENT}", headers={"X-Agent-Secret": secret}).json()
    assert tokens["tokens_in"] == 1_000_000


def test_a_provider_without_cache_fields_prices_all_input_at_standard(env, monkeypatch):
    """Providers that report no cache split must behave exactly as before.

    Runs against a synthetic no-cache entry: gpt-4o-mini used to be the only
    such row, and it now carries OpenAI's real cached-input rate, so the real
    table no longer exercises this path.
    """
    import proxy
    table = dict(proxy.prices())
    table["test-no-cache-model"] = {"in": 15.0, "out": 60.0, "provider": "test"}
    monkeypatch.setattr(proxy, "prices", lambda: table)
    monkeypatch.setattr(proxy, "aliases", lambda: {})
    assert round(proxy.cost_cents_exact("test-no-cache-model", 1_000_000, 0), 4) == 1500.0
    assert proxy.cost_cents_exact("test-no-cache-model", 1_000_000, 0,
                                  cache_hit_in=1_000_000) == 1500.0


# ── the price table is honest about itself ─────────────────────────────────

def test_deepseek_prices_carry_their_source_and_are_not_marked_verified():
    import proxy
    entry = proxy.lookup("deepseek-v4-flash")
    assert entry["source"].startswith("https://api-docs.deepseek.com")
    assert entry["as_of"] == "2026-09-13"
    assert entry["verified"] is False, (
        "these came from a docs page summarised by a search result, not from an "
        "operator confirming them against the live pricing page")


def test_the_pricing_endpoint_says_how_many_are_unverified(env):
    tc, key, secret = env
    body = tc.get("/v1/pricing").json()
    # The endpoint must report how the numbers were obtained, and must not
    # imply everything is checked. Some entries are verified (Claude, read off
    # Anthropic's pricing page) and some are not (OpenAI, never checked).
    assert body["verified_count"] > 0
    assert body["count"] > body["verified_count"], (
        "nothing is flagged unverified — either the table really was fully "
        "checked, or the flag has stopped meaning anything")
    assert body["models"]["claude-sonnet-5"]["verified"] is True
    # OpenAI's rows were verified on 2026-09-15; DeepSeek's still are not (they
    # came from a docs page summarised by a search result, not an operator read).
    assert body["models"]["gpt-4o-mini"]["verified"] is True
    assert body["models"]["deepseek-v4-flash"]["verified"] is False
    assert body["models"]["deepseek-v4-flash"]["cache_hit"] == 0.0028
