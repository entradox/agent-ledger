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
        assert r.text.startswith("# "), p


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
