#!/usr/bin/env python3
"""The published discovery text must match the configured network.

Bugs this locks down, all found before flipping to mainnet:
  1. "mainnet" was computed as `network == "eip155:845"`. Base mainnet is
     eip155:8453, so the flag read FALSE on mainnet — the published document
     contradicted the live network.
  2. The asset address was a hardcoded constant pointing at TESTNET USDC. On
     mainnet that tells every agent to pay with a token that is not the one the
     endpoint settles in.
  3. ~18 literal "TESTNET ONLY ... a mainnet wallet cannot complete it"
     statements. True on Sepolia, actively harmful on mainnet: they steer agents
     away from a working payment path.

Checked in BOTH modes, against the real HTTP responses — a fix that only works
in the mode it was written for is exactly how #1 happened.

Run: /opt/miniconda3/bin/python3 tests/test_x402_discovery_network.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PAYTO = "0x363c520492EDbA89057bCe696B74263B3295a72A"
TESTNET, MAINNET = "eip155:84532", "eip155:8453"
USDC_TESTNET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
USDC_MAINNET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
FAILS = []


def render(network):
    """Bring up the app in a subprocess and fetch the discovery documents."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("X402_", "CDP_"))}
    env.update({"X402_PAY_TO": PAYTO, "X402_NETWORK": network,
                "AGENT_LEDGER_DATA": "/tmp/al_disc_test"})
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


for label, net, asset, is_main in (
        ("TESTNET", TESTNET, USDC_TESTNET, False),
        ("MAINNET", MAINNET, USDC_MAINNET, True)):
    print("=" * 72)
    print(f"{label}  (X402_NETWORK={net})")
    print("=" * 72)
    d = render(net)
    if "error" in d:
        FAILS.append(f"{label}: app failed to serve: {d['error'][:200]}")
        print("  FAILED:", d["error"][:400])
        continue

    aj = json.loads(d["agentjson"])
    x4 = json.loads(d["x402"])
    llms = d["llms"]
    agents = d["agents"]
    start = d["start"]

    checks = [
        (x4.get("network") == net, f"/.well-known/x402 network == {net}"),
        (x4.get("mainnet") is is_main,
         f"/.well-known/x402 mainnet is {is_main} (got {x4.get('mainnet')!r})"),
        (x4.get("asset") == asset,
         f"/.well-known/x402 asset == {asset} (got {x4.get('asset')})"),
        # The agent-card carries no payment block; the separate MCP descriptor
        # (reached at /.well-known/mcp.json) is where an MCP client looks for it.
        # Asserting on agent-card here was my own wrong assumption about the
        # document, not a product defect.
        (json.loads(d["mcp"]).get("payment", {}).get("mainnet") is is_main,
         f"mcp descriptor mainnet is {is_main}"),
        (asset in d["x402"], "x402 doc carries the network's USDC address"),
    ]

    if is_main:
        checks += [
            ("testnet only" not in llms.lower()
             and "TESTNET ONLY" not in llms,
             "llms.txt must not claim testnet-only on mainnet"),
            ("cannot complete" not in llms,
             "llms.txt must not say a mainnet wallet cannot pay"),
            ("cannot complete" not in start,
             "/start must not say a mainnet wallet cannot pay"),
            ("MAINNET" in llms.upper(), "llms.txt states it is mainnet"),
            (USDC_TESTNET not in d["x402"],
             "x402 doc must not advertise the TESTNET token address"),
        ]
    else:
        checks += [
            ("TESTNET" in llms.upper(), "llms.txt states it is testnet"),
            (USDC_MAINNET not in d["x402"],
             "x402 doc must not advertise the MAINNET token address"),
        ]

    for ok, msg in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {msg}")
        if not ok:
            FAILS.append(f"{label}: {msg}")

print()
print("=" * 72)
if FAILS:
    print(f"FAILURES ({len(FAILS)}):")
    for f in FAILS:
        print("  ✗", f)
    sys.exit(1)
print("DISCOVERY MATCHES THE CONFIGURED NETWORK IN BOTH MODES ✓")
