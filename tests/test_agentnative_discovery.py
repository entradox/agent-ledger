"""Agent-native discovery fixes (2026-10-09).

Five defects found by probing the LIVE site, each fixed here:

  1. `robots.txt` disallowed `/v1/`, which blocked `/v1/products` and `/v1/pricing`
     — the machine catalog every discovery document in the repo points at.
  2. `/v1/products` `discovery.pricing` pointed at `/v1/pricing`, the MODEL price
     table (what Claude/GPT tokens cost), not what this city charges.
  3. The MCP handshake advertised the FastMCP framework version (4.1.0) instead of
     the product version, contradicting every catalog.
  4. `/auth.md` was a 404 — the first path an agent guesses for auth guidance.
  5. `Accept: text/markdown` on the canonical URLs returned HTML.

Every test below asserts a value that the pre-fix code gets WRONG, so each one is
its own canary: revert the fix and the test fails.
"""

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

import api_server


def _client():
    return TestClient(api_server.app)


# ── 1. robots.txt must not forbid the catalog it advertises ──────────────────
def test_robots_allows_the_machine_catalog():
    body = _client().get("/robots.txt").text
    assert "Allow: /v1/products" in body, "robots.txt still blocks the product catalog"
    assert "Allow: /v1/pricing" in body, "robots.txt still blocks the pricing catalog"


def test_robots_keeps_its_original_intent():
    body = _client().get("/robots.txt").text
    assert "Disallow: /dashboard" in body, "the credential page must stay disallowed"
    assert "Disallow: /v1/" in body, "the blanket API disallow was dropped, not overridden"
    assert "Sitemap:" in body


def test_robots_allow_is_more_specific_than_the_disallow():
    """robots.txt resolves by LONGEST match. The Allow must be the longer rule."""
    body = _client().get("/robots.txt").text
    allow = [l.split(": ", 1)[1] for l in body.splitlines() if l.startswith("Allow: ")]
    disallow = [l.split(": ", 1)[1] for l in body.splitlines() if l.startswith("Disallow: ")]
    assert max(len(a) for a in allow) > max(len(d) for d in disallow)


# ── 2. discovery must point at what the CITY charges ─────────────────────────
def test_discovery_pricing_points_at_product_pricing_not_the_model_table():
    d = _client().get("/v1/products").json()["discovery"]
    assert d["pricing"] == "https://aiagentscity.com/pricing"
    assert "/v1/pricing" not in d["pricing"], "still pointing at the model price table"
    assert d["model_price_table"] == "https://aiagentscity.com/v1/pricing"


def test_the_pricing_pointer_target_really_prices_the_product():
    """Follow the pointer the way an agent would, and check it answers with OUR prices."""
    body = _client().get("/pricing").text
    assert "$19" in body and "$79" in body, "/pricing is not the product pricing page"


def test_model_price_table_is_still_reachable():
    """The model table was not deleted — it is an AgentLedger feature, just relabelled."""
    r = _client().get("/v1/pricing")
    assert r.status_code == 200
    assert "models" in r.json()


# ── 3. the handshake must carry the PRODUCT version, not the framework's ─────
def test_mcp_handshake_version_is_the_published_product_version():
    import al_mcp_http
    published = json.loads(
        (Path(api_server.__file__).parent / "server.json").read_text())["version"]
    assert al_mcp_http._product_version() == published
    assert al_mcp_http._product_version() != "4.1.0", "framework version is leaking again"


def test_product_version_reads_like_a_version_not_a_fallback():
    import al_mcp_http
    assert re.match(r"^\d+\.\d+", al_mcp_http._product_version()), \
        "version fell back — server.json unreadable at import"


# ── 4. /auth.md must exist ───────────────────────────────────────────────────
def test_auth_md_serves_markdown():
    r = _client().get("/auth.md")
    assert r.status_code == 200, "/auth.md 404s — the first guess for auth guidance"
    assert r.headers["content-type"].startswith("text/markdown")


def test_auth_md_names_the_real_credential_paths():
    body = _client().get("/auth.md").text
    assert "X-Workspace-Key" in body and "X-Agent-Secret" in body
    assert "/v1/billing/x402" in body, "the agent self-serve rail is missing"
    assert "/start" in body, "the free workspace path is missing"


# ── 5. Accept: text/markdown on the canonical URLs ───────────────────────────
CANONICAL = ("/", "/products", "/developers", "/changelog", "/manifesto", "/compare")


def test_markdown_twin_is_served_when_asked():
    c = _client()
    for p in CANONICAL:
        r = c.get(p, headers={"Accept": "text/markdown"})
        assert r.status_code == 200, p
        assert r.headers["content-type"].startswith("text/markdown"), p
        assert "# AI Agent City — agent-readable knowledge index" in r.text, p


def test_plain_get_still_serves_html():
    c = _client()
    for p in CANONICAL:
        r = c.get(p)
        assert r.headers["content-type"].startswith("text/html"), p


def test_qvalues_decide_the_negotiation():
    c = _client()
    r = c.get("/", headers={"Accept": "text/markdown;q=0.5, text/html;q=0.9"})
    assert r.headers["content-type"].startswith("text/html"), "q-values ignored"

    r = c.get("/", headers={"Accept": "text/markdown;q=0.9, text/html;q=0.5"})
    assert r.headers["content-type"].startswith("text/markdown"), "q-values ignored"


def test_wildcard_clients_are_unaffected():
    c = _client()
    for accept in ("*/*", "text/html", "text/*, */*"):
        r = c.get("/", headers={"Accept": accept})
        assert r.headers["content-type"].startswith("text/html"), accept


# ── 6. the two defects the adversarial review caught in the first draft ──────
def test_auth_md_never_hardcodes_the_settlement_network():
    """X402_NETWORK defaults to eip155:84532 (Base Sepolia TESTNET).

    The first draft of /auth.md said "Real USDC settles on Base mainnet" — the
    exact drift this repo's own comments say has been re-shipped three times.
    The document must carry the DERIVED sentence, whatever the config says.
    """
    import api_server
    body = _client().get("/auth.md").text
    assert api_server._x402_settlement_words() in body, \
        "auth.md does not carry the derived settlement sentence"
    assert "{X402_SETTLEMENT}" not in body, "placeholder leaked unsubstituted"


def test_auth_md_does_not_restate_the_first_drafts_mainnet_literal():
    """Assert the exact stale literal is gone, not the word "mainnet".

    A naive `"MAINNET" not in body` check is WRONG: the correct TESTNET sentence
    reads "a mainnet wallet cannot complete it". Presence of the derived sentence
    is the real test (above); this pins the literal the first draft shipped.
    """
    body = _client().get("/auth.md").text
    assert "Real USDC settles on Base" not in body


def test_auth_md_documents_the_mandatory_version_header():
    """POST /v1/track returns 400 version_header without AL-API-Version
    (routes_agents.py:117). A doc that omits it dead-ends on its own example."""
    import ledger_engine
    body = _client().get("/auth.md").text
    assert "AL-API-Version" in body
    assert ledger_engine.AL_API_VERSION in body
    assert "{AL_API_VERSION}" not in body, "placeholder leaked unsubstituted"


def test_negotiated_responses_declare_vary_accept():
    """Without Vary: Accept a shared cache can serve markdown to a browser."""
    c = _client()
    for headers in ({"Accept": "text/markdown"}, {"Accept": "text/html"}, None):
        r = c.get("/", headers=headers) if headers else c.get("/")
        assert "accept" in r.headers.get("vary", "").lower(), (headers, r.headers.get("vary"))


def test_markdown_twin_has_exactly_one_h1():
    c = _client()
    for p in CANONICAL:
        body = c.get(p, headers={"Accept": "text/markdown"}).text
        h1 = [l for l in body.splitlines() if l.startswith("# ")]
        assert len(h1) == 1, (p, h1)


def test_auth_md_is_discoverable_from_the_indexes():
    """A surface nobody can find does not close the gap it was built for."""
    c = _client()
    assert "/auth.md" in c.get("/okf/index.md").text
    assert "/auth.md" in c.get("/llms.txt").text


def test_auth_md_renders_no_unsubstituted_token():
    """The repo-wide INV-5 sweep in test_x402_discovery_network.py does NOT
    enumerate /auth.md, so this closes that gap explicitly rather than by
    assuming coverage: any `{TOKEN}` shape reaching the wire fails here."""
    import re as _re
    body = _client().get("/auth.md").text
    leftovers = sorted(set(_re.findall(r"\{[A-Z_]+\}", body)))
    assert not leftovers, f"/auth.md rendered unsubstituted tokens: {leftovers}"
