# tests/test_x402_guard_catches_novel_leak.py
"""D-1314 acceptance criterion 2 — prove the new guard catches a leak it was
never told about.

A guard that only re-tests the surfaces D-1312 already fixed, against the same
four phrases, has improved nothing. This file is therefore a PROBE, not a
regression test: it fabricates a leak the guard's author never saw, in wording
the old phrase list cannot match, and requires the guard to go RED.

Two things are asserted on the SAME served bytes:

  1. the guard that was replaced (`git a9fa689:tests/test_x402_discovery_network.py`)
     reports NO violation, and
  2. the guard in this repo reports a violation.

That pair is the whole argument. Either half alone proves nothing: (1) alone
shows the old guard was weak, (2) alone could be a guard that fires on
everything.

How the leak is injected
------------------------
Not as a synthetic string handed to the checker — that would prove the checker
matches a string, not that the surface is guarded. The leak is monkeypatched into
a REAL route's own template/render path, and then fetched over HTTP through
TestClient. So the bytes the guard sees are served bytes, produced by the real
substitution pipeline, and the probe cannot pass by bypassing it.

Nothing on disk is modified: the patch lives in the booted subprocess.

Run: /opt/miniconda3/bin/python3 tests/test_x402_guard_catches_novel_leak.py
     /opt/miniconda3/bin/python3 -m pytest tests/test_x402_guard_catches_novel_leak.py -q
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Inputs, not expected values (same rule as the guard itself).
PAY_TO_INPUT = "0x363c520492EDbA89057bCe696B74263B3295a72A"
FACILITATOR_INPUT = "https://x402.org/facilitator"

# ── the guard under test, imported from the real file ────────────────────────
# Loaded by path rather than by module name: there is no tests/__init__.py, and
# importing the guard by name would depend on pytest's sys.path insertion. The
# probe must exercise the SHIPPED guard, never a copy of its logic.
_GUARD_PATH = REPO / "tests" / "test_x402_discovery_network.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("_guard_under_test", _GUARD_PATH)
    assert spec is not None, f"could not load the guard from {_GUARD_PATH}"
    assert spec.loader is not None, f"no loader for {_GUARD_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load_guard()

# ── the guard that was replaced, reproduced faithfully (git a9fa689) ─────────
# These values ARE literals, deliberately and unavoidably: they are the old
# guard's own phrase list and asset constant. Reproducing what it searched for is
# the only way to show what it missed. They are never used as an expected value
# for a served byte — only as the search terms of the superseded implementation.
OLD_PHRASES = ("TESTNET ONLY", "84532", "Sepolia", "cannot complete it")
USDC_TESTNET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
OLD_MAINNET = "eip155:8453"


def old_guard_violations(net, surface, text):
    """What the replaced guard would have reported for this surface.

    Two structural weaknesses are reproduced exactly, because they are the reason
    a novel leak got through and the probe would be dishonest without them:

      * it only ever inspected docs[MAINNET] — in testnet mode it made NO network
        assertion at all, so a testnet surface could claim anything;
      * on mainnet it searched for four literals plus the testnet token, so any
        wording outside that list was invisible to it.
    """
    if net != OLD_MAINNET:
        return []  # it never looked at testnet mode
    out = []
    for phrase in OLD_PHRASES:
        if phrase in text:
            out.append(f"old-guard: {surface} carries {phrase!r}")
    if USDC_TESTNET in text:
        out.append(f"old-guard: {surface} advertises the testnet USDC token")
    return out


# ── the leaks: fresh prose, never in the old phrase list ─────────────────────
# LEAK_A names a THIRD network ("Base Goerli", eip155:84531 — a real, retired
# testnet). It defeats the old guard in the very mode the old guard checked:
# none of "TESTNET ONLY" / "84532" / "Sepolia" / "cannot complete it" appear, and
# 84531 does not contain the substring 84532. No amount of phrase-list care would
# have caught it, because the phrase for "wrong network" cannot be enumerated —
# only the PROPERTY "the network named is the configured one" can.
LEAK_A = ("  Settlement moved to Base Goerli (eip155:84531) — if a request is "
          "refused, retry against that chain.")

# LEAK_B claims mainnet while configured for testnet. The old guard made no
# assertion in testnet mode whatsoever, so this direction was entirely unguarded.
LEAK_B = ("  Payments settle in real USDC on Base MAINNET (eip155:8453). "
          "No testnet wallet is needed.")


CHILD = r'''
import json, sys
sys.path.insert(0, "@@REPO@@")
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute
import api_server as a
import x402_verify

# Inject the leak into REAL routes, in the subprocess only. Nothing on disk is
# touched, and both routes keep their own rendering pipeline:
#   /agent-ledger -> _status_html() reads status.html and runs the real
#                    .replace("{X402_SETTLEMENT_SPAN}", ...) substitution
#   /agents.txt   -> the real handler, wrapped
# so the probe cannot pass by handing the checker bytes the app never served.
# @@LEAK@@ is replaced by a json.dumps() STRING LITERAL — it must not be quoted
# in this template.
leak = @@LEAK@@
if leak:
    _tmpl = a._status_html_template
    a._status_html_template = lambda: _tmpl().replace(
        "</body>", leak + "\n</body>")

    # /agents.txt: replace the route with a NEW APIRoute carrying the wrapped
    # endpoint. Assigning `route.endpoint` alone is a no-op (APIRoute binds its
    # handler in __init__), and rebuilding `route.app` by hand skips FastAPI's
    # request-scope setup ("fastapi_inner_astack not found"). Constructing the
    # route through APIRoute.__init__ is what keeps the wrapped handler and the
    # framework's own scope handling together.
    _new_routes = []
    for _route in a.app.routes:
        if type(_route).__name__ == "APIRoute" and _route.path == "/agents.txt":
            _orig = _route.endpoint

            def _leaked(_orig=_orig, _leak=leak):
                return _orig().replace("\nCONTACT\n", "\n" + _leak + "\n\nCONTACT\n")

            _new_routes.append(APIRoute(
                _route.path, _leaked, methods=sorted(_route.methods or ["GET"]),
                response_class=_route.response_class, name=_route.name,
                include_in_schema=_route.include_in_schema))
        else:
            _new_routes.append(_route)
    a.app.routes[:] = _new_routes

with TestClient(a.app) as c:
    surfaces = {
        "/agent-ledger": c.get("/agent-ledger").text,
        "/agents.txt": c.get("/agents.txt").text,
        "/llms.txt": c.get("/llms.txt").text,
    }
    facts = {
        "network": a._x402_network(),
        "label": a._x402_network_label(),
        "asset": a._x402_asset(),
        "pay_to": a._x402_pay_to() or "",
        "amount_atomic": a._x402_amount_atomic(),
        "till_amount_atomic": str(int(round(
            float(str(x402_verify.X402_MINT_PRICE).lstrip("$")) * 1_000_000))),
        "known_ids": sorted(set(a.X402_USDC_BY_NETWORK) | set(a.X402_MAINNET_NETWORKS)
                            | {a._x402_network()}),
        "asset_table": dict(a.X402_USDC_BY_NETWORK),
        "namespaces": sorted({i.split(":")[0] for i in
                              (set(a.X402_USDC_BY_NETWORK)
                               | set(a.X402_MAINNET_NETWORKS))}),
    }
print("===JSON===")
print(json.dumps({"facts": facts, "surfaces": surfaces}))
'''


def boot(network, leak=""):
    """Boot the app with an optional leak injected into real routes."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("X402_", "CDP_"))}
    env.update({"X402_PAY_TO": PAY_TO_INPUT, "X402_NETWORK": network,
                "X402_FACILITATOR_URL": FACILITATOR_INPUT,
                "AGENT_LEDGER_DATA": "/tmp/al_leak_probe"})
    code = (CHILD.replace("@@REPO@@", str(REPO))
            .replace("@@LEAK@@", json.dumps(leak)))  # JSON string literal: safe
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, cwd=str(REPO))
    if r.returncode != 0:
        raise RuntimeError("probe app failed to boot:\n" + r.stderr[-2000:])
    lines = r.stdout.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "===JSON===" and i + 1 < len(lines):
            return json.loads(lines[i + 1])
    raise RuntimeError("no ===JSON=== payload from probe app:\n" + r.stdout[-800:])


def new_guard_violations(net, surface, text, facts):
    """What the guard in this repo reports for this surface, in this mode.

    Calls the guard's OWN `scan_surface`, so the probe and the tests can never
    disagree: if the shared checker were weakened, the probe would go GREEN and
    this file would fail.
    """
    ids = {i for i in facts["known_ids"] if i != net}
    forbidden = {
        "ids": ids,
        "bare_refs": {i.split(":")[-1] for i in ids},
        "assets": {v for k, v in facts["asset_table"].items() if k != net},
        # the other mode's label, from a real boot of production code
        "labels": _other_labels(net, facts),
    }
    return guard.scan_surface(surface, text, facts, forbidden)


_OTHER_LABEL_CACHE = {}


def _other_labels(net, facts):
    """The other mode's label, obtained from production code, not written here."""
    if net not in _OTHER_LABEL_CACHE:
        other = [i for i in facts["known_ids"] if i != net]
        got = set()
        for o in other:
            d = boot(o)  # a real boot: the label is production's own output
            got.add(d["facts"]["label"])
        _OTHER_LABEL_CACHE[net] = got
    return _OTHER_LABEL_CACHE[net]


def run_probe():
    """Print the full RED/GREEN transcript and return (ok, transcript)."""
    lines = []
    ok = True

    def say(s=""):
        lines.append(s)

    net_main = guard.mainnet_network()
    net_test = guard.other_network(net_main)

    say("=" * 78)
    say("D-1314 SYNTHETIC-LEAK PROBE")
    say("Configured networks (from config, not written here): "
        f"{list(guard.NETWORKS)}")
    say("=" * 78)

    cases = [
        ("A  wrong THIRD network named, in mainnet mode", net_main, LEAK_A,
         "/agent-ledger"),
        ("A2 same leak on /agents.txt (the surface that hid the last one)",
         net_main, LEAK_A, "/agents.txt"),
        ("B  claims mainnet while configured for testnet", net_test, LEAK_B,
         "/agent-ledger"),
    ]

    # ── 0. clean baseline: the guard must be GREEN on the shipped bytes ──────
    for net in (net_main, net_test):
        d = boot(net, leak="")
        v = [m for s, m in
             ((s, msg)
              for s, t in d["surfaces"].items()
              for msg in new_guard_violations(net, s, t, d["facts"]))]
        say(f"[baseline {net}] new guard violations: {len(v)}")
        for m in v:
            say("    " + m)
        ok &= not v

    # ── 1..n: inject each leak, require old GREEN and new RED ───────────────
    for title, net, leak, surface in cases:
        d = boot(net, leak=leak)
        text = d["surfaces"][surface]
        say()
        say("-" * 78)
        say(f"CASE {title}")
        say(f"  mode={net}  surface={surface}")
        say(f"  injected: {leak.strip()}")

        # the injection must have reached the SERVED bytes, or the case is
        # vacuous and a GREEN guard would mean nothing.
        reached = leak.strip() in text
        say(f"  reached served bytes: {reached}")
        if not reached:
            ok = False
            say("  !! PROBE INVALID: the leak never reached the surface")
            continue

        old = old_guard_violations(net, surface, text)
        new = new_guard_violations(net, surface, text, d["facts"])
        say(f"  OLD guard (phrase list) : {'RED' if old else 'GREEN'} "
            f"({len(old)} violation(s))")
        for m in old:
            say("      " + m)
        say(f"  NEW guard (invariants)  : {'RED' if new else 'GREEN'} "
            f"({len(new)} violation(s))")
        for m in new:
            say("      " + m)

        caught = (not old) and bool(new)
        say(f"  => {'PASS' if caught else 'FAIL'}: the rewrite is what caught it"
            if caught else
            f"  => FAIL: old={'RED' if old else 'GREEN'} new={'RED' if new else 'GREEN'}")
        ok &= caught

        # ── removal: the same surface, leak gone, must go GREEN again ───────
        d2 = boot(net, leak="")
        v2 = new_guard_violations(net, surface, d2["surfaces"][surface], d2["facts"])
        say(f"  after removal, {surface} violations: {len(v2)} "
            f"({'GREEN' if not v2 else 'STILL RED'})")
        for m in v2:
            say("      " + m)
        ok &= not v2

    say()
    say("=" * 78)
    say(f"PROBE RESULT: {'ALL CASES PASS' if ok else 'FAILED'}")
    say("=" * 78)
    return ok, "\n".join(lines)


# ── as a test: the probe must pass, and the halves must both hold ────────────

def test_the_new_guard_catches_a_novel_leak_the_old_guard_missed():
    """AC2. Asserts both halves on identical served bytes."""
    ok, transcript = run_probe()
    assert ok, "synthetic-leak probe failed:\n" + transcript


def test_the_old_guard_is_genuinely_blind_to_the_novel_leak():
    """Guard-on-the-probe: without this, the probe could be claiming the old
    guard missed something it would actually have caught."""
    net_main = guard.mainnet_network()
    d = boot(net_main, leak=LEAK_A)
    for surface in ("/agent-ledger", "/agents.txt"):
        text = d["surfaces"][surface]
        assert LEAK_A.strip() in text, "injection did not reach the surface"
        assert old_guard_violations(net_main, surface, text) == [], (
            f"the probe is invalid: the OLD guard would have caught this leak "
            f"on {surface}")
        assert new_guard_violations(net_main, surface, text, d["facts"]), (
            f"the NEW guard missed the leak on {surface}")


def test_the_old_guard_made_no_assertion_at_all_in_testnet_mode():
    """The second structural blindness, asserted directly.

    This is not a typo in the old guard — it indexed docs[MAINNET] under every
    parametrization, so a testnet deployment had no wrong-network check. A
    testnet surface naming mainnet passed it, which is why this direction is now
    covered rather than assumed.
    """
    net_test = guard.other_network(guard.mainnet_network())
    d = boot(net_test, leak=LEAK_B)
    text = d["surfaces"]["/agent-ledger"]
    assert LEAK_B.strip() in text, "injection did not reach the surface"
    assert old_guard_violations(net_test, "/agent-ledger", text) == [], \
        "old guard was expected to make no testnet-mode assertion"
    assert new_guard_violations(net_test, "/agent-ledger", text, d["facts"]), \
        "the new guard must catch a testnet surface claiming mainnet"


def test_the_probe_does_not_fire_on_clean_bytes():
    """Negative control: the RED above is caused by the injected leak, not by a
    guard that fires on everything."""
    for net in guard.NETWORKS:
        d = boot(net, leak="")
        for surface, text in d["surfaces"].items():
            assert not new_guard_violations(net, surface, text, d["facts"]), (
                f"guard fired on clean bytes at {surface} under {net}")


def test_the_injected_surface_still_carries_its_own_real_content():
    """The injection wraps the real pipeline rather than replacing it: the
    substituted settlement sentence must still be present next to the leak."""
    net = guard.mainnet_network()
    d = boot(net, leak=LEAK_A)
    text = d["surfaces"]["/agent-ledger"]
    assert d["facts"]["network"] in text, (
        "the leak patch replaced the real settlement sentence instead of adding "
        "to it — the probe would then not be testing a decorated real surface")
    assert "{X402_SETTLEMENT" not in text, "the real substitution stopped running"


if __name__ == "__main__":
    _ok, _transcript = run_probe()
    print(_transcript)
    sys.exit(0 if _ok else 1)
