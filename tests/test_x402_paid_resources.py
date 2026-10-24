# tests/test_x402_paid_resources.py — D-1641 per-call x402-payable resources.
#
# The card's money invariants, exercised: unpaid gets the SDK's own 402
# envelope; a verified settlement gets the resource; a replayed tx_hash serves
# the cached resource instead of double-calling upstream; a settlement to a
# foreign recipient is refused; an upstream failure AFTER settlement never
# caches an error — the same payment must be re-servable.
import shutil, socket, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

import x402_verify


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, workspace_engine, identity, metrics
    import routes_satellite, routes_paid, routes_billing, api_server
    importlib.reload(workspace_engine)
    importlib.reload(ledger_engine)
    importlib.reload(identity)
    importlib.reload(metrics)
    importlib.reload(routes_satellite)
    importlib.reload(routes_paid)
    importlib.reload(routes_billing)
    importlib.reload(api_server)
    from fastapi.testclient import TestClient
    yield TestClient(api_server.app), api_server
    shutil.rmtree(tmp, ignore_errors=True)


def _public_dns(monkeypatch):
    def fake(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)


def _unpaid(monkeypatch, captured=None):
    def fake(request, route_key=""):
        if captured is not None:
            captured["route_key"] = route_key
        return {"verified": False, "payer_wallet": None, "tx_hash": None,
                "unpaid_response": {"status": 402,
                                    "headers": {"payment-required": "xyz"},
                                    "body": {"error": "payment required"}}}
    monkeypatch.setattr("x402_verify.verify_payment", fake)


def _paid(monkeypatch, wallet="0xBUYER", tx="0xTX1",
          recipient="0xOURTREASURY", amount="10000", captured=None):
    def fake(request, route_key=""):
        if captured is not None:
            captured["route_key"] = route_key
        return {"verified": True, "payer_wallet": wallet, "tx_hash": tx,
                "recipient": recipient, "amount": amount, "asset": "USDC",
                "network": "eip155:8453",
                "settlement_headers": {"PAYMENT-RESPONSE": "abc"},
                "unpaid_response": None}
    monkeypatch.setattr("x402_verify.verify_payment", fake)


# ── unpaid → 402 ─────────────────────────────────────────────────────────────

def test_probe_unpaid_returns_sdk_402(client, monkeypatch):
    _public_dns(monkeypatch)
    c, _ = client
    _unpaid(monkeypatch)
    r = c.post("/api/x402/agent-watch/probe",
               json={"url": "https://example.com/mcp/"})
    assert r.status_code == 402
    assert r.headers.get("payment-required") == "xyz"
    assert r.json()["error"] == "payment required"


def test_census_unpaid_returns_402(client, monkeypatch):
    c, _ = client
    _unpaid(monkeypatch)
    r = c.get("/api/x402/agent-watch/census")
    assert r.status_code == 402


def test_trustscan_unpaid_returns_402(client, monkeypatch):
    c, _ = client
    _unpaid(monkeypatch)
    r = c.post("/api/x402/trustscan/npm", json={"package": "express"})
    assert r.status_code == 402


def test_bad_input_is_a_free_422_never_charged(client, monkeypatch):
    """Input validation runs BEFORE charging — a bad package name is a 422 and
    verify_payment is never even called.

    D-3993: this protection belongs to a caller who has PRESENTED a payment.
    An unpaid caller is told the price first (see the discovery block below) —
    refusing it with a 422 is what hid the listing from sweepers."""
    c, _ = client
    called = {"n": 0}
    def spy(request, route_key=""):
        called["n"] += 1
        return {"verified": False, "unpaid_response": None}
    monkeypatch.setattr("x402_verify.verify_payment", spy)
    r = c.post("/api/x402/trustscan/npm", json={"package": "EXPRESS"},
               headers={"X-PAYMENT": "fake"})
    assert r.status_code == 422
    assert called["n"] == 0


# ── discovery: the price comes before the body judge ────────────────────────
#
# Measured live 2026-10-23 (D-3993): POST /api/x402/agent-watch/probe and
# POST /api/x402/trustscan/npm both answered 422 to an empty body, so a sweeper
# that probes blind learned no price and skipped the listing, while a listed
# peer answered 402 to the same empty POST. x402's discovery contract puts the
# challenge first; the payer's protection is that the body is judged BEFORE
# settlement — not that an unpaid caller is refused.

@pytest.mark.parametrize("path", ["/api/x402/agent-watch/probe",
                                  "/api/x402/trustscan/npm"])
@pytest.mark.parametrize("payload", [{}, None])
def test_unpaid_request_gets_the_price_even_with_no_usable_body(
        client, monkeypatch, path, payload):
    c, _ = client
    _unpaid(monkeypatch)
    r = c.post(path) if payload is None else c.post(path, json=payload)
    assert r.status_code == 402, (
        f"{path} answered {r.status_code} to an empty body — a sweeper never "
        f"sees a price here, so the listing cannot be discovered")
    assert r.headers.get("payment-required") == "xyz"


def test_probe_paid_but_invalid_body_is_422_and_never_settles(client, monkeypatch):
    """A caller who HAS presented a payment gets its body judged before any
    settlement: a 422 never costs the payer."""
    c, _ = client
    called = {"n": 0}
    def spy(request, route_key=""):
        called["n"] += 1
        return {"verified": True, "payer_wallet": "0xP", "tx_hash": "0xT"}
    monkeypatch.setattr("x402_verify.verify_payment", spy)
    r = c.post("/api/x402/agent-watch/probe", json={},
               headers={"X-PAYMENT": "fake"})
    assert r.status_code == 422
    assert called["n"] == 0, "settlement was attempted for a body we refused"


# ── paid → resource ──────────────────────────────────────────────────────────

def test_probe_paid_returns_probe_result(client, monkeypatch):
    _public_dns(monkeypatch)
    c, _ = client
    captured = {}
    _paid(monkeypatch, captured=captured)
    monkeypatch.setattr("routes_satellite._post_json", lambda *a, **k: (
        {"endpoint_url": "https://example.com/mcp/", "alive": True,
         "latency_p50_ms": 120, "latency_p95_ms": 300, "protocol": "mcp",
         "tool_count": 8, "price_challenge_found": False,
         "auth_metadata_present": True, "error": None,
         "probed_at": "2026-10-12T00:00:00Z"}, None))
    r = c.post("/api/x402/agent-watch/probe",
               json={"url": "https://example.com/mcp/"},
               headers={"X-PAYMENT": "signed"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["result"]["alive"] is True
    assert body["result"]["tool_count"] == 8
    # the route was priced by its own RouteConfig, not the mint's
    assert captured["route_key"] == "POST /api/x402/agent-watch/probe"
    # the settlement receipt header rides the paid response
    assert r.headers.get("payment-response") == "abc"


def test_census_paid_returns_census(client, monkeypatch):
    c, _ = client
    captured = {}
    _paid(monkeypatch, captured=captured)
    monkeypatch.setattr("routes_satellite._get_json", lambda *a, **k: (
        {"total": 500, "alive": 120, "dead": 300, "unknown": 80,
         "pct_alive": 24.0}, None))
    r = c.get("/api/x402/agent-watch/census", headers={"X-PAYMENT": "signed"})
    assert r.status_code == 200
    assert r.json()["census"]["alive"] == 120
    assert captured["route_key"] == "GET /api/x402/agent-watch/census"


def test_trustscan_paid_returns_verdict(client, monkeypatch):
    c, _ = client
    captured = {}
    _paid(monkeypatch, captured=captured)
    monkeypatch.setattr("routes_satellite.npm_trust_check", lambda name: {
        "ok": True, "found": True, "package": name,
        "summary": "established package"})
    r = c.post("/api/x402/trustscan/npm", json={"package": "express"},
               headers={"X-PAYMENT": "signed"})
    assert r.status_code == 200
    assert r.json()["package"] == "express"
    assert captured["route_key"] == "POST /api/x402/trustscan/npm"


# ── replay → cached ──────────────────────────────────────────────────────────

def test_replayed_tx_serves_cached_resource_no_double_upstream(client, monkeypatch):
    c, _ = client
    _paid(monkeypatch, tx="0xREPLAY")
    calls = {"n": 0}
    def fake_get(*a, **k):
        calls["n"] += 1
        return ({"total": 5, "alive": 3, "dead": 1, "unknown": 1,
                 "pct_alive": 60.0}, None)
    monkeypatch.setattr("routes_satellite._get_json", fake_get)
    r1 = c.get("/api/x402/agent-watch/census", headers={"X-PAYMENT": "p1"})
    r2 = c.get("/api/x402/agent-watch/census", headers={"X-PAYMENT": "p2"})
    assert r1.status_code == r2.status_code == 200
    assert calls["n"] == 1, "replay must serve the cached resource"
    assert r2.json() == r1.json()


# ── recipient mismatch → refuse ──────────────────────────────────────────────

def test_foreign_recipient_is_refused(client, monkeypatch):
    c, _ = client
    monkeypatch.setenv("X402_RECEIVING_ADDRESS", "0xOURTREASURY")
    _paid(monkeypatch, recipient="0xSOMEONEELSE")
    upstream = {"n": 0}
    monkeypatch.setattr("routes_satellite._get_json",
                        lambda *a, **k: upstream.update(n=upstream["n"] + 1))
    r = c.get("/api/x402/agent-watch/census", headers={"X-PAYMENT": "signed"})
    assert r.status_code == 402
    assert upstream["n"] == 0


def test_missing_recipient_refused_when_address_configured(client, monkeypatch):
    c, _ = client
    monkeypatch.setenv("X402_RECEIVING_ADDRESS", "0xOURTREASURY")
    _paid(monkeypatch, recipient=None)
    r = c.get("/api/x402/agent-watch/census", headers={"X-PAYMENT": "signed"})
    assert r.status_code == 402


# ── settle aftermath ─────────────────────────────────────────────────────────

def test_verified_without_tx_hash_refused(client, monkeypatch):
    """No tx_hash = no replay key = a payment that cannot be deduplicated."""
    c, _ = client
    _paid(monkeypatch, tx=None)
    r = c.get("/api/x402/agent-watch/census", headers={"X-PAYMENT": "signed"})
    assert r.status_code == 402


def test_upstream_failure_after_settlement_is_re_servable(client, monkeypatch):
    """The money moved but the resource failed: the error must NOT be cached —
    retrying the same tx_hash re-runs the work instead of 409ing."""
    c, _ = client
    _paid(monkeypatch, tx="0xFLAKY")
    state = {"fail": True, "n": 0}
    def flaky(*a, **k):
        state["n"] += 1
        if state["fail"]:
            return (None, "unreachable")
        return ({"total": 9, "alive": 9, "dead": 0, "unknown": 0,
                 "pct_alive": 100.0}, None)
    monkeypatch.setattr("routes_satellite._get_json", flaky)
    r1 = c.get("/api/x402/agent-watch/census", headers={"X-PAYMENT": "p"})
    assert r1.status_code == 502
    state["fail"] = False
    r2 = c.get("/api/x402/agent-watch/census", headers={"X-PAYMENT": "p"})
    assert r2.status_code == 200
    assert r2.json()["census"]["total"] == 9
    assert state["n"] == 2


def test_settled_resource_is_recorded_with_resource_tag(client, monkeypatch):
    """x402_settled must carry `resource` so the mint is distinguishable from
    per-call demand (the watch ping reads x402_paid_calls at /v1/metrics)."""
    c, api_server = client
    _paid(monkeypatch, wallet="0xREPEATER", tx="0xTAG1")
    monkeypatch.setattr("routes_satellite._get_json", lambda *a, **k: (
        {"total": 1, "alive": 1, "dead": 0, "unknown": 0, "pct_alive": 100.0},
        None))
    r = c.get("/api/x402/agent-watch/census", headers={"X-PAYMENT": "p"})
    assert r.status_code == 200
    import metrics
    paid = metrics.paid_calls_from_file()
    assert paid["by_resource"].get("aw_census") == 1
    assert paid["resource_calls"] == 1
    assert paid["resource_buyers"].get("aw_census") == 1


def test_repeat_buyer_is_counted(client, monkeypatch):
    """The card's pass signal: the SAME wallet settling twice for a resource
    shows up as a repeat buyer, not just two settlements."""
    c, _ = client
    monkeypatch.setattr("routes_satellite._get_json", lambda *a, **k: (
        {"total": 1, "alive": 1, "dead": 0, "unknown": 0, "pct_alive": 100.0},
        None))
    _paid(monkeypatch, wallet="0xREPEATER", tx="0xTA")
    c.get("/api/x402/agent-watch/census", headers={"X-PAYMENT": "p1"})
    _paid(monkeypatch, wallet="0xREPEATER", tx="0xTB")
    c.get("/api/x402/agent-watch/census", headers={"X-PAYMENT": "p2"})
    import metrics
    paid = metrics.paid_calls_from_file()
    assert paid["by_resource"].get("aw_census") == 2
    assert paid["repeat_resource_buyers"] == 1


# ── the one-trial gate must not trip resource routes ─────────────────────────

class _FakeURL:
    path = "/api/x402/agent-watch/probe"
    def __str__(self):
        return "http://test/api/x402/agent-watch/probe"


class _FakeRequest:
    def __init__(self):
        self.method = "POST"
        self.headers = {"x-payment": "hdr"}
        self.query_params = {}
        self.url = _FakeURL()


def test_repeat_payments_settle_on_every_route(monkeypatch):
    """A wallet that already paid must keep paying on every route — resource
    calls (the demand signal) and the mint route alike. Blueprint v2 / D-1645
    removed the one-trial gate entirely."""
    import importlib, workspace_engine, x402_verify
    importlib.reload(workspace_engine)
    from x402.http import HTTPRequestContext
    from x402.http.types import HTTPProcessResult, ProcessSettleResult

    class _Payload:
        payload = {"authorization": {"from": "0xREPEATWALLET"}}

    reqs = type("R", (), {"pay_to": "0xR", "amount": "10000",
                          "asset": "USDC", "network": "eip155:8453"})()
    outcome = HTTPProcessResult(type="payment-verified", response=None,
                                payment_payload=_Payload(),
                                payment_requirements=reqs)
    settle = ProcessSettleResult(success=True, transaction="0xT",
                                 payer="0xREPEATWALLET", network="eip155:8453")

    class _Server:
        def process_http_request(self, ctx):
            return outcome
        def process_settlement(self, payload, reqs, ctx):
            return settle

    monkeypatch.setattr(x402_verify, "X402_ENABLED", True)
    monkeypatch.setattr(x402_verify, "HTTPRequestContext", HTTPRequestContext)
    monkeypatch.setattr(x402_verify, "resource_server", _Server())
    # Blueprint v2 / D-1645: the one-trial-per-wallet gate is gone everywhere —
    # the mint pass is renewable, so a repeat wallet settles on the mint route
    # exactly as it does on a resource route.
    out = x402_verify.verify_payment(_FakeRequest(),
                                     route_key="POST /api/x402/agent-watch/probe")
    assert out["verified"] is True

    out = x402_verify.verify_payment(_FakeRequest())
    assert out["verified"] is True, (
        "a repeat wallet must settle on the mint route — the pass is renewable")


# ── discovery publishes the resources ────────────────────────────────────────

def test_wellknown_lists_paid_resources(client, monkeypatch):
    c, _ = client
    r = c.get("/.well-known/x402")
    assert r.status_code == 200
    res = r.json()["resources"]
    assert "POST https://aiagentscity.com/api/x402/agent-watch/probe" in res
    assert "GET https://aiagentscity.com/api/x402/agent-watch/census" in res
    assert "POST https://aiagentscity.com/api/x402/trustscan/npm" in res
    percall = {p["route"]: p for p in r.json()["perCall"]}
    # The published price must BE the configured price, so the assertion is
    # derived from the constant rather than pinned to a number. Pinned at 0.01 it
    # went red for a legitimate price move (D-1676) while staying green if the
    # document had served a hardcoded 0.01 the till never charged — this project's
    # most-repeated served-surface defect.
    expected = float(str(x402_verify.X402_RESOURCE_PRICE).lstrip("$"))
    assert percall["/api/x402/agent-watch/probe"]["priceUsd"] == expected
    assert percall["/api/x402/agent-watch/census"]["method"] == "GET"


def test_openapi_marks_resources_payable(client):
    c, _ = client
    r = c.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    expected = float(str(x402_verify.X402_RESOURCE_PRICE).lstrip("$"))
    for path, method in (("/api/x402/agent-watch/probe", "post"),
                         ("/api/x402/agent-watch/census", "get"),
                         ("/api/x402/trustscan/npm", "post")):
        op = paths[path][method]
        assert "402" in op["responses"], f"{path} missing 402 response"
        assert op["x-payment-info"]["x402"]["priceUsd"] == expected, (
            f"{method.upper()} {path} advertises a price that is not the "
            f"configured resource price {expected}")
        assert op["x-payment-info"]["x402"]["network"]


def test_mint_route_still_mints(client, monkeypatch):
    """The resource generalization must not disturb the existing mint flow."""
    c, _ = client
    monkeypatch.setattr(
        "x402_verify.verify_payment",
        lambda request, route_key="": {"verified": True,
                                       "payer_wallet": "0xABC",
                                       "tx_hash": "0xTXMINT"})
    r = c.post("/v1/billing/x402", headers={"X-PAYMENT": "fake"})
    assert r.status_code == 200
    assert "workspace_key" in r.json()
