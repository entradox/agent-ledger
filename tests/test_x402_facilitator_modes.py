#!/usr/bin/env python3
"""Prove the CDP wiring: all four facilitator modes resolve correctly.

Run: /opt/miniconda3/bin/python3 tests/test_x402_facilitator_modes.py
"""
import importlib
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILS = []


def run(env_overrides, label):
    """Import x402_verify in a subprocess with the given env, report mode."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("X402_", "CDP_"))}
    env.update(env_overrides)
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import x402_verify as x\n"
        "print('MODE', x.FACILITATOR_MODE)\n"
        "print('URL', x.FACILITATOR_URL_RESOLVED)\n"
        "print('ENABLED', x.X402_ENABLED)\n"
        "print('REASON', x.X402_DISABLED_REASON)\n"
    ) % str(REPO)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, cwd=str(REPO))
    out = {}
    for line in r.stdout.splitlines():
        if " " in line:
            k, v = line.split(" ", 1)
            out[k] = v
    if r.returncode != 0 and not out:
        out = {"MODE": "CRASH", "REASON": r.stderr.strip()[-200:]}
    print(f"\n--- {label} ---")
    for k in ("MODE", "URL", "ENABLED", "REASON"):
        print(f"  {k:8s}: {out.get(k)}")
    return out


print("=" * 70)
print("x402 facilitator mode resolution")
print("=" * 70)

PAYTO = "0x363c520492EDbA89057bCe696B74263B3295a72A"

# 1. Testnet, no CDP creds -> public facilitator, must stay ENABLED.
a = run({"X402_PAY_TO": PAYTO}, "1. testnet + no CDP creds (today's prod config)")
if a.get("MODE") != "public-testnet":
    FAILS.append(f"expected public-testnet, got {a.get('MODE')}")
if "x402.org" not in a.get("URL", ""):
    FAILS.append(f"expected x402.org url, got {a.get('URL')}")
if a.get("ENABLED") != "True":
    FAILS.append(f"testnet must still work, ENABLED={a.get('ENABLED')} "
                 f"({a.get('REASON')})")

# 2. Mainnet WITHOUT CDP creds -> must REFUSE, not advertise.
b = run({"X402_PAY_TO": PAYTO, "X402_NETWORK": "eip155:8453"},
        "2. mainnet + no CDP creds (must refuse)")
if b.get("ENABLED") == "True":
    FAILS.append("mainnet without CDP creds was ENABLED — would advertise a "
                 "payment that can never settle")
if "CDP" not in (b.get("REASON") or ""):
    FAILS.append(f"refusal reason not explanatory: {b.get('REASON')}")

# 3. Mainnet WITH CDP creds -> CDP mode. Fails auth (fake creds) but must
#    select the CDP facilitator, which is the thing under test.
c = run({"X402_PAY_TO": PAYTO, "X402_NETWORK": "eip155:8453",
         "CDP_API_KEY_ID": "fake-id", "CDP_API_KEY_SECRET": "fake-secret"},
        "3. mainnet + CDP creds (selects CDP)")
if c.get("MODE") != "cdp":
    FAILS.append(f"expected cdp mode, got {c.get('MODE')}")
if "cdp.coinbase.com" not in c.get("URL", ""):
    FAILS.append(f"expected CDP url, got {c.get('URL')}")
if "could never settle" in (c.get("REASON") or ""):
    FAILS.append("CDP creds present but the mainnet guard fired anyway")

# 4. Explicit URL wins over CDP creds.
d = run({"X402_PAY_TO": PAYTO, "X402_FACILITATOR_URL": "https://example.com/f",
         "CDP_API_KEY_ID": "fake-id", "CDP_API_KEY_SECRET": "fake-secret"},
        "4. explicit URL beats CDP creds")
if d.get("MODE") != "explicit-url":
    FAILS.append(f"explicit URL must win, got {d.get('MODE')}")
if d.get("URL") != "https://example.com/f":
    FAILS.append(f"expected the explicit url, got {d.get('URL')}")

print()
print("=" * 70)
if FAILS:
    print(f"FAILURES ({len(FAILS)}):")
    for f in FAILS:
        print(f"  ✗ {f}")
    sys.exit(1)
print("ALL MODES CORRECT ✓")
print("  testnet still works · mainnet refuses without CDP · CDP selected when")
print("  creds exist · explicit URL overrides")
