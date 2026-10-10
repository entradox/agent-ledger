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
    "agent-ledger": 17, "agent-watch": 8, "perimeter-watch": 6,
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


def test_catalog_agent_ledger_version_matches_the_running_app(client):
    """The catalog's agent-ledger entry must agree with the app's OWN APP_VERSION.

    This is the invariant that would have caught the drift found on 2026-10-07: the
    catalog was copied from a September registry dict and claimed v0.4.1 while the
    code served v0.4.3. Derive it from APP_VERSION (the single source /health uses),
    never hardcode a second copy — but do NOT compare against CITY_CATALOG itself,
    which is the tautology this suite already had to be rewritten for.
    """
    d = client.get("/v1/products").json()
    led = next(p for p in d["products"] if p["id"] == "agent-ledger")
    assert led["version"].lstrip("v") == api_server.APP_VERSION, \
        f"catalog says {led['version']}, app serves {api_server.APP_VERSION}"


def test_catalog_declares_when_its_versions_were_observed(client):
    """Satellite versions cannot be derived in-process, so the payload must say WHEN
    they were probed. A version claim with no date is a claim nobody can age."""
    d = client.get("/v1/products").json()
    assert d["versions_observed"] == api_server.CITY_CATALOG_VERSIONS_OBSERVED
    assert d["versions_observed"], "versions_observed must not be empty"


def test_mcp_catalog_and_products_agree_on_versions(client):
    """mcp.json and /v1/products are built by SEPARATE code paths from CITY_CATALOG.
    A drift in either builder is a live contradiction between two discovery surfaces
    a crawler may read. (This is not tautological: the two dicts are constructed
    independently, so a change to one field mapping does not move the other.)

    Also pins the versions as LITERALS — the 2026-10-07 finding was five stale version
    strings, and a change here must be a conscious act, not a silent carry-over."""
    expected = {
        "agent-ledger": "v0.4.6", "agent-watch": "v0.1.0", "perimeter-watch": "v1.0",
        "cited": "v1.0", "trustscan": "v0.1.0",
    }
    cat = {p["id"]: p["version"] for p in client.get("/v1/products").json()["products"]}
    mcp = {s["id"]: s["version"] for s in client.get("/.well-known/mcp.json").json()["servers"]}
    assert cat == mcp, f"catalog vs mcp.json disagree: {cat} != {mcp}"
    assert cat == expected, f"versions drifted from the frozen set: {cat} != {expected}"


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

def test_catalog_lists_exactly_the_tools_the_mcp_server_actually_serves():
    """The catalog's tool list must equal the MCP server's OWN registry.

    Why this is not tautological, and why the obvious version of this test was
    worthless: CITY_CATALOG is a hand-written literal list, while this reads the
    FastMCP registry that the @mcp.tool decorators built. They are two independent
    objects, so drift in either one is caught.

    The bug this exists for, found live on 2026-10-07: the catalog advertised
    `ledger_list_agents`, which does not exist. An agent that read the catalog and
    called it got "Unknown tool". Worse, that specific name was removed from the MCP
    surface on 2026-09-26 precisely because advertising it told every arriving agent
    that a cross-tenant listing exists and that its key is called admin_secret
    (al_mcp_http.py:458) — so the stale list re-published a hint that had been
    deliberately withdrawn.

    A real tools/call cannot be asserted in-suite: the city gateway translates
    satellite calls to REST, so a call returns 200 even when the native tool name
    does not exist (verified — `ts_health` returned 200 while `ts_health` was absent,
    because the translation layer answered). That is why the live check lives in
    scripts/audit_city_tools.py, and this test guards the registry itself.
    """
    import asyncio
    import al_mcp_http

    served = sorted(t.name for t in asyncio.run(al_mcp_http.mcp.list_tools()))
    claimed = next(p["tools"] for p in api_server.CITY_CATALOG
                   if p["id"] == "agent-ledger")

    phantom = [t for t in claimed if t not in served]
    missing = [t for t in served if t not in claimed]
    assert not phantom, f"catalog advertises tools that do not exist: {phantom}"
    assert not missing, f"MCP serves tools the catalog does not advertise: {missing}"
    assert len(claimed) == EXPECTED_TOOL_COUNTS["agent-ledger"]


def test_no_discovery_surface_re_advertises_the_removed_owner_tool():
    """/llms.txt is the surface a scanning agent reads first. It carried the same
    phantom `ledger_list_agents  — owner-only (admin_secret param)` line: the tool,
    the parameter name, and the claim that a cross-tenant listing exists. All three
    were deliberately withdrawn on 2026-09-26."""
    assert "ledger_list_agents" not in api_server.LLMS_TXT
    assert "admin_secret" not in api_server.LLMS_TXT


def test_llms_txt_versions_match_the_catalog():
    """Two hand-maintained version lists in one file drift. The /llms.txt body
    advertised v1.30.0 for three satellites and v4.0.3 for trustscan while the
    catalog (corrected 2026-10-07) said v0.1.0/v1.0/v1.0/v0.1.0. Every version the
    catalog states must appear in the manifest; a crawler reading both should not
    find two different answers."""
    txt = api_server.LLMS_TXT
    for p in api_server.CITY_CATALOG:
        assert p["version"] in txt, (
            f"{p['id']} version {p['version']} is in the catalog but not /llms.txt")
    for stale in ("v1.30.0", "v4.0.3", "v0.4.1"):
        assert stale not in txt, f"/llms.txt still advertises the stale version {stale}"
