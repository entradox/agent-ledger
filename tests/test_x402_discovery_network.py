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
    code = (
        "import sys, json; sys.path.insert(0, %r)\n"
        "from fastapi.testclient import TestClient\n"
        "import api_server as a\n"
        "c = TestClient(a.app)\n"
        "out = {\n"
        "  'agentjson': c.get('/.well-known/agent-card.json').text,\n"
        "  'x402': c.get('/.well-known/x402').text,\n"
        "  'llms': c.get('/llms.txt').text,\n"
        "  'agents': c.get('/agents.txt').text,\n"
        "  'mcp': c.get('/.well-known/mcp.json').text,\n"
        "  'start': c.get('/start').text,\n"
        "}\n"
        "print('===JSON==='); print(json.dumps(out))\n"
    ) % str(REPO)
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

@pytest.mark.parametrize("surface", ["llms", "start", "agents", "agentjson", "x402", "mcp"])
def test_mainnet_never_claims_testnet_only(docs, surface):
    d = docs[MAINNET]
    text = d[surface]
    assert "TESTNET ONLY" not in text.upper(), \
        f"{surface} claims TESTNET ONLY while running on mainnet"
    assert "cannot complete it" not in text.lower(), \
        f"{surface} tells a mainnet wallet it cannot pay"
    assert USDC_TESTNET not in text, \
        f"{surface} advertises the testnet USDC token on mainnet"


def test_mainnet_start_page_advertises_a_working_payment_path(docs):
    """`/start` is the page a prospective payer reads.

    Regression: it carried BOTH the derived mainnet sentence AND a stale literal
    'Mainnet arrives when Coinbase CDP onboarding completes', contradicting itself.
    """
    start = docs[MAINNET]["start"]
    assert "MAINNET" in start.upper(), "/start must state it settles on mainnet"
    assert "arrives when" not in start.lower(), \
        "/start must not claim mainnet is still pending while it is live"


def test_testnet_mode_still_discloses_testnet(docs):
    """The mirror assertion: testnet must not masquerade as mainnet."""
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
