"""Discovery surfaces for the city and for PAP-shaped personal agents.

Two routes and one honesty gate:
  GET /v1/products                          — the machine-readable city catalog
  GET /.well-known/personal-agent.json      — PAP-shaped discovery document

Why these exist together: PAP (announced 2026-10-06) specifies that a personal
agent "starts on the website, where it can discover what the company offers and
how to reach it". That is the same gap the city had — a human could read
/about, an agent had five unconnected servers and no index. One catalog, three
renderings (/v1/products, this discovery doc, /.well-known/mcp.json).

WHY THE EXPECTATIONS BELOW ARE LITERALS, NOT `api_server.CITY_CATALOG`:
the first version of this file asserted `d["count"] == len(api_server.CITY_CATALOG)`.
A mutation test proved it was tautological — adding a sixth product to
CITY_CATALOG moved BOTH sides of the assertion, so the suite stayed green while
the served catalog silently gained a phantom product. A guard that cannot fail
is worse than no guard. Pinning the expected ids as literals means a product can
only be added by editing this file too, which forces a conscious decision.

The honesty gate is the most important assertion here: PAP v0.1 is NOT
published (Sierra's own domain returns HTTP 401 as of 2026-10-07). A discovery
document whose field names are a reading of announcement prose must SAY SO
machine-readably, or a consumer treats a guess as an implemented standard.
"""
import pytest
from fastapi.testclient import TestClient

import api_server

# FROZEN expectations. Deliberately duplicated from CITY_CATALOG so that the
# test and the code cannot drift together. Update BOTH when the city changes.
EXPECTED_PRODUCT_IDS = {"agent-ledger", "agent-watch", "perimeter-watch", "cited", "trustscan"}
EXPECTED_COUNT = 5
# Tool counts per product, as actually served. A count that silently drops is a
# user-visible regression on a public surface.
EXPECTED_TOOL_COUNTS = {
    "agent-ledger": 13, "agent-watch": 8, "perimeter-watch": 6,
    "cited": 9, "trustscan": 4,
}


@pytest.fixture(scope="module")
def client():
    return TestClient(api_server.app)


class _FakeReq:
    """Minimal stand-in for what client_ip() reads, so the limiter can be driven
    directly (400 distinct source IPs) without 400 HTTP round-trips."""

    def __init__(self, ip: str):
        self.headers = {"x-forwarded-for": ip}
        self.client = type("C", (), {"host": ip})()


def test_products_catalog_lists_exactly_the_expected_products(client):
    r = client.get("/v1/products")
    assert r.status_code == 200, f"/v1/products -> {r.status_code}"
    d = r.json()
    assert d["count"] == EXPECTED_COUNT, f"catalog count drifted: {d['count']}"
    ids = {p["id"] for p in d["products"]}
    assert ids == EXPECTED_PRODUCT_IDS, f"product set drifted: {ids ^ EXPECTED_PRODUCT_IDS}"


def test_products_catalog_tool_counts_are_what_we_claim(client):
    """A public 'N tools' claim must match the tool list actually served."""
    d = client.get("/v1/products").json()
    for p in d["products"]:
        assert p["tool_count"] == len(p["tools"]), p["id"]
        assert p["tool_count"] == EXPECTED_TOOL_COUNTS[p["id"]], \
            f"{p['id']} tool count is {p['tool_count']}, expected {EXPECTED_TOOL_COUNTS[p['id']]}"
        assert p["mcp_url"].startswith("https://"), p
        assert p["status"], p


def test_products_catalog_carries_the_discovery_index(client):
    """An agent arriving must be able to reach every other surface from here."""
    disc = client.get("/v1/products").json()["discovery"]
    for key in ("llms_txt", "openapi", "mcp_catalog", "pap"):
        assert disc.get(key, "").startswith("https://"), key


def test_products_catalog_forbids_raw_infra_hosts(client):
    """The fleet's own trust-surface rule: no *.up.railway.app on a public
    surface. A catalog is the easiest place for one to leak back in."""
    assert "up.railway.app" not in client.get("/v1/products").text


def test_pap_discovery_document_is_served(client):
    r = client.get("/.well-known/personal-agent.json")
    assert r.status_code == 200, f"PAP doc -> {r.status_code}"
    d = r.json()
    # The PAP-specified shape: a business the agent can discover.
    assert d["business"]["name"]
    assert str(d["business"]["url"]).startswith("https://")
    assert d["sign_in"]["grant"] == "oauth"
    assert d["interfaces"], "an agent needs at least one route to reach the business"
    assert {p["id"] for p in d["products"]} == EXPECTED_PRODUCT_IDS


def test_pap_discovery_declares_it_is_not_a_standard_yet(client):
    """HONESTY GATE. PAP v0.1 is unpublished. If this ever flips to a
    conformance claim without the spec existing, the city is over-claiming on
    a live public surface — the exact defect class this project keeps shipping."""
    d = client.get("/.well-known/personal-agent.json").json()
    assert d["provisional"] is True
    assert d["paper_spec_published"] is False
    assert "not" in d["basis"].lower() and "unpublished" in d["basis"].lower()
    assert "not yet published" in d["spec"].lower()


def test_products_and_pap_doc_cannot_disagree_on_the_product_set(client):
    """Two renderings of one catalog. A product in one and not the other is a
    live contradiction on a discovery surface."""
    cat = client.get("/v1/products").json()
    pap = client.get("/.well-known/personal-agent.json").json()
    assert {p["id"] for p in cat["products"]} == {p["id"] for p in pap["products"]}


def test_discovery_rate_table_is_bounded(client, monkeypatch):
    """The discovery guard keys on client IP on an UNAUTHENTICATED route. Without a
    cap, a rotating source grows `_CITY_RATE` one key per request forever — the guard
    itself becomes the memory-exhaustion vector. Assert a hard ceiling."""
    monkeypatch.setattr(api_server, "CITY_RATE_MAX_KEYS", 50)
    api_server._CITY_RATE.clear()
    try:
        for i in range(400):
            api_server._city_discovery_guard(_FakeReq(f"10.0.0.{i}"))
        assert len(api_server._CITY_RATE) <= 50, \
            f"rate table grew to {len(api_server._CITY_RATE)} keys (cap 50)"
    finally:
        api_server._CITY_RATE.clear()


def test_discovery_rate_limit_still_actually_limits(client, monkeypatch):
    """Bounding the table must not disable the limit it exists to enforce."""
    monkeypatch.setattr(api_server, "CITY_DISCOVERY_RATE_LIMIT", 3)
    api_server._CITY_RATE.clear()
    try:
        req = _FakeReq("203.0.113.9")
        for _ in range(3):
            api_server._city_discovery_guard(req)          # allowed
        with pytest.raises(Exception) as exc:
            api_server._city_discovery_guard(req)          # 4th must be refused
        assert "429" in str(exc.value) or "rate limit" in str(exc.value).lower()
    finally:
        api_server._CITY_RATE.clear()


def test_mcp_catalog_refactor_lists_exactly_the_expected_servers(client):
    """/.well-known/mcp.json now reads CITY_CATALOG (one source of truth) —
    the refactor must not have dropped or renamed a server, and must not have
    re-introduced a raw infra host."""
    r = client.get("/.well-known/mcp.json")
    assert r.status_code == 200
    d = r.json()
    assert {s["id"] for s in d["servers"]} == EXPECTED_PRODUCT_IDS
    assert len(d["servers"]) == EXPECTED_COUNT
    assert "up.railway.app" not in r.text
