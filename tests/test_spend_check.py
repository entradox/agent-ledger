# tests/test_spend_check.py
"""POST /v1/check and the ledger_check_spend MCP tool: ask before you spend.

An agent that is about to spend money (an LLM call, an x402 purchase, a paid
API) asks "may I?" and gets allowed/denied, a stable reason code, and the
headroom left. The load-bearing property is PARITY WITH THE PROXY: whatever the
check says about a request body, the proxy's 402 gate must do the same. If
they disagreed, an agent told "yes" would be refused a moment later and the
check would be worse than useless. Both run on spend_policy.decide().

The second property: a check is read-only. It must never record, reserve, or
move the totals a cap is enforced from.
"""
import asyncio
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
AGENT = "checking-agent"
V = {"AL-API-Version": AL_VERSION}


class _Upstream(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _Upstream.seen.append(self.path)
        body = json.dumps({"id": "c", "model": "gpt-4o-mini",
                           "usage": {"prompt_tokens": 10, "completion_tokens": 10},
                           "choices": [{"message": {"role": "assistant", "content": "k"}}]}
                          ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def env(monkeypatch):
    _Upstream.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("AL_PROXY_OPENAI_BASE", f"http://127.0.0.1:{server.server_address[1]}")
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, workspace_engine, identity, alert_delivery
    import proxy, spend_policy, routes_proxy, routes_agents, al_mcp_http, api_server
    for module in (proxy, alert_delivery, ledger_engine, workspace_engine, identity,
                   spend_policy, routes_proxy, routes_agents, al_mcp_http, api_server):
        importlib.reload(module)
    from fastapi.testclient import TestClient
    tc = TestClient(api_server.app)
    _, key = workspace_engine.create_workspace(owner_email="c@example.com")
    r = tc.post("/v1/track", headers=V, json={"agent_id": AGENT, "rail": "api_key",
                                              "amount_cents": 0, "service": "seed",
                                              "workspace_key": key})
    assert r.status_code == 200, r.text
    yield tc, key, r.json()["agent_secret"]
    server.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)


def _check(tc, secret, **body):
    return tc.post("/v1/check", headers={"X-Agent-Secret": secret},
                   json={"agent_id": AGENT, **body})


def _budget(tc, secret, monthly, daily=0, **extra):
    r = tc.post("/v1/budget", headers=V, json={"agent_id": AGENT, "monthly_cents": monthly,
                                               "daily_cents": daily, "agent_secret": secret,
                                               **extra})
    assert r.status_code == 200, r.text


def _spend(tc, secret, cents):
    r = tc.post("/v1/track", headers=V, json={"agent_id": AGENT, "rail": "x402",
                                              "amount_cents": cents, "service": "s",
                                              "agent_secret": secret})
    assert r.status_code == 200, r.text


def _total(tc, secret):
    return tc.get(f"/v1/report/{AGENT}",
                  headers={"X-Agent-Secret": secret}).json()["total_spend_cents"]


# ── the decision ────────────────────────────────────────────────────────────

def test_a_spend_that_fits_is_allowed_with_the_headroom(env):
    tc, _, secret = env
    _budget(tc, secret, 500)
    _spend(tc, secret, 100)
    r = _check(tc, secret, amount_cents=40)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["allowed"] is True and body["decision"] == "allow"
    assert body["reason"] == "within_budget"
    monthly = [w for w in body["windows"] if w["window"] == "monthly"][0]
    assert (monthly["cap"], monthly["spent"], monthly["remaining"], monthly["after"]) == \
        (500, 100, 400, 140)
    assert "360 cents of headroom" in body["message"]


def test_a_spend_that_would_cross_the_cap_is_denied_with_the_reason(env):
    tc, _, secret = env
    _budget(tc, secret, 500)
    _spend(tc, secret, 480)
    body = _check(tc, secret, amount_cents=40).json()
    assert body["allowed"] is False and body["decision"] == "deny"
    assert body["reason"] == "over_monthly_cap"
    assert "520" in body["message"] and "500" in body["message"]


def test_the_boundary_is_the_same_as_the_ledgers(env):
    """Exactly reaching the cap is allowed; one cent over is not. Same rule as
    track() and the proxy (spent + amount > cap), and the check must agree
    with what a real /v1/track of that amount would then do."""
    tc, _, secret = env
    _budget(tc, secret, 500)
    _spend(tc, secret, 460)
    assert _check(tc, secret, amount_cents=40).json()["allowed"] is True
    assert _check(tc, secret, amount_cents=41).json()["allowed"] is False
    over = tc.post("/v1/track", headers=V, json={"agent_id": AGENT, "rail": "x402",
                                                 "amount_cents": 41, "service": "s",
                                                 "agent_secret": secret})
    assert over.status_code == 402


def test_the_daily_cap_is_checked_too(env):
    tc, _, secret = env
    _budget(tc, secret, 10_000, daily=50)
    _spend(tc, secret, 30)
    body = _check(tc, secret, amount_cents=30).json()
    assert body["allowed"] is False and body["reason"] == "over_daily_cap"


def test_token_caps_are_checked_when_tokens_are_named(env):
    tc, _, secret = env
    _budget(tc, secret, 10_000, monthly_tokens=1000)
    body = _check(tc, secret, model="gpt-4o-mini", tokens_in=800, tokens_out=400).json()
    assert body["allowed"] is False and body["reason"] == "over_monthly_token_cap"


def test_no_budget_is_allowed_and_says_so(env, monkeypatch):
    """Never 'within_budget' when there is no budget: that would be a lie.

    New workspaces cap every agent by default (VALUE-BUILD-2), so an uncapped
    agent now only exists in a workspace minted before that default. Model one.
    """
    tc, _, _ = env
    import workspace_engine
    monkeypatch.setattr(workspace_engine, "DEFAULT_BUDGET", {})
    _, legacy_key = workspace_engine.create_workspace(owner_email="legacy@example.com")
    r = tc.post("/v1/track", headers=V, json={"agent_id": "legacy-agent", "rail": "api_key",
                                              "amount_cents": 0, "service": "seed",
                                              "workspace_key": legacy_key})
    body = tc.post("/v1/check", headers={**V, "X-Agent-Secret": r.json()["agent_secret"]},
                   json={"agent_id": "legacy-agent", "amount_cents": 99_999}).json()
    assert body["allowed"] is True and body["reason"] == "no_budget_set"
    assert "no budget" in body["message"]


def test_an_unpriced_model_is_allowed_but_never_called_within_budget(env):
    tc, _, secret = env
    _budget(tc, secret, 1)
    body = _check(tc, secret, model="no-such-model-xyz", tokens_in=10, tokens_out=10).json()
    assert body["allowed"] is True and body["reason"] == "unpriced_model"
    assert body["estimate_cents"] is None and body["price"] is None


# ── the price oracle half ───────────────────────────────────────────────────

def test_model_and_tokens_are_priced_from_the_table_with_provenance(env):
    """gpt-4o-mini: 0.15 in / 0.60 out per 1M -> 1M + 1M = 75 cents."""
    tc, _, secret = env
    body = _check(tc, secret, model="gpt-4o-mini",
                  tokens_in=1_000_000, tokens_out=1_000_000).json()
    assert body["estimate_cents"] == 75
    assert body["basis"] == "model_tokens"
    assert body["price"]["model"] == "gpt-4o-mini"
    assert body["price"]["in_per_million_usd"] == 0.15
    assert "verified" in body["price"] and "as_of" in body["price"]


def test_a_fractional_cent_rounds_up_like_the_proxy(env):
    tc, _, secret = env
    body = _check(tc, secret, model="gpt-4o-mini", tokens_in=1000, tokens_out=1000).json()
    assert body["estimate_cents"] == 1


# ── parity with the proxy: the whole point ──────────────────────────────────

@pytest.mark.parametrize("seeded", [0, 73, 74, 80])
def test_check_and_proxy_agree_on_the_same_request_body(env, seeded):
    """For a real request body the check's verdict must equal what the proxy
    does with that body: allowed <=> forwarded, denied <=> 402 and the provider
    never contacted. Swept across the cap boundary.

    The body reserves 100k output tokens (6c) plus ~1k input, so the proxy
    estimates 7c: seeded 73 lands exactly on the cap of 80, 74 is one over.
    An estimator that ignored max_tokens would say 1c and wrongly allow 74
    (canary: that mutation turns the [74] case red)."""
    tc, _, secret = env
    _budget(tc, secret, 80)
    if seeded:
        _spend(tc, secret, seeded)
    payload = {"model": "gpt-4o-mini", "max_tokens": 100_000,
               "messages": [{"role": "user", "content": "x" * 4000}]}
    verdict = _check(tc, secret, model="gpt-4o-mini", payload=payload).json()
    assert verdict["basis"] == "payload"
    assert verdict["allowed"] is (seeded <= 73), verdict
    before = len(_Upstream.seen)
    r = tc.post("/proxy/openai/v1/chat/completions",
                headers={"Authorization": "Bearer sk-x", "X-AL-Agent": AGENT,
                         "X-AL-Secret": secret},
                json=payload)
    if verdict["allowed"]:
        assert r.status_code == 200, r.text
        assert len(_Upstream.seen) == before + 1
    else:
        assert r.status_code == 402
        assert len(_Upstream.seen) == before, "a call the check denied reached the provider"


def test_a_token_cap_does_not_make_the_check_stricter_than_the_proxy(env):
    """The proxy never checks token caps before a call. A payload check that
    did would say 'deny' for a call the proxy forwards: the exact disagreement
    this endpoint exists to rule out."""
    tc, _, secret = env
    _budget(tc, secret, 10_000, monthly_tokens=10)
    payload = {"model": "gpt-4o-mini", "max_tokens": 100,
               "messages": [{"role": "user", "content": "hello"}]}
    verdict = _check(tc, secret, model="gpt-4o-mini", payload=payload).json()
    r = tc.post("/proxy/openai/v1/chat/completions",
                headers={"Authorization": "Bearer sk-x", "X-AL-Agent": AGENT,
                         "X-AL-Secret": secret}, json=payload)
    assert verdict["allowed"] is (r.status_code == 200), (verdict, r.status_code)


# ── read-only ───────────────────────────────────────────────────────────────

def test_a_check_records_nothing(env):
    tc, _, secret = env
    _budget(tc, secret, 500)
    _spend(tc, secret, 100)
    for _ in range(5):
        assert _check(tc, secret, amount_cents=300).json()["allowed"] is True
    assert _total(tc, secret) == 100
    body = _check(tc, secret, amount_cents=1)
    assert body.json()["recorded"] is False


# ── credentials and malformed questions ─────────────────────────────────────

def test_a_check_needs_the_agents_credential(env):
    tc, _, secret = env
    assert tc.post("/v1/check", json={"agent_id": AGENT, "amount_cents": 1}).status_code == 401
    r = _check(tc, "wrong-secret", amount_cents=1)
    assert r.status_code == 401


def test_the_workspace_key_can_check(env):
    tc, key, _ = env
    r = tc.post("/v1/check", headers={"X-Workspace-Key": key},
                json={"agent_id": AGENT, "amount_cents": 1})
    assert r.status_code == 200 and r.json()["allowed"] is True


def test_another_workspaces_key_cannot_check(env):
    tc, _, _ = env
    import workspace_engine
    _, other = workspace_engine.create_workspace(owner_email="other@example.com")
    r = tc.post("/v1/check", headers={"X-Workspace-Key": other},
                json={"agent_id": AGENT, "amount_cents": 1})
    assert r.status_code == 401


@pytest.mark.parametrize("body,code", [
    ({}, "nothing_to_check"),
    ({"model": "gpt-4o-mini"}, "nothing_to_check"),
    ({"amount_cents": 5, "model": "gpt-4o-mini"}, "ambiguous_spend"),
    ({"amount_cents": -1}, "invalid_amount"),
    ({"model": "gpt-4o-mini", "tokens_in": -5, "tokens_out": 10}, "invalid_tokens"),
])
def test_a_malformed_question_gets_a_typed_error(env, body, code):
    tc, _, secret = env
    r = _check(tc, secret, **body)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == code


# ── MCP: same answer, same credentials ──────────────────────────────────────

def test_mcp_tool_is_registered_and_read_only(env):
    import al_mcp_http as al
    result = al.mcp._list_tools()
    tools = asyncio.run(result) if hasattr(result, "__await__") else result
    tool = {t.name: t for t in tools}.get("ledger_check_spend")
    assert tool is not None, "ledger_check_spend is not a registered MCP tool"
    assert tool.annotations.read_only_hint is True


def test_mcp_and_rest_give_the_same_decision(env):
    tc, _, secret = env
    import al_mcp_http as al
    _budget(tc, secret, 500)
    _spend(tc, secret, 480)
    rest = _check(tc, secret, amount_cents=40).json()
    mcp = al.ledger_check_spend(AGENT, amount_cents=40, agent_secret=secret)
    for k in ("allowed", "reason", "estimate_cents", "windows", "message"):
        assert mcp[k] == rest[k], k


def test_mcp_refuses_without_a_credential(env):
    import al_mcp_http as al
    out = al.ledger_check_spend(AGENT, amount_cents=1)
    assert out.get("error_code") == "agent_secret_mismatch"
