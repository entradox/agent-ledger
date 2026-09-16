# tests/test_skill_md_surface.py
"""D-1313: the validated x402 buyer skill must be reachable on the LIVE surface.

The skill was validated and correct but had no home: /skill, /skills,
/buyer-skill and /SKILL.md all 404'd, and /docs is FastAPI's Swagger UI (not a
docs site), so an agent reading the machine surface could not find it.

These tests pin three separate things, because the failure modes are separate:

1. The route answers 200 as markdown, and its body is the VERSIONED FILE with
   live values substituted — not a divergent copy.
2. The x402 block names the CONFIGURED network and the matching USDC contract,
   and carries no testnet phrase. The project has shipped a stale x402 literal
   three times (`eip155:845`, the testnet asset, the stale mainnet copy).
3. The file actually SHIPS. `.railwayignore` carried `*.md` with only
   `!README.md` negated, which excluded every markdown file from the image:
   /app/skill/agent-ledger/ was present but EMPTY in production and
   skills_list_tool returned {"skills":[]} there while the same call was
   non-empty on a dev box. A served asset the build context ignores 404s in prod
   with every local test green, so that guarantee is asserted, not assumed.

Run: /opt/miniconda3/bin/python3 -m pytest tests/test_skill_md_surface.py -q
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILL_REL = "skill/agent-ledger-buyer.md"
SKILL_FILE = REPO / SKILL_REL

# Keep the in-process app off the real data dir (this module imports api_server
# in a few tests, and that module caches its data dir at import time).
os.environ.setdefault("AGENT_LEDGER_DATA", "/tmp/al_skill_test")

NETWORK_TESTNET, NETWORK_MAINNET = "eip155:84532", "eip155:8453"
USDC_TESTNET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
USDC_MAINNET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAYTO = "0x363c520492EDbA89057bCe696B74263B3295a72A"

# Only ever true on a testnet. A mainnet surface carrying one of these is
# telling a paying agent the paywall will not work for them.
TESTNET_ONLY_PHRASES = ("84532", "Sepolia", "TESTNET ONLY")

# Any surviving {X402_ token means a .replace() did not run — the D-1270 bug
# class, where a page rendered its own placeholder text.
PLACEHOLDER_PREFIX = "{X402_"

# Rendered in a subprocess: the network is read at import time, so both modes
# cannot be exercised in one in-process run.
CHILD = '''import os, sys, json
sys.path.insert(0, "@@REPO@@")
from fastapi.testclient import TestClient
import api_server as a
with TestClient(a.app) as c:
    md = c.get("/skill.md")
    alias = c.get("/skill")
    out = {"md_status": md.status_code,
           "md_ct": md.headers.get("content-type", ""),
           "md_body": md.text,
           "alias_status": alias.status_code,
           "alias_body": alias.text,
           "llms": c.get("/llms.txt").text,
           "live": {"network": a._x402_network(),
                    "network_label": a._x402_network_label(),
                    "asset": a._x402_asset(),
                    "amount_atomic": a._x402_amount_atomic(),
                    "pay_to": a._x402_pay_to() or "",
                    "price_usd": f"{a.X402_MINT_PRICE_FOR_DISCOVERY:.2f}",
                    "free_agent_cap": str(a.BETA_AGENT_CAP),
                    "al_api_version": a.AL_API_VERSION,
                    "pass_hours": str(round(__import__("x402_verify").X402_PRO_PASS_SECONDS / 3600))}}
print("===JSON===")
print(json.dumps(out))
'''


def render(network, cdp=True):
    """Boot the app with X402_NETWORK set, and fetch the skill surface."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("X402_", "CDP_"))}
    env.update({"X402_PAY_TO": PAYTO, "X402_NETWORK": network,
                "AGENT_LEDGER_DATA": "/tmp/al_skill_test"})
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
def mainnet():
    d = render(NETWORK_MAINNET)
    if "error" in d:
        pytest.fail(f"app failed to serve: {d['error'][:400]}")
    return d


# ── the file exists and is the served one ────────────────────────────────────

def test_the_skill_file_is_versioned_in_the_repo():
    assert SKILL_FILE.is_file(), f"{SKILL_REL} must exist — the route reads it"
    assert SKILL_FILE.stat().st_size > 1000, "the skill must not be a stub"


def test_skill_md_returns_200_as_markdown(mainnet):
    assert mainnet["md_status"] == 200, \
        f"GET /skill.md -> {mainnet['md_status']}"
    ctype = mainnet["md_ct"].lower()
    assert "text/markdown" in ctype, \
        f"GET /skill.md must be served as markdown, got {ctype!r}"
    assert "charset=utf-8" in ctype, \
        f"the skill is UTF-8 (it carries em dashes), got {ctype!r}"


def test_the_extensionless_alias_answers_identically(mainnet):
    """/.well-known/x402 and /x402.json set the precedent, and one function
    serves both spellings, so they cannot disagree."""
    assert mainnet["alias_status"] == 200, \
        f"GET /skill -> {mainnet['alias_status']}"
    assert mainnet["alias_body"] == mainnet["md_body"], \
        "the two spellings must serve identical bytes"


def test_served_body_is_the_versioned_file_with_live_values_only(mainnet):
    """The served document must differ from the file ONLY by substitution.

    Substitute the app's OWN live values into the raw template and the result
    must equal the served bytes exactly. That proves two things at once: the
    published text is the reviewed text (nothing added, dropped or reworded on
    the way out), and every value came from live config rather than a pasted
    literal. Forward substitution on purpose — reversing a value like "3" would
    match every "3" in the body and prove nothing.
    """
    raw = SKILL_FILE.read_text(encoding="utf-8")
    served = mainnet["md_body"]
    assert served != raw, "served body is the raw template — no substitution ran"

    live = mainnet["live"]
    expected = raw
    for token, value in (
        ("{X402_NETWORK}", live["network"]),
        ("{X402_NETWORK_LABEL}", live["network_label"]),
        ("{X402_ASSET}", live["asset"]),
        ("{X402_AMOUNT_ATOMIC}", live["amount_atomic"]),
        ("{X402_PRICE_USD}", live["price_usd"]),
        ("{X402_PAY_TO_NOTE}", f" (`{live['pay_to']}`)" if live["pay_to"] else ""),
        ("{X402_PASS_HOURS}", live["pass_hours"]),
        ("{FREE_AGENT_CAP}", live["free_agent_cap"]),
        ("{AL_API_VERSION}", live["al_api_version"]),
    ):
        expected = expected.replace(token, value)

    assert expected == served, (
        "the served skill is not the versioned file plus live substitutions — "
        "the published text and the reviewed text have diverged")
    assert PLACEHOLDER_PREFIX not in served, \
        "served skill rendered an unsubstituted placeholder"


def test_llms_txt_links_the_skill(mainnet):
    """The whole point: an agent that reads llms.txt must find the skill."""
    assert "/skill.md" in mainnet["llms"], \
        "llms.txt must link /skill.md — otherwise the skill has no home again"


# ── the x402 block follows the CONFIGURED network ────────────────────────────

def test_mainnet_skill_names_mainnet_network_and_token(mainnet):
    body = mainnet["md_body"]
    assert NETWORK_MAINNET in body, \
        f"the served skill must name {NETWORK_MAINNET}"
    assert USDC_MAINNET in body, \
        "the served skill must carry the mainnet USDC contract"
    assert "USDC" in body and "10000" in body, \
        "the served skill must state the asset and the atomic amount"


@pytest.mark.parametrize("phrase", TESTNET_ONLY_PHRASES)
def test_mainnet_skill_carries_no_testnet_phrase(mainnet, phrase):
    assert phrase not in mainnet["md_body"], (
        f"the served skill carries {phrase!r} while configured for mainnet — "
        f"this is the sentence that sends paying agents away from a working "
        f"paywall")


def test_mainnet_skill_does_not_advertise_the_testnet_token(mainnet):
    assert USDC_TESTNET not in mainnet["md_body"], \
        "advertised the testnet USDC contract on mainnet"


def test_the_network_is_derived_not_a_literal():
    """The reversal condition: flipping X402_NETWORK must flip the document.

    Without this, a hardcoded "eip155:8453" would pass every mainnet assertion
    above and quietly lie on a testnet deployment.
    """
    d = render(NETWORK_TESTNET)
    if "error" in d:
        pytest.fail(f"app failed to serve on testnet: {d['error'][:400]}")
    body = d["md_body"]
    assert NETWORK_TESTNET in body, \
        "the skill must name the network it actually settles on"
    assert USDC_TESTNET in body, "the asset must follow the network"
    assert USDC_MAINNET not in body, \
        "must not advertise mainnet USDC while configured for testnet"
    assert "mainnet" not in body.lower(), \
        "must not claim mainnet while configured for testnet"


# ── the served asset must actually SHIP ──────────────────────────────────────

def test_the_build_context_does_not_exclude_served_markdown():
    """A served .md that .railwayignore drops 404s in production, silently.

    Not hypothetical: `*.md` with only `!README.md` negated emptied
    /app/skill/agent-ledger/ and dropped /app/recipes.md in the live image, so
    skills_list_tool returned {"skills":[]} there while a dev box returned the
    real skill. The route would answer 200 locally and 404 in production with
    the whole suite green either way.
    """
    rules = [ln.strip() for ln in (REPO / ".railwayignore").read_text().splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    assert not _ignored_by_patterns(SKILL_REL, rules), (
        f"{SKILL_REL} is excluded from the deploy build context by "
        f".railwayignore — GET /skill.md would 404 in production while passing "
        f"every local test. Add a `!skill/**/*.md` negation AFTER the `*.md` rule.")


def _ignored_by_patterns(path, rules):
    """Evaluate .railwayignore gitignore semantics: the LAST matching rule wins.

    Deliberately tiny — it handles the two shapes this file uses (a glob like
    `*.md`, a negation like `!README.md`), which is exactly the pair whose
    ORDERING was the defect.
    """
    import fnmatch
    state = False
    for rule in rules:
        negated = rule.startswith("!")
        pattern = rule[1:] if negated else rule
        hit = False
        if fnmatch.fnmatch(path, pattern):
            hit = True
        elif pattern.startswith("**/"):
            # `**/` matches zero or more directories
            hit = fnmatch.fnmatch(path, pattern[3:])
        elif pattern.startswith("*."):
            # a bare `*.md` in gitignore matches at ANY depth
            hit = fnmatch.fnmatch(Path(path).name, pattern)
        if hit:
            state = not negated
    return state


def test_the_pattern_checker_detects_the_original_defect():
    """Negative control for the test above.

    If `_ignored_by_patterns` returned False for everything, the shipping test
    would pass against the very .railwayignore that caused the prod outage.
    """
    broken = ["*.md", "!README.md"]
    assert _ignored_by_patterns(SKILL_REL, broken) is True, \
        "the checker must flag a served .md as ignored under the broken rules"
    fixed = ["*.md", "!README.md", "!skill/*.md", "!skill/**/*.md"]
    assert _ignored_by_patterns(SKILL_REL, fixed) is False, \
        "the checker must clear the path once the negation is added"
    assert _ignored_by_patterns("README.md", fixed) is False
    assert _ignored_by_patterns("other.md", fixed) is True, \
        "the negation must be narrow — it must not un-ignore every .md"
    # A rule that comes BEFORE the `*.md` glob must not win.
    wrong_order = ["!skill/*.md", "*.md"]
    assert _ignored_by_patterns(SKILL_REL, wrong_order) is True, \
        "ordering matters: an early negation is overridden by a later glob"


# ── no regression on the surfaces this change sits beside ────────────────────

@pytest.mark.parametrize("path,expected", [
    ("/llms.txt", 200), ("/agents.txt", 200), ("/start", 200), ("/docs", 200),
    ("/agent-ledger", 200), ("/server.json", 200),
    # /mcp/ is a POST transport that requires an Accept header; a bare GET is
    # answered with 406, which is it behaving correctly, not regressing.
    ("/mcp/", 406),
])
def test_adjacent_surfaces_still_answer(mainnet, path, expected):
    """As a context manager so the FastMCP lifespan runs — without it every
    /mcp/ request raises 'task group is not initialized', a harness failure that
    reads as a product failure."""
    from fastapi.testclient import TestClient
    import api_server as a
    with TestClient(a.app) as c:
        r = c.get(path)
    assert r.status_code == expected, f"{path} -> {r.status_code}, want {expected}"
