# tests/test_x402_discovery_network.py
"""Discovery documents must match the CONFIGURED network — asserted as PROPERTIES.

The defect this file exists for
-------------------------------
Discovery prose hardcoded "TESTNET ONLY ... a mainnet wallet cannot complete it".
That became false the moment X402_NETWORK flipped, and it tells paying agents to
stay away from a working payment path. It has now shipped four times in this
project: `eip155:845`, the testnet asset, the stale mainnet copy, and the
unsubstituted literals.

Why the previous guard could not stop the fifth
----------------------------------------------
It was a LITERAL PHRASE LIST:

    TESTNET_ONLY_PHRASES = ("TESTNET ONLY", "84532", "Sepolia",
                            "cannot complete it")

and it passed `/agents.txt` while that surface read "It is not real money yet.
Mainnet is pending onboarding." — none of those four strings. A phrase list tests
the phrases someone thought of, not the property that matters. Worse, it ran its
network check in ONE direction (mainnet surfaces must not claim testnet), so the
testnet deployment had no wrong-network check at all.

What replaces it
----------------
Two PROPERTIES, both wording-independent, both derived from live config. There is
no phrase list in this file and no expected value written as a literal.

  INV-1  CONFIGURED-NETWORK CONSISTENCY
         Every network identifier on every served surface equals the configured
         one. Wording is irrelevant: if a surface says 84532 while config says
         8453, it fails regardless of the sentence around it.

  INV-2  NO FINGERPRINT OF ANOTHER NETWORK IN SERVED OUTPUT
         The served bytes carry no identifier of any OTHER known network — the
         other network's CAIP-2 id, its bare chain id, its USDC contract, or its
         network label. Every fingerprint is read from config, and the "other
         network" fingerprints are taken from an ACTUAL boot in the other mode,
         so nothing is transcribed by hand. Mode-independent: it must hold in
         testnet rehearsal AND production.

  INV-3  PAYMENT TERMS IMPLY NETWORK DISCLOSURE  (the anti-deletion floor)
         A surface that states payment parameters (the configured USDC contract
         or payTo) must ALSO name the configured network. This is what stops
         "fixing" a leak by deleting the sentence: the sentence can only go if
         the payment terms go with it.

  INV-4  DECLARED PAYMENT VALUES EQUAL THE DERIVED ONES
         Every 40-hex address in served bytes is the configured asset or the
         configured payTo; every structured `"amount"` equals the atomic amount
         the till actually charges; every structured `"network"` equals config.

  INV-5  NO UNSUBSTITUTED TEMPLATE TOKEN
         The D-1270 class — a page rendering its own placeholder. Matched by
         pattern, not by a list of the token names someone remembered.

Coverage is a SWEEP, not a list
-------------------------------
Every static GET route on the app is fetched (enumerated from the route table at
runtime, so a NEW route is swept automatically), plus the two MCP tool surfaces,
plus the served 402 challenge. D-1312 was missed because a surface was not in a
hand-written list; the sweep makes "not in the list" impossible to hit by
accident. Routes that must be skipped (binary assets) are recorded and asserted,
so a route cannot leave the sweep silently.

What this guard deliberately does NOT do
----------------------------------------
It does not judge PROSE. A sentence that names no identifier ("payments settle on
a testnet for now") cannot be falsified by grep, and any attempt to do it is a
phrase list again. The guard pins identifiers and structured fields, which is
where every one of the four shipped defects actually lived. The network LABEL is
the one prose-adjacent case that is checkable, and INV-2 covers it because the
label is derived from production code rather than written here.

Two surfaces are pinned as REQUIRED to disclose, because INV-3 (which keys off
payment parameters) does not reach prose pages that state no parameters:
/agents.txt carried the historical leak while naming no address at all.

Run: /opt/miniconda3/bin/python3 -m pytest tests/test_x402_discovery_network.py -q
"""
import functools
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# Helpers referenced by both the child and the tests below.
def decode_payment_request(header_value):
    """Parse a WWW-Authenticate: Payment header and return its request dict."""
    import base64
    params = {}
    for m in re.finditer(
            r'([a-zA-Z_][\w-]*)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))',
            header_value[len("Payment "):]):
        params[m.group(1)] = m.group(2) or m.group(3)
    req_b64 = params.get("request", "")
    padded = req_b64 + "=" * (-len(req_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(padded).decode())


# ── APP CONFIG INPUT (never an expected value) ───────────────────────────────
# These two are handed to the app as configuration. They are INPUTS. No assertion
# in this file compares a served byte against them: every assertion compares the
# served bytes against `facts`, which the running app derives from this very
# config. Writing an expected network/asset/amount/address as a literal here is
# the defect this file removes — do not reintroduce one as a "fix".
PAY_TO_INPUT = "0x363c520492EDbA89057bCe696B74263B3295a72A"
FACILITATOR_INPUT = "https://x402.org/facilitator"

# The one surface that can never be swept: a binary asset has no text to check.
NON_TEXT_ROUTE = "/img/dashboard.png"

# ── fingerprint patterns ─────────────────────────────────────────────────────
# `eip155:`-scoped rather than a general `namespace:reference` pattern: a general
# one matches CSS (`margin:10px`, `width:64ch`) and produced ~20 false hits per
# HTML page when measured across this app's 53 routes. The namespace set is built
# from config, so a new namespace is picked up without editing this file.
_ADDR = re.compile(r"0x[0-9a-fA-F]{40}")
_AMOUNT_DECL = re.compile(r'"amount"\s*:\s*"?(?P<v>\d+)"?')
_NETWORK_DECL = re.compile(r'"network"\s*:\s*"(?P<v>[^"]+)"')
_TOKEN = re.compile(r"\{[A-Z][A-Z0-9_]*\}")


def _namespaced_pattern(namespaces):
    alt = "|".join(re.escape(n) for n in sorted(namespaces, key=len, reverse=True))
    return re.compile(rf"(?<![\w:])(?P<ns>{alt}):(?P<ref>[A-Za-z0-9]{{2,}})(?![\w])")


def network_ids(text, namespaces):
    """Every CAIP-2-style network identifier in `text`, as 'ns:ref' strings."""
    return [f"{m.group('ns')}:{m.group('ref')}"
            for m in _namespaced_pattern(namespaces).finditer(text)]


def addresses(text):
    return {a.lower() for a in _ADDR.findall(text)}


def _boundary_hits(text, needle):
    """Occurrences of `needle` not embedded in a longer word or number."""
    if not needle:
        return []
    return re.findall(r"(?<![\w:.])" + re.escape(needle) + r"(?![\w])", text)


# ── the child that boots the app and fetches every surface ───────────────────
# Real Python, not a string built from escaped newlines. Subprocess on purpose:
# the network is read at import time, so both modes cannot be exercised in one
# in-process run.
CHILD = r'''
import base64, json, sys
sys.path.insert(0, "@@REPO@@")
from fastapi.testclient import TestClient
import api_server as a
import x402_verify
import mpp_verify

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


# The sweep is enumerated from the app's OWN route table, so a new route is
# covered the day it is added and a dropped one cannot leave silently.
routes = []
for _route in a.app.routes:
    _path = getattr(_route, "path", None)
    _methods = getattr(_route, "methods", None) or set()
    if _path and "{" not in _path and "GET" in _methods:
        routes.append(_path)
routes = sorted(set(routes))

surfaces, skipped, statuses = {}, {}, {}
# As a context manager so the FastMCP lifespan runs. Without it the MCP session
# manager is uninitialized and every tools/call raises -- a harness failure that
# would otherwise look like a product failure.
with TestClient(a.app) as c:
    for p in routes:
        try:
            r = c.get(p)
        except Exception as e:  # noqa: BLE001 - report, never crash the sweep
            skipped[p] = "raised: " + repr(e)
            continue
        ct = r.headers.get("content-type", "")
        statuses[p] = r.status_code
        if not any(t in ct for t in ("text", "json", "xml")):
            skipped[p] = "non-text content-type: " + ct
            continue
        surfaces[p] = r.text

    for name, args in (("mcp::ledger_api_docs(rest)", {"topic": "rest"}),
                       ("mcp::ledger_api_docs()", {})):
        surfaces[name] = mcp_call(c, "ledger_api_docs", args)
        statuses[name] = 200

    # The till itself. Its 402 envelope carries the network, asset, amount and
    # payTo an agent is actually asked to pay -- so it is a surface, and it is
    # swept like one whenever the challenge is served.
    r = c.post("/v1/billing/x402", json={"agent_id": "guard-probe"})
    hdr = r.headers.get("payment-required") or ""
    challenge = {"status": r.status_code, "body": r.text, "header": bool(hdr),
                 "decoded": None}
    if hdr:
        try:
            challenge["decoded"] = base64.urlsafe_b64decode(
                hdr + "=" * (-len(hdr) % 4)).decode()
        except Exception as e:  # noqa: BLE001
            challenge["decoded"] = None
            challenge["decode_error"] = repr(e)
    if challenge["decoded"]:
        surfaces["POST::/v1/billing/x402"] = challenge["decoded"]
        statuses["POST::/v1/billing/x402"] = r.status_code

    # MPP challenge from the same response (Path A). Emitted even when x402 is
    # disabled by missing CDP creds, as long as MPP is configured, so the guard
    # can verify the fail-closed shape.
    www_hdr = r.headers.get("www-authenticate", "")
    if www_hdr.lower().startswith("payment "):
        surfaces["POST::/v1/billing/x402/mpp"] = www_hdr
        statuses["POST::/v1/billing/x402/mpp"] = r.status_code
        try:
            surfaces["POST::/v1/billing/x402/mpp_decoded"] = json.dumps(
                decode_payment_request(www_hdr))
        except Exception as e:  # noqa: BLE001
            surfaces["POST::/v1/billing/x402/mpp_decoded"] = "decode error: " + repr(e)

    known_ids = sorted(set(a.X402_USDC_BY_NETWORK)
                       | set(a.X402_MAINNET_NETWORKS)
                       | {a._x402_network()})

    facts = {
        "network": a._x402_network(),
        "is_mainnet": a._x402_is_mainnet(),
        "label": a._x402_network_label(),
        "asset": a._x402_asset(),
        "pay_to": a._x402_pay_to() or "",
        # what every discovery document and the MPP offer derive from
        "amount_atomic": a._x402_amount_atomic(),
        # what the TILL actually charges (x402_verify is the single source of
        # truth; discovery docs now derive from it too).
        "till_amount_atomic": str(x402_verify.x402_mint_price_atomic()),
        "till_price": x402_verify.X402_MINT_PRICE,
        "price_usd": float(str(x402_verify.X402_MINT_PRICE).lstrip("$")),
        "mpp_enabled": bool(getattr(mpp_verify, "MPP_ENABLED", False)) if 'mpp_verify' in sys.modules else False,
        "asset_table": dict(a.X402_USDC_BY_NETWORK),
        "known_ids": known_ids,
        "namespaces": sorted({i.split(":")[0] for i in known_ids}),
        "enabled": bool(getattr(x402_verify, "X402_ENABLED", False)),
        "mode": x402_verify.FACILITATOR_MODE,
        "disabled_reason": getattr(x402_verify, "X402_DISABLED_REASON", ""),
        "settlement": a.X402_SETTLEMENT,
    }
    print("===JSON===")
    print(json.dumps({"facts": facts, "surfaces": surfaces, "skipped": skipped,
                      "statuses": statuses, "routes": routes,
                      "challenge": challenge}))
'''


def render(network, facilitator=FACILITATOR_INPUT, extra_env=None):
    """Boot the app in a subprocess and fetch every served surface.

    `facilitator` is set by default so x402 is ENABLED in both modes without CDP
    credentials: a mainnet network with no credentials is refused by design
    (`x402_verify._init` raises rather than advertising a network no configured
    facilitator can settle). That refusal is asserted separately, via
    `facilitator=None`.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("X402_", "CDP_", "MPP_"))}
    env.update({"X402_PAY_TO": PAY_TO_INPUT, "X402_NETWORK": network,
                "AGENT_LEDGER_DATA": "/tmp/al_disc_test",
                "MPP_SECRET_KEY": "test-secret-for-discovery-guard"})
    if facilitator:
        env["X402_FACILITATOR_URL"] = facilitator
    if extra_env:
        env.update(extra_env)
    code = CHILD.replace("@@REPO@@", str(REPO))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, cwd=str(REPO))
    if r.returncode != 0:
        return {"error": r.stderr.strip()[-800:]}
    # A sentinel, not `line.startswith("{")`: any log line an import prints could
    # begin with a brace, and picking the wrong one would look like a product
    # failure. The child prints the payload on the line after the marker.
    lines = r.stdout.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "===JSON===" and i + 1 < len(lines):
            try:
                return json.loads(lines[i + 1])
            except ValueError as exc:
                # Not swallowed: a child that printed a marker but no parsable
                # payload is a harness failure, and it must say so rather than
                # fall through to a generic message that hides the cause.
                return {"error": f"child printed ===JSON=== but the payload did "
                                 f"not parse: {exc}: {lines[i + 1][:300]}"}
    return {"error": "no ===JSON=== payload in child output: " + r.stdout[-400:]}


@functools.lru_cache(maxsize=1)
def configured_networks():
    """The networks config knows about — read from config, not written here.

    Deliberately NOT an in-process `import api_server`: this module is collected
    before other modules have pinned AGENT_LEDGER_DATA, and importing the app at
    collection time would freeze its data dir on the way in.
    """
    code = ("import json,sys; sys.path.insert(0, %r); import api_server as a; "
            "print(json.dumps(sorted(a.X402_USDC_BY_NETWORK)))" % str(REPO))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(REPO))
    if r.returncode != 0:
        raise RuntimeError("could not read configured networks: " + r.stderr[-500:])
    nets = json.loads(r.stdout.strip().splitlines()[-1])
    if len(nets) < 2:
        raise RuntimeError(f"need 2+ configured networks to compare, got {nets}")
    return tuple(nets)


NETWORKS = configured_networks()


@functools.lru_cache(maxsize=1)
def mainnet_networks():
    """The networks CONFIG marks as mainnet — read from config, not written here.

    `X402_MAINNET_NETWORKS` is the app's own definition of "this settles real
    money". Asking config for it keeps the harness free of a chain-id literal,
    which is the discipline this card exists to enforce.
    """
    code = ("import json,sys; sys.path.insert(0, %r); import api_server as a; "
            "print(json.dumps(sorted(a.X402_MAINNET_NETWORKS)))" % str(REPO))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(REPO))
    if r.returncode != 0:
        raise RuntimeError("could not read mainnet networks: " + r.stderr[-500:])
    nets = json.loads(r.stdout.strip().splitlines()[-1])
    assert nets, "config declares no mainnet networks"
    return tuple(nets)


def mainnet_network():
    """The configured mainnet network that the sweep also covers."""
    for net in mainnet_networks():
        if net in NETWORKS:
            return net
    raise AssertionError(
        f"no configured mainnet network is in the swept set {NETWORKS}")


def other_network(net):
    """A configured network that is not `net` — for the mode-independence checks."""
    for candidate in NETWORKS:
        if candidate != net:
            return candidate
    raise AssertionError(f"only one configured network ({net}); cannot compare")


@pytest.fixture(scope="module")
def docs():
    """Both configured network modes, rendered once."""
    out = {net: render(net) for net in NETWORKS}
    for net, d in out.items():
        if "error" in d:
            pytest.fail(f"app failed to serve under {net}: {d['error'][:500]}")
    return out


def forbidden_for(docs_, net):
    """Fingerprints of every OTHER known network — derived, never transcribed.

    The ids, bare chain ids and USDC contracts come from config. The network
    LABELS come from an actual boot in the other mode, so the label text is
    production's own output rather than a copy of it written here.
    """
    me = docs_[net]["facts"]
    ids = {i for i in me["known_ids"] if i != net}
    return {
        "ids": ids,
        "bare_refs": {i.split(":")[-1] for i in ids},
        "assets": {v for k, v in me["asset_table"].items() if k != net},
        "labels": {d["facts"]["label"] for n, d in docs_.items() if n != net},
    }


def scan_surface(surface, text, facts, forbidden):
    """Every invariant violation on one served surface. Empty list == clean.

    One function implements the invariants, so the tests and the negative-control
    probe cannot drift apart: a leak the tests would miss is a leak the probe
    would miss too.
    """
    bad = []
    net = facts["network"]

    # INV-1 — every network id on the surface is the configured one.
    for found in network_ids(text, facts["namespaces"]):
        if found != net:
            bad.append(f"INV-1 {surface}: names network {found!r} while config "
                       f"is {net!r}")

    # INV-2 — no fingerprint of any other known network, in any spelling.
    for other in sorted(forbidden["ids"]):
        if _namespaced_pattern(facts["namespaces"]).search(text) and other in \
                network_ids(text, facts["namespaces"]):
            bad.append(f"INV-2 {surface}: carries the other network's id {other!r}")
    for ref in sorted(forbidden["bare_refs"]):
        if _boundary_hits(text, ref):
            bad.append(f"INV-2 {surface}: carries the bare chain id {ref!r} "
                       f"while config is {net!r}")
    for asset in sorted(forbidden["assets"]):
        if asset.lower() in text.lower():
            bad.append(f"INV-2 {surface}: carries the other network's USDC "
                       f"contract {asset}")
    for label in sorted(forbidden["labels"]):
        if label and label.lower() in text.lower():
            bad.append(f"INV-2 {surface}: carries the other network's label "
                       f"{label!r}")

    # INV-3 — payment parameters imply the configured network is named.
    terms = [t for t in (facts["asset"], facts["pay_to"])
             if t and t.lower() in text.lower()]
    if terms and net not in network_ids(text, facts["namespaces"]):
        bad.append(f"INV-3 {surface}: states payment parameters {terms} but "
                   f"never names the configured network {net!r} — deleting the "
                   f"network sentence is not a fix")

    # INV-4 — declared payment values equal the derived ones.
    allowed_addrs = {a.lower() for a in (facts["asset"], facts["pay_to"]) if a}
    for addr in sorted(addresses(text) - allowed_addrs):
        bad.append(f"INV-4 {surface}: carries address {addr} which is neither "
                   f"the configured asset nor the configured payTo")
    for m in _AMOUNT_DECL.finditer(text):
        if m.group("v") != facts["till_amount_atomic"]:
            bad.append(f"INV-4 {surface}: declares amount {m.group('v')!r} but "
                       f"the till charges {facts['till_amount_atomic']!r}")
    for m in _NETWORK_DECL.finditer(text):
        if m.group("v") != net:
            bad.append(f"INV-4 {surface}: declares network {m.group('v')!r} but "
                       f"config is {net!r}")

    # INV-5 — no unsubstituted template token reached the wire.
    for tok in sorted(set(_TOKEN.findall(text))):
        bad.append(f"INV-5 {surface}: rendered the unsubstituted token {tok!r}")
    return bad


def scan_mode(docs_, net):
    """All violations across every swept surface of one mode, as (surface, msg)."""
    d = docs_[net]
    forbidden = forbidden_for(docs_, net)
    out = []
    for surface, text in sorted(d["surfaces"].items()):
        for msg in scan_surface(surface, text, d["facts"], forbidden):
            out.append((surface, msg))
    return out


def violations(docs_, net, surface):
    d = docs_[net]
    return scan_surface(surface, d["surfaces"][surface], d["facts"],
                        forbidden_for(docs_, net))


# ── the sweep itself ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("net", NETWORKS)
def test_app_serves_discovery_in_both_modes(docs, net):
    assert "error" not in docs[net], docs[net].get("error", "")[:400]
    assert docs[net]["surfaces"], "the sweep fetched nothing"


@pytest.mark.parametrize("net", NETWORKS)
def test_every_served_route_is_swept_or_recorded_as_skipped(docs, net):
    """A guard can only pin what it actually fetched.

    D-1312's gap was a surface absent from a hand-written list. The sweep is
    enumerated from the app's route table instead, so coverage cannot shrink by
    typo: every route is either swept or named in `skipped` with a reason.
    """
    d = docs[net]
    accounted = set(d["surfaces"]) | set(d["skipped"]) | {"POST::/v1/billing/x402"}
    missing = [r for r in d["routes"] if r not in accounted]
    assert not missing, f"routes neither swept nor skipped: {missing}"
    unexpected = sorted(set(d["skipped"]) - {NON_TEXT_ROUTE})
    assert not unexpected, (
        f"routes left the sweep for a non-binary reason: "
        f"{ {u: d['skipped'][u] for u in unexpected} }")
    assert len(d["surfaces"]) >= 40, (
        f"only {len(d['surfaces'])} surfaces swept — the sweep lost coverage")


@pytest.mark.parametrize("net", NETWORKS)
def test_the_sweep_reaches_every_surface_that_speaks_for_the_product(docs, net):
    """The load-bearing surfaces must be IN the sweep, named explicitly.

    The sweep is dynamic; this pins that the dynamic set actually contains the
    documents an agent reads, so a refactor that stops serving one fails here
    rather than silently narrowing the guard.
    """
    required = {
        "/", "/start", "/llms.txt", "/agents.txt", "/agent-ledger", "/docs",
        "/openapi.json", "/skill.md", "/.well-known/x402", "/.well-known/x402.json",
        "/.well-known/agent-card.json", "/.well-known/mcp.json", "/sitemap.xml",
        "mcp::ledger_api_docs(rest)", "mcp::ledger_api_docs()",
    }
    got = set(docs[net]["surfaces"])
    assert not (required - got), f"surfaces missing from the sweep: {required - got}"


# ── INV-1 / INV-2 / INV-4 / INV-5: every surface, every mode ─────────────────

@pytest.mark.parametrize("surface", [
    "/", "/start", "/llms.txt", "/agents.txt", "/agent-ledger", "/docs",
    "/openapi.json", "/skill.md", "/skill", "/sitemap.xml", "/robots.txt",
    "/stats", "/status", "/about", "/terms", "/privacy", "/security",
    "/quickstart", "/compare", "/reliability", "/perimeter-watch", "/cited",
    "/agent-watch", "/trust-scan", "/server.json", "/mcp.json", "/agents.json",
    "/agent-directory.json", "/.well-known/x402", "/.well-known/x402.json",
    "/.well-known/payments", "/.well-known/agent.json",
    "/.well-known/agent-card.json", "/.well-known/agent-directory.json",
    "/.well-known/agents.json", "/.well-known/mcp", "/.well-known/mcp.json",
    "/.well-known/mcp/server-card.json", "/.well-known/glama.json",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
    "/mcp::ledger_api_docs(rest)", "/mcp::ledger_api_docs()",
    "/POST::/v1/billing/x402",
    "/POST::/v1/billing/x402/mpp",
])
def test_no_surface_violates_the_network_invariants(docs, surface):
    """The property assertion. Wording-independent; identical in both modes.

    `surface` is written with a leading slash so pytest ids stay readable; the
    MCP and till keys are mapped back below.
    """
    key = surface[1:] if surface.startswith("/mcp::") or \
        surface.startswith("/POST::") else surface
    for net in NETWORKS:
        if key not in docs[net]["surfaces"]:
            if key in ("POST::/v1/billing/x402", "POST::/v1/billing/x402/mpp"):
                continue  # the till refuses rather than 402s in this mode
            pytest.fail(f"{key} was not swept under {net} — coverage gap")
        found = violations(docs, net, key)
        assert not found, "\n".join(found)


def test_every_swept_surface_is_invariant_clean(docs):
    """The whole sweep, both modes, in one assertion — nothing excluded."""
    problems = []
    for net in NETWORKS:
        problems += [(net,) + p for p in scan_mode(docs, net)]
    assert not problems, "\n".join(f"[{n}] {m}" for n, _s, m in problems)


# ── INV-3: the anti-deletion floor, over the whole sweep ─────────────────────

@pytest.mark.parametrize("net", NETWORKS)
def test_a_surface_stating_payment_terms_names_the_configured_network(docs, net):
    """Stops the fix 'delete the sentence'.

    Deleting the network sentence from a surface that still quotes the USDC
    contract or the payTo is not a fix — the surface still asks for payment while
    declining to say where. `scan_surface` reports that as INV-3; this test makes
    the failure legible by listing the surfaces it applies to.
    """
    d = docs[net]
    terms = []
    for surface, text in sorted(d["surfaces"].items()):
        if any(t and t.lower() in text.lower()
               for t in (d["facts"]["asset"], d["facts"]["pay_to"])):
            terms.append(surface)
    assert len(terms) >= 4, (
        f"expected several surfaces to state payment parameters, found {terms} — "
        f"if the parameters stopped being published, INV-3 has nothing to guard")
    found = [v for s in terms for v in violations(docs, net, s) if "INV-3" in v]
    assert not found, "\n".join(found)


# ── required disclosure: prose pages that state no parameters ────────────────

# INV-3 keys off payment parameters, so it does not reach these. /agents.txt is
# the surface the historical leak actually lived on and it names no address, so
# the disclosure is required explicitly. The value asserted is `facts["network"]`
# — the configured id — never a literal.
REQUIRED_DISCLOSURE = (
    "/llms.txt",
    "/start",
    "/agents.txt",
    "/agent-ledger",
    "/openapi.json",
    "/skill.md",
    "/.well-known/x402",
    "/.well-known/x402.json",
    "/.well-known/agent-card.json",
    "/.well-known/mcp.json",
    "/mcp.json",
    "mcp::ledger_api_docs(rest)",
    "mcp::ledger_api_docs()",
)


# MPP surfaces added by D-1319.
MPP_SURFACES = (
    "POST::/v1/billing/x402/mpp",
    "POST::/v1/billing/x402/mpp_decoded",
)


@pytest.mark.parametrize("surface", REQUIRED_DISCLOSURE)
def test_required_surfaces_disclose_the_configured_network_in_both_modes(docs, surface):
    """Each of these must name the network it actually settles on — in BOTH modes.

    This is the negative control D-1312 established, generalised: the invariant is
    "names the CONFIGURED network", never "testnet must not appear". A testnet
    deployment MUST disclose testnet, and this asserts exactly that when config is
    testnet. Because it runs in both modes, deleting the sentence to satisfy the
    mainnet side is detected immediately.
    """
    for net in NETWORKS:
        text = docs[net]["surfaces"][surface]
        assert net in network_ids(text, docs[net]["facts"]["namespaces"]), (
            f"{surface} does not name the configured network {net} — a surface "
            f"that settles somewhere must say where")


def test_the_disclosure_requirement_is_not_satisfied_by_the_other_mode(docs):
    """Guard-on-the-guard: the required set must actually discriminate.

    If both modes produced the same id, every assertion above would pass while
    pinning nothing. Asserts the two modes genuinely disagree, derived from config.
    """
    assert len(NETWORKS) >= 2
    ids = {net: docs[net]["facts"]["network"] for net in NETWORKS}
    assert len(set(ids.values())) == len(NETWORKS), (
        f"config did not produce distinct networks: {ids}")
    a, b = NETWORKS[0], NETWORKS[1]
    surface = "/llms.txt"
    assert docs[a]["facts"]["network"] not in network_ids(
        docs[b]["surfaces"][surface], docs[b]["facts"]["namespaces"]), (
        f"{surface} names {a}'s network while configured for {b}")


# ── the till: the envelope an agent is actually asked to pay ─────────────────

def test_the_till_envelope_matches_the_configured_network(docs):
    """The 402 challenge is the contract. It must agree with config.

    In the mode that can settle, the served challenge's `accepts[0]` must carry
    the configured network, asset, amount and payTo. Where the challenge is not
    served, the refusal must be EXPLICIT and network-consistent — never a silent
    absence, which is how an agent ends up with a 402 it cannot honour.
    """
    served, refused = [], []
    for net in NETWORKS:
        ch = docs[net]["challenge"]
        if ch["decoded"]:
            served.append(net)
            acc = json.loads(ch["decoded"])["accepts"][0]
            f = docs[net]["facts"]
            assert acc["network"] == f["network"], \
                f"till envelope network {acc['network']} != config {f['network']}"
            assert acc["asset"].lower() == f["asset"].lower(), \
                "till envelope asset did not follow the configured network"
            assert acc["amount"] == f["till_amount_atomic"], \
                (f"till envelope asks for {acc['amount']} but the registered "
                 f"price is {f['till_amount_atomic']}")
            assert acc["payTo"].lower() == f["pay_to"].lower(), \
                "till envelope payTo is not the configured receiving wallet"
        else:
            refused.append(net)
            assert ch["status"] != 402, (
                f"{net}: a 402 without a decodable envelope leaves the agent "
                f"nothing to pay against"
                + (f" (decode failed: {ch['decode_error']})"
                   if ch.get("decode_error") else ""))
            f = docs[net]["facts"]
            blob = ch["body"] + "\n" + f["disabled_reason"]
            assert f["network"] in network_ids(blob, f["namespaces"]), (
                f"{net}: the till is not serving a challenge and the refusal "
                f"does not name the configured network — an unexplained "
                f"non-payment surface: {blob[:200]}")
    assert served or refused, "the till neither served a challenge nor refused"


def test_mpp_challenge_is_well_formed_and_bound(docs):
    """Path A: the MPP WWW-Authenticate header is valid, honest, and bound.

    The header must use the custom `x402-base` method (not stripe/tempo),
    charge intent, realm matching the host, and a base64url JCS request
    containing amount (base units), currency and recipient == payTo. The id
    must change when terms change (proven by the SDK's HMAC binding).
    """
    for net in NETWORKS:
        f = docs[net]["facts"]
        if not f["mpp_enabled"]:
            continue
        www = docs[net]["surfaces"].get("POST::/v1/billing/x402/mpp", "")
        assert www, f"{net}: MPP enabled but no WWW-Authenticate: Payment header"
        params = {}
        for m in re.finditer(
                r'([a-zA-Z_][\w-]*)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))',
                www[len("Payment "):]
        ):
            params[m.group(1)] = m.group(2) or m.group(3)
        assert params.get("method") == "x402-base", (
            f"{net}: MPP method must be the custom x402-base, got "
            f"{params.get('method')!r}")
        assert params.get("intent") == "charge", (
            f"{net}: MPP intent must be charge, got {params.get('intent')!r}")
        req = decode_payment_request(www)
        assert req["amount"] == f["till_amount_atomic"], (
            f"{net}: MPP amount {req['amount']!r} != till amount "
            f"{f['till_amount_atomic']!r}")
        assert req["currency"].lower() == f["asset"].lower(), (
            f"{net}: MPP currency {req['currency']!r} != configured asset")
        assert req["recipient"].lower() == f["pay_to"].lower(), (
            f"{net}: MPP recipient {req['recipient']!r} != configured payTo")
        assert "id" in params and params["id"], f"{net}: MPP challenge missing id"


def test_mpp_challenge_absent_without_secret(docs):
    """Fail closed: no MPP_SECRET_KEY means no WWW-Authenticate challenge."""
    mainnet = mainnet_network()
    d = render(mainnet, facilitator=FACILITATOR_INPUT,
               extra_env={"MPP_SECRET_KEY": ""})
    assert "error" not in d, d.get("error", "")[:400]
    assert not d["facts"].get("mpp_enabled"), "MPP must be disabled with no secret"
    assert "POST::/v1/billing/x402/mpp" not in d["surfaces"], (
        "a challenge was emitted when no MPP secret is configured")


def test_mpp_surface_guard_catches_wrong_amount():
    """Negative control: a synthetic wrong amount in an MPP offer is RED, then GREEN.

    Criterion 7 requires the guard to catch a bad MPP surface in NEW wording
    the old phrase list never matched, and then recover when the bad surface is
    removed. render() boots the app in a subprocess, so in-process monkey-
    patching cannot reach it; we use a subprocess-only env hook
    `_MPP_TEST_OFFER_OVERRIDES` that production code never consults for real
    pricing. The injected amount "99999" is intentionally wrong and is checked
    by the guard's INV-4 invariant (declared amount must equal what the till
    charges), not by a phrase list.
    """
    net = NETWORKS[0]

    # RED: inject a wrong amount into ONLY the /v1/billing/x402 MPP offer.
    d = render(net, extra_env={
        "MPP_SECRET_KEY": "test-secret-for-guard",
        "_MPP_TEST_OFFER_OVERRIDES": "/v1/billing/x402=99999",
    })
    assert "error" not in d, d.get("error", "")[:400]
    op = json.loads(d["surfaces"]["/openapi.json"])[
        "paths"]["/v1/billing/x402"]["post"]
    offer_amounts = [o["amount"] for o in
                     op.get("x-payment-info", {}).get("offers", [])]
    assert "99999" in offer_amounts, (
        "harness: synthetic wrong amount did not reach the MPP offer")
    red = scan_mode({net: d}, net)
    red_messages = [m for _s, m in red if "INV-4" in m and "99999" in m]
    assert red_messages, (
        f"guard did not go RED on the injected wrong amount. All violations: {red}")

    # GREEN: remove the override; the same surface must be clean again.
    d2 = render(net, extra_env={"MPP_SECRET_KEY": "test-secret-for-guard"})
    assert "error" not in d2, d2.get("error", "")[:400]
    op2 = json.loads(d2["surfaces"]["/openapi.json"])[
        "paths"]["/v1/billing/x402"]["post"]
    offer_amounts2 = [o["amount"] for o in
                      op2.get("x-payment-info", {}).get("offers", [])]
    assert offer_amounts2, "harness: MPP offer disappeared without override"
    assert "99999" not in offer_amounts2, (
        "harness: override leaked into the GREEN boot")
    green = scan_mode({net: d2}, net)
    assert not green, (
        f"guard stayed RED after removing the override: {green}")


def test_mpp_offer_is_in_openapi(docs):
    """The OpenAPI discovery doc carries MPP's offers[] shape alongside x402."""
    for net in NETWORKS:
        f = docs[net]["facts"]
        op = json.loads(docs[net]["surfaces"]["/openapi.json"])[
            "paths"]["/v1/billing/x402"]["post"]
        offers = op.get("x-payment-info", {}).get("offers", [])
        assert offers, f"{net}: x-payment-info.offers[] is missing"
        offer = offers[0]
        assert offer["method"] == "x402-base", f"{net}: offer method wrong"
        assert offer["intent"] == "charge"
        assert offer["amount"] == f["till_amount_atomic"], (
            f"{net}: offer amount {offer['amount']!r} != till amount")
        assert offer["currency"].lower() == f["asset"].lower()


# The ten D-1293 x402scan keys that must all SURVIVE in x-payment-info.
#
# STRUCTURE (settled by Stripe/Tempo's validator, `npx mppx@latest validate`):
# `offers[]` owns the top level, and these keys live under the nested `x402`
# key. Flattening them to the top level was tried and the validator rejected the
# document outright with:
#     "Cannot mix offers with flat payment info fields"
# which costs the endpoint its MPP discoverability. So do NOT "fix" this by
# flattening. What this guard protects is that none of the ten keys is dropped
# or renamed — that is the actual D-1293 contract.
D1293_X_PAYMENT_INFO_KEYS = (
    "protocols",
    "pricingMode",
    "currency",
    "amountUnit",
    "amount",
    "asset",
    "network",
    "payTo",
    "priceUsd",
    "priceDescription",
)


def test_x_service_info_is_at_openapi_root(docs):
    """Criterion 5: x-service-info is a document-root extension, not info-level."""
    for net in NETWORKS:
        schema = json.loads(docs[net]["surfaces"]["/openapi.json"])
        # Must be at document root (sibling of openapi, info, paths).
        assert "x-service-info" in schema, (
            f"{net}: x-service-info missing from OpenAPI document root")
        assert schema["x-service-info"].get("docs", {}).get("llms") == "/llms.txt", (
            f"{net}: x-service-info.docs.llms missing or wrong: "
            f"{schema.get('x-service-info')}")
        # Must NOT be at the old wrong location.
        assert "x-service-info" not in schema.get("info", {}), (
            f"{net}: x-service-info must not live under info; it is a "
            "document-root OpenAPI extension")


def test_all_d1293_keys_survive_under_the_x402_key(docs):
    """D-1293 discovery contract: all ten x402scan keys survive in x-payment-info.

    They live under the nested `x402` key because the validator forbids mixing
    `offers[]` with flat payment-info fields (see the constant's comment). This
    fails if any key is moved, renamed, or dropped — which is the real contract.
    """
    for net in NETWORKS:
        op = json.loads(docs[net]["surfaces"]["/openapi.json"])[
            "paths"]["/v1/billing/x402"]["post"]
        xpi = op.get("x-payment-info", {})
        # offers[] must own the top level, NOT be mixed with flat fields.
        assert "offers" in xpi, f"{net}: x-payment-info.offers[] missing"
        flat_leaks = [k for k in D1293_X_PAYMENT_INFO_KEYS if k in xpi]
        assert not flat_leaks, (
            f"{net}: {flat_leaks} are flat at the top level alongside offers[]; "
            "the validator rejects mixing offers with flat payment info fields")
        # Every one of the ten keys must still be served, under x402.
        x402 = xpi.get("x402", {})
        missing = [k for k in D1293_X_PAYMENT_INFO_KEYS if k not in x402]
        assert not missing, (
            f"{net}: x-payment-info.x402 is missing D-1293 keys: {missing}")


def test_mainnet_without_settlement_credentials_is_refused_not_advertised():
    """A config that could never settle must fail loudly, not publish a 402.

    Without this, X402_NETWORK=mainnet plus no CDP creds would advertise a mainnet
    demand the public x402.org facilitator cannot settle (it serves testnets only),
    so every payment would fail at settlement with no explanation.
    """
    mainnet = mainnet_network()
    d = render(mainnet, facilitator=None)
    assert "error" not in d, "app should still serve, with x402 disabled"
    assert d["facts"]["network"] == mainnet
    assert d["facts"]["enabled"] is False, (
        "mainnet without CDP creds must report x402 disabled rather than enabled")
    assert d["facts"]["disabled_reason"], "a refusal must carry its reason"


# ── the documented single source of truth for the price ──────────────────────

def test_the_discovery_amount_follows_the_price_the_till_charges():
    """One price, one source. This is INV-2's `amount` case under a changed price.

    Booting with a price that differs from the default is the only way to tell a
    DERIVED amount from a transcribed one. With a single source, flipping
    X402_MINT_PRICE must move the MPP offer amount, the x402 challenge amount,
    and every served discovery document together.
    """
    net = NETWORKS[0]
    d = render(net, extra_env={"X402_MINT_PRICE": "$0.07",
                                "MPP_SECRET_KEY": "test-secret-for-guard"})
    assert "error" not in d, d.get("error", "")[:400]
    assert d["facts"]["till_amount_atomic"] == "70000", \
        "harness: the price override did not reach the till"
    # The MPP offer must move too.
    op = json.loads(d["surfaces"]["/openapi.json"])[
        "paths"]["/v1/billing/x402"]["post"]
    offer_amounts = [o["amount"] for o in
                     op.get("x-payment-info", {}).get("offers", [])]
    assert "70000" in offer_amounts, (
        "MPP offer amount did not follow the price flip")
    found = scan_mode({net: d}, net)
    assert not found, "\n".join(f"[{s}] {m}" for s, m in found)


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
    assert path in docs[NETWORKS[0]]["surfaces"], f"{path} was not swept"
    assert docs[NETWORKS[0]]["statuses"][path] == 200, \
        f"{path} -> {docs[NETWORKS[0]]['statuses'][path]}"
    body = docs[NETWORKS[0]]["surfaces"][path]
    assert body.strip(), f"{path} must return a non-empty document"
    json.loads(body)  # these spellings are JSON manifests


# ── D-1280: agents stuck at the MCP door ─────────────────────────────────────

def test_oauth_discovery_paths_answer_instead_of_404ing(docs):
    """150 OAuth discovery probes 404'd. A 404 reads as 'discovery broken'."""
    for p in ["/.well-known/oauth-protected-resource",
              "/.well-known/oauth-protected-resource/mcp",
              "/.well-known/oauth-authorization-server",
              "/.well-known/oauth-authorization-server/mcp",
              "/mcp/.well-known/oauth-protected-resource",
              "/mcp/.well-known/oauth-authorization-server"]:
        for net in NETWORKS:
            assert docs[net]["statuses"][p] == 200, \
                f"{p} -> {docs[net]['statuses'][p]} under {net}"
            # Must be an EMPTY doc: we have no auth server, and pointing a client
            # at a fake one would be worse than the 404.
            body = json.loads(docs[net]["surfaces"][p])
            assert body == {}, f"{p} must declare no authorization server, got {body}"


# ── D-1293: the payable endpoint must be REGISTERABLE by a directory ──────────

def test_the_x402_endpoint_has_an_input_schema_and_a_402(docs):
    """x402scan refused to register the ONLY endpoint that takes money:
    "validation: Missing input schema — add a requestBody or parameter schema
    to your OpenAPI spec so agents know what to send" (live, 2026-09-16:
    registerFromOrigin -> noValidResources, failed 1 / skipped 75).

    The route takes a raw Request, so FastAPI emitted no requestBody and the
    endpoint was treated as non-invocable. A directory cannot list an endpoint
    it does not know how to call.
    """
    net = NETWORKS[0]
    op = json.loads(docs[net]["surfaces"]["/openapi.json"])["paths"]["/v1/billing/x402"]["post"]
    assert "requestBody" in op, "no input schema — directory registration will fail"
    assert op["requestBody"]["content"]["application/json"]["schema"]["type"] == "object"
    assert "402" in op["responses"], "the 402 challenge is this endpoint's primary contract"


@pytest.mark.parametrize("net", NETWORKS)
def test_x_payment_info_is_derived_from_the_configured_network(docs, net):
    """Prices/network/asset must come from the same config the endpoint charges
    from. A second literal copy is a second thing that can go wrong silently —
    the exact bug class of `eip155:845` and the hardcoded testnet asset.

    Every expected value here is read from `facts` (the running app's own
    derivation), never written as a literal.
    """
    f = docs[net]["facts"]
    xpi = json.loads(docs[net]["surfaces"]["/openapi.json"])[
        "paths"]["/v1/billing/x402"]["post"]["x-payment-info"]
    # MPP shape: offers[] at the top level.
    offers = xpi.get("offers", [])
    assert offers, "x-payment-info.offers[] missing"
    assert offers[0]["method"] == "x402-base"
    assert offers[0]["intent"] == "charge"
    assert offers[0]["amount"] == f["amount_atomic"], "offer amount did not follow price"
    assert offers[0]["currency"].lower() == f["asset"].lower()
    # x402scan shape: all ten keys survive, under the nested x402 key
    # (validator forbids mixing offers[] with flat payment-info fields).
    x402 = xpi.get("x402", {})
    for k in D1293_X_PAYMENT_INFO_KEYS:
        assert k in x402, f"x-payment-info.x402 missing D-1293 key {k!r}"
    assert x402.get("network") == f["network"], "network missing"
    assert x402.get("asset").lower() == f["asset"].lower(), "asset missing"
    assert x402.get("payTo").lower() == f["pay_to"].lower(), "payTo missing"
    assert x402.get("amount") == f["amount_atomic"], "amount missing"
    assert x402.get("pricingMode") == "fixed"
    assert x402.get("protocols", [{}])[0].get("protocol") == "x402"


def test_both_well_known_spellings_serve_the_fan_out(docs):
    """x402scan fetches `/.well-known/x402` first, then `.json`. Its docs say
    registerFromOrigin fails with `noDiscovery` when only one variant exists.
    One function serves both, so they cannot disagree."""
    net = NETWORKS[0]
    f = docs[net]["facts"]
    a = json.loads(docs[net]["surfaces"]["/.well-known/x402"])
    b = json.loads(docs[net]["surfaces"]["/.well-known/x402.json"])
    assert a["version"] == 1 and b["version"] == 1
    assert a["resources"] == b["resources"] == ["POST https://aiagentscity.com/v1/billing/x402"]
    # the rich challenge detail must survive the addition
    assert a["x402Version"] == 2
    assert a["payTo"].lower() == f["pay_to"].lower()
    assert a["network"] == f["network"]


# ── D-1314 Part B: the buyer skill must be findable ─────────────────────────

@pytest.mark.parametrize("net", NETWORKS)
def test_agents_txt_discovery_block_lists_the_buyer_skill(docs, net):
    """The published skill was linked from llms.txt only. An agent that reads the
    .txt orientation surface — the one crawlers actually request — could not find
    it. The DISCOVERY block exists precisely to list discovery surfaces."""
    body = docs[net]["surfaces"]["/agents.txt"]
    assert "DISCOVERY" in body, "/agents.txt lost its DISCOVERY block"
    block = body.split("DISCOVERY", 1)[1].split("PAYMENT STATUS", 1)[0]
    assert "/skill.md" in block, (
        "/agents.txt DISCOVERY must list /skill.md — the buyer skill is invisible "
        "to a crawler that reads this surface")


@pytest.mark.parametrize("net", NETWORKS)
def test_the_sitemap_lists_the_buyer_skill(docs, net):
    """D-1314 decision: /skill.md IS sitemap content.

    The old docstring excluded "API and discovery paths ... because they are not
    indexable content" while the list itself already carried /docs and /stats, so
    the rule was about CONTENT, not path shape. /skill.md is prose served as
    text/markdown — the same class as /docs. The docstring now states that rule,
    and this test pins the entry so the code and the docstring cannot drift apart
    again.
    """
    xml = docs[net]["surfaces"]["/sitemap.xml"]
    locs = re.findall(r"<loc>([^<]+)</loc>", xml)
    assert locs, "sitemap has no <loc> entries"
    assert any(l.endswith("/skill.md") for l in locs), (
        f"/skill.md is missing from sitemap.xml; located: {locs}")
    # the exclusion the docstring still claims must NOT be applied to descriptors
    assert not any(l.endswith(("/openapi.json", "/agents.txt", "/llms.txt"))
                   for l in locs), (
        "transport descriptors are still excluded — the docstring says so and the "
        "list must agree")
