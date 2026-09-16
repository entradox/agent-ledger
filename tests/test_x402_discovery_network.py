# tests/test_x402_discovery_network.py
"""D-1232: discovery documents must match the CONFIGURED network, in both modes.

The defect this pins: discovery prose hardcoded "TESTNET ONLY ... a mainnet wallet
cannot complete it". That became false the moment X402_NETWORK flipped to mainnet,
and it would actively tell paying agents to stay away from a working payment path.

This file was previously a SCRIPT (it ran its checks at import via a module-level
loop and `sys.exit`). pytest collected ZERO tests from it, so `pytest` reported
success while none of these assertions were ever evaluated — and a real
contradiction on `/start` survived inside the "pinned" surface. Converted to real
test functions so the suite enforces it. Keep them as tests, not a script.

D-1312 widened the net. Two surfaces were still serving the stale claim after
mainnet landed: `/agent-ledger` (served from status.html with no substitution at
all) and the MCP tool `ledger_api_docs` (docs_content.py, never routed through the
derived helper). Both are fetched here now. Note the OLD 'mcp' key fetched
/.well-known/mcp.json — a DIFFERENT surface from the MCP tool list, which is
exactly how the gap looked covered. The tool surface is reached by a real
tools/call, under the keys prefixed `mcp_`.

Run: /opt/miniconda3/bin/python3 -m pytest tests/test_x402_discovery_network.py -q
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PAYTO = "0x363c520492EDbA89057bCe696B74263B3295a72A"
TESTNET, MAINNET = "eip155:84532", "eip155:8453"
USDC_TESTNET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
USDC_MAINNET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# Phrases that are only ever true on a testnet. A mainnet surface carrying any of
# them is telling a paying agent the paywall will not work for them.
TESTNET_ONLY_PHRASES = ("TESTNET ONLY", "84532", "Sepolia", "cannot complete it")

# Every surface fetched in both modes. The set is asserted below, so dropping one
# by accident fails loudly rather than silently removing coverage.
ALL_SURFACES = ("llms", "start", "agents", "agentjson", "x402", "mcp",
                "agentledger", "mcp_rest_docs", "mcp_all_docs")

# The child is real Python, not a string built out of escaped newlines: it stays
# readable, editable, and free of the backslash-escaping traps that come with
# concatenating a script one line at a time.
CHILD = '''import os, sys, json
sys.path.insert(0, "@@REPO@@")
from fastapi.testclient import TestClient
import api_server as a

H = {"Accept": "application/json, text/event-stream",
     "Content-Type": "application/json"}


def mcp_call(c, name, args):
    """A real tools/call against the mounted MCP app.

    D-1312: THIS is the surface `ledger_api_docs` serves. The guard used to
    assert on /.well-known/mcp.json under a key named "mcp", which is how a
    testnet-only claim survived in the MCP docs while the test passed.
    """
    r = c.post("/mcp/", headers=H, json={"jsonrpc": "2.0", "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "guard", "version": "1"}}})
    h = dict(H)
    sid = r.headers.get("mcp-session-id")
    if sid:
        h["mcp-session-id"] = sid
    c.post("/mcp/", headers=h,
           json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    r = c.post("/mcp/", headers=h, json={"jsonrpc": "2.0", "id": 2,
        "method": "tools/call",
        "params": {"name": name, "arguments": args}})
    for line in r.text.splitlines():
        if line.startswith("data:"):
            payload = json.loads(line[5:].strip())
            return json.loads(payload["result"]["content"][0]["text"])["markdown"]
    return r.text


# As a context manager so the FastMCP lifespan runs. Without it the MCP session
# manager is uninitialized and every tools/call raises -- a harness failure that
# would otherwise look like a product failure.
with TestClient(a.app) as c:
    out = {
      'agentjson': c.get('/.well-known/agent-card.json').text,
      'x402': c.get('/.well-known/x402').text,
      'x402json': c.get('/.well-known/x402.json').text,
      'llms': c.get('/llms.txt').text,
      'agents': c.get('/agents.txt').text,
      'mcp': c.get('/.well-known/mcp.json').text,
      'start': c.get('/start').text,
      'agentledger': c.get('/agent-ledger').text,
      'mcp_rest_docs': mcp_call(c, 'ledger_api_docs', {'topic': 'rest'}),
      'mcp_all_docs': mcp_call(c, 'ledger_api_docs', {}),
      'openapi': c.get('/openapi.json').json(),
    }
print('===JSON===')
print(json.dumps(out))
'''


def render(network, cdp=True):
    """Boot the app in a subprocess and fetch the discovery documents.

    Subprocess on purpose: the network is read at import time, so an in-process
    reload cannot exercise both modes in one run.

    CDP creds are supplied by default because a mainnet network without them is
    REFUSED by design (x402_verify._init raises rather than advertising a network
    no configured facilitator can settle). That refusal is itself asserted below.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("X402_", "CDP_"))}
    env.update({"X402_PAY_TO": PAYTO, "X402_NETWORK": network,
                "AGENT_LEDGER_DATA": "/tmp/al_disc_test"})
    if cdp:
        env.update({"CDP_API_KEY_ID": "test-key-id",
                    "CDP_API_KEY_SECRET": "test-key-secret"})
    code = CHILD.replace("@@REPO@@", str(REPO))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, cwd=str(REPO))
    if r.returncode != 0:
        return {"error": r.stderr.strip()[-600:]}
    for line in r.stdout.splitlines():
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                pass
    return {"error": "no json in output: " + r.stdout[-300:]}


@pytest.fixture(scope="module")
def docs():
    """Both network modes, rendered once."""
    return {net: render(net) for net in (TESTNET, MAINNET)}


# ── both modes must serve ─────────────────────────────────────────────────────

@pytest.mark.parametrize("net", [TESTNET, MAINNET])
def test_app_serves_discovery_in_both_modes(docs, net):
    d = docs[net]
    if "error" in d:
        pytest.fail(f"app failed to serve under {net}: {d['error'][:400]}")


@pytest.mark.parametrize("net", [TESTNET, MAINNET])
def test_every_guarded_surface_is_actually_fetched(docs, net):
    """A guard can only pin what it actually fetched.

    Kept as a test so a typo in a key — or a surface quietly dropped from the
    child dict — fails here instead of removing coverage. That is precisely how
    /agent-ledger and the MCP tool surface went unchecked through D-1232.
    """
    missing = [s for s in ALL_SURFACES if s not in docs[net]]
    assert not missing, f"guard never fetched: {missing}"


# ── the configured network is what discovery advertises ───────────────────────

@pytest.mark.parametrize("net,asset,is_main", [
    (TESTNET, USDC_TESTNET, False),
    (MAINNET, USDC_MAINNET, True),
])
def test_x402_document_matches_configured_network(docs, net, asset, is_main):
    d = docs[net]
    payload = json.loads(d["x402"])
    assert payload["network"] == net, \
        f"/.well-known/x402 must advertise {net}"
    assert payload["mainnet"] is is_main, \
        "the mainnet flag must match the configured network"
    assert payload["asset"] == asset, \
        f"/v1 billing doc must carry the {net} USDC address"


@pytest.mark.parametrize("net,wrong_asset", [
    (TESTNET, USDC_MAINNET),
    (MAINNET, USDC_TESTNET),
])
def test_never_advertises_the_wrong_networks_token(docs, net, wrong_asset):
    """The token address is what an agent actually pays in."""
    d = docs[net]
    assert wrong_asset not in d["x402"], \
        f"advertised the wrong network's USDC on {net}"


# ── the load-bearing one: no surface may tell agents NOT to pay ───────────────
# This is the assertion that catches the real-world failure. On mainnet, a page
# saying "a mainnet wallet cannot complete it" drives paying agents away.

@pytest.mark.parametrize("surface", ALL_SURFACES)
def test_mainnet_never_claims_testnet_only(docs, surface):
    text = docs[MAINNET][surface]
    for phrase in TESTNET_ONLY_PHRASES:
        assert phrase not in text, (
            f"{surface} carries {phrase!r} while running on mainnet — this is "
            f"the sentence that drives paying agents away from a paywall that "
            f"settles real USDC")
    assert USDC_TESTNET not in text, \
        f"{surface} advertises the testnet USDC token on mainnet"


@pytest.mark.parametrize("surface", ALL_SURFACES)
def test_no_surface_leaks_an_unsubstituted_placeholder(docs, surface):
    """The D-1270 bug class, across every guarded surface.

    A page that renders `{X402_SETTLEMENT_HTML}` literally means no .replace()
    ran on it — the same wiring gap that let /agent-ledger keep a stale literal
    for a whole release while seven other surfaces were verified.
    """
    text = docs[MAINNET][surface] + docs[TESTNET][surface]
    assert "{X402_SETTLEMENT" not in text, \
        f"{surface} rendered an unsubstituted settlement placeholder"


# ── the negative control: the fix must be DERIVED, not a deletion ────────────
# Without this, "fixing" it by deleting the sentence outright would pass every
# mainnet assertion above — and leave a testnet deployment lying to its users.

@pytest.mark.parametrize("surface", ["agentledger", "mcp_rest_docs"])
def test_testnet_still_discloses_testnet(docs, surface):
    """D-1312's two surfaces must FOLLOW the network, in both directions."""
    text = docs[TESTNET][surface]
    assert "TESTNET" in text.upper(), \
        f"{surface} must disclose testnet mode when configured for testnet"
    assert "84532" in text, \
        f"{surface} must name the testnet network it actually settles on"
    assert USDC_MAINNET not in text, \
        f"{surface} must not advertise mainnet USDC while on testnet"


def test_quickstart_docs_disclose_testnet_in_testnet_mode(docs):
    """The quickstart block carried the same hardcoded claim as the REST block,
    so both are covered — one guard that misses a sibling block is how this
    defect survived three times."""
    text = docs[TESTNET]["mcp_all_docs"]
    assert "TESTNET" in text.upper(), \
        "the MCP docs bundle must disclose testnet mode when on testnet"


def test_mainnet_start_page_advertises_a_working_payment_path(docs):
    """`/start` is the page a prospective payer reads.

    Regression: it carried BOTH the derived mainnet sentence AND a stale literal
    'Mainnet arrives when Coinbase CDP onboarding completes', contradicting itself.
    """
    start = docs[MAINNET]["start"]
    assert "MAINNET" in start.upper(), "/start must state it settles on mainnet"
    assert "arrives when" not in start.lower(), \
        "/start must not claim mainnet is still pending while it is live"


def test_mainnet_landing_page_advertises_a_working_payment_path(docs):
    """Regression (D-1312): /agent-ledger said 'Testnet only right now ... a
    mainnet wallet cannot complete it' while the endpoint settled real mainnet
    USDC. It is the first page a human or crawler reads."""
    page = docs[MAINNET]["agentledger"]
    assert "MAINNET" in page.upper(), \
        "/agent-ledger must state it settles on mainnet"
    assert "pending onboarding" not in page.lower(), \
        "/agent-ledger must not claim mainnet is still pending while it is live"


def test_agents_txt_payment_status_follows_the_network(docs):
    """Regression (D-1312): /agents.txt carried a THIRD copy of the claim —
    'It is not real money yet. Mainnet is pending onboarding.' — which the
    hardcoded guard phrase list never matched, so it passed on mainnet."""
    for net, expected in ((MAINNET, "MAINNET"), (TESTNET, "TESTNET")):
        body = docs[net]["agents"]
        assert expected in body.upper(), \
            f"/agents.txt PAYMENT STATUS must state {expected} under {net}"
        assert "pending onboarding" not in body.lower(), \
            "/agents.txt must not claim mainnet is pending"


def test_testnet_mode_still_discloses_testnet(docs):
    """The mirror assertion: testnet must not masquerade as mainnet.

    Covers llms.txt (D-1232) and the two D-1312 surfaces are asserted
    separately below so a failure names the surface that broke.
    """
    assert "TESTNET" in docs[TESTNET]["llms"].upper(), \
        "llms.txt must disclose testnet mode"
    assert USDC_MAINNET not in docs[TESTNET]["llms"], \
        "must not advertise mainnet USDC while on testnet"


def test_mainnet_without_cdp_creds_is_refused_not_advertised():
    """A config that could never settle must fail loudly, not publish a 402.

    Without this, X402_NETWORK=mainnet plus no CDP creds would advertise a mainnet
    demand the public x402.org facilitator cannot settle (it serves testnets only),
    so every payment would fail at settlement with no explanation.
    """
    d = render(MAINNET, cdp=False)
    assert "error" not in d, "app should still serve, with x402 disabled"
    payload = json.loads(d["x402"])
    assert payload.get("enabled") is False, \
        "mainnet without CDP creds must report x402 disabled rather than enabled"


# ── discovery aliases: every spelling a live crawler actually requests ────────
# Counted in /data/metrics.jsonl (2026-09-15), all previously 404.

@pytest.mark.parametrize("path", [
    "/.well-known/agents.json",
    "/agents.json",
    "/.well-known/agent-directory.json",
    "/agent-directory.json",
    "/.well-known/mcp",
    "/mcp.json",
])
def test_agent_requested_discovery_spellings_are_served(docs, path):
    """A 404 on a discovery path is how a paying agent gives up."""
    import json as _json
    code_path = path
    # Reuse the rendered MAINNET doc set: it already fetched the app, but we need
    # this specific path, so probe it directly against the same app instance.
    from fastapi.testclient import TestClient
    import api_server as a
    r = TestClient(a.app).get(code_path)
    assert r.status_code == 200, f"{path} must be served, got {r.status_code}"
    assert r.json(), f"{path} must return a non-empty document"


# ── D-1280: agents stuck at the MCP door ─────────────────────────────────────
# Measured 2026-09-16 in /data/metrics.jsonl: 79 distinct agents with 1,162
# "Invalid Content-Type header" 400s and ZERO successes, hammering only /mcp and
# /mcp/. One had 630 attempts across 3 days. The SDK's DNS-rebinding guard
# (transport_security.validate_request) rejects the POST before the JSON-RPC
# handler, so a client that omits Content-Type — or sends the HTTP client's
# default form encoding — is turned away with no hint why.

def test_oauth_discovery_paths_answer_instead_of_404ing():
    """150 OAuth discovery probes 404'd. A 404 reads as 'discovery broken'."""
    from fastapi.testclient import TestClient
    import api_server as a
    c = TestClient(a.app)
    for p in ["/.well-known/oauth-protected-resource",
              "/.well-known/oauth-protected-resource/mcp",
              "/.well-known/oauth-authorization-server",
              "/.well-known/oauth-authorization-server/mcp",
              "/mcp/.well-known/oauth-protected-resource",
              "/mcp/.well-known/oauth-authorization-server"]:
        r = c.get(p)
        assert r.status_code == 200, f"{p} -> {r.status_code}"
        # Must be an EMPTY doc: we have no auth server, and pointing a client at
        # a fake one would be worse than the 404.
        body = r.json()
        assert body == {}, f"{p} must declare no authorization server, got {body}"


# ── D-1293: the payable endpoint must be REGISTERABLE by a directory ──────────

def test_the_x402_endpoint_has_an_input_schema_and_a_402():
    """x402scan refused to register the ONLY endpoint that takes money:
    "validation: Missing input schema — add a requestBody or parameter schema
    to your OpenAPI spec so agents know what to send" (live, 2026-09-16:
    registerFromOrigin -> noValidResources, failed 1 / skipped 75).

    The route takes a raw Request, so FastAPI emitted no requestBody and the
    endpoint was treated as non-invocable. A directory cannot list an endpoint
    it does not know how to call.
    """
    doc = render(MAINNET)["openapi"]
    op = doc["paths"]["/v1/billing/x402"]["post"]
    assert "requestBody" in op, "no input schema — directory registration will fail"
    assert op["requestBody"]["content"]["application/json"]["schema"]["type"] == "object"
    assert "402" in op["responses"], "the 402 challenge is this endpoint's primary contract"


def test_x_payment_info_is_derived_from_the_configured_network():
    """Prices/network/asset must come from the same config the endpoint charges
    from. A second literal copy is a second thing that can go wrong silently —
    the exact bug class of `eip155:845` and the hardcoded testnet asset."""
    for net, usdc in ((MAINNET, USDC_MAINNET), (TESTNET, USDC_TESTNET)):
        doc = render(net)["openapi"]
        xpi = doc["paths"]["/v1/billing/x402"]["post"]["x-payment-info"]
        assert xpi["network"] == net, f"network did not follow config: {xpi['network']}"
        assert xpi["asset"] == usdc, "asset did not follow the network"
        assert xpi["payTo"] == PAYTO
        assert xpi["amount"] == "10000"        # $0.01 in USDC atomic units
        assert xpi["pricingMode"] == "fixed"
        assert xpi["protocols"][0]["protocol"] == "x402"
    assert "x-guidance" in render(MAINNET)["openapi"]["info"]


def test_both_well_known_spellings_serve_the_fan_out():
    """x402scan fetches `/.well-known/x402` first, then `.json`. Its docs say
    registerFromOrigin fails with `noDiscovery` when only one variant exists.
    One function serves both, so they cannot disagree."""
    docs = render(MAINNET)
    a, b = json.loads(docs["x402"]), json.loads(docs["x402json"])
    assert a["version"] == 1 and b["version"] == 1
    assert a["resources"] == b["resources"] == ["POST https://aiagentscity.com/v1/billing/x402"]
    # the rich challenge detail must survive the addition
    assert a["x402Version"] == 2
    assert a["payTo"] == PAYTO
