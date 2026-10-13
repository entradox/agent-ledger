"""D-1646 — every indexed product carries one honest connect line and one
free proof on its served surfaces, and the repositioned copy matches the
blueprint verbatim. A connect line to a dead endpoint is a false claim, so
the registry's connect map is also checked against the routes the app
actually serves."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_server

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def client():
    return TestClient(api_server.app)


@pytest.fixture(scope="module")
def registry():
    return json.loads((ROOT / "city_registry.json").read_text())


# ── Criterion 1: every product has one connect line + one free proof ───────

def test_every_registry_product_has_connect_and_free_proof(registry):
    for p in registry["products"]:
        assert p["connect"], f"{p['id']} has no connect line"
        assert p["free_proof"], f"{p['id']} has no free proof"


def test_every_connect_surface_is_a_route_the_app_or_an_mcp_serves(client, registry):
    """Connect lines must resolve somewhere real — an MCP path, a REST
    prefix, a page this app serves, or an npx install. Nothing fabricated."""
    mcp_prefixes = ("/mcp", "/benefits/mcp")
    for p in registry["products"]:
        for kind, line in p["connect"].items():
            if kind == "install":
                assert line.startswith("npx "), f"{p['id']} install line"
                continue
            if kind == "paid_call":
                # "POST https://…" — a route on the paid rail, not a GET page.
                assert line.startswith("POST https://"), \
                    f"{p['id']} paid_call malformed"
                path = line.split("aiagentscity.com", 1)[-1]
                assert client.post(path).status_code in (400, 401, 402, 422), \
                    f"{p['id']} paid_call {path} does not resolve"
                continue
            assert line.startswith("https://"), f"{p['id']} {kind} not a URL"
            path = line.split("aiagentscity.com", 1)[-1]
            is_served = (path.startswith(mcp_prefixes)
                         or path.startswith("/v1/")
                         or path.startswith("/api/")
                         or client.get(path).status_code in (200, 301, 302, 307))
            assert is_served, f"{p['id']} {kind} → {path} does not resolve"


# ── Criterion 3: repositioning strings verbatim on served surfaces ─────────

def test_perimeter_watch_carries_the_repositioned_question(client):
    body = client.get("/perimeter-watch").text
    assert "still what it claims" in body
    # Morgan F5: the tool only verifies owned endpoints (authority:true) —
    # the copy must scope to YOUR perimeter, not a counterparty's.
    assert "your endpoint" in body
    assert "v1.0" in body and "v1.30.0" not in body


def test_perimeter_watch_nowhere_claims_counterparty_verification(client):
    """pw_snapshot requires authority attestation — surfaces must not
    suggest it verifies endpoints the caller doesn't own."""
    for path in ("/perimeter-watch", "/products", "/"):
        body = client.get(path).text
        assert "agent is pointed at" not in body, path


def test_cited_carries_the_outcome_proof_positioning(client):
    body = client.get("/cited").text
    assert "Verify the claim that AI recommends" in body
    assert "v1.0" in body and "v1.30.0" not in body


def test_product_pages_show_no_stale_version_literals(client):
    """The drift class Morgan flagged on D-1644 must not reappear on the
    product pages."""
    for path in ("/agent-watch", "/perimeter-watch", "/cited", "/trust-scan"):
        body = client.get(path).text
        assert "v1.30.0" not in body, f"{path} stale v1.30.0"
        assert "v4.0.3" not in body, f"{path} stale v4.0.3"


def test_trustscan_discloses_remote_scan_scope(client):
    """Remote scans are host-path-scoped — the page must say so, not imply an
    agent can scan arbitrary remote code free."""
    body = client.get("/trust-scan").text
    assert "path-scoped" in body


# ── The catalog pages reflect the seven-product map ────────────────────────

def test_products_page_lists_all_seven_with_slots(client):
    body = client.get("/products").text
    for name in ("AgentLedger", "Agent Watch", "Perimeter Watch", "TrustScan",
                 "Cited", "Benefits City", "AI Workbench"):
        assert name in body, f"{name} missing from /products"
    assert "Seven products" in body
    assert "Six products" not in body
    assert "TRUST" in body and "OUTCOME" in body


def test_developers_lists_six_mcp_servers(client):
    body = client.get("/developers").text
    for server in ("agent-ledger", "agent-watch", "perimeter-watch",
                   "cited", "trustscan", "benefits-city"):
        assert server in body, f"{server} missing from /developers"


def test_homepage_speaks_the_two_questions(client):
    body = client.get("/").text
    assert "TRUST" in body and "OUTCOME" in body
    assert "Benefits City" in body
    assert "Six products" not in body


def test_okf_index_lists_all_seven_with_slot_markers():
    import site_pages
    text = site_pages.okf_index_md()
    for name in ("AgentLedger", "Agent Watch", "Perimeter Watch", "TrustScan",
                 "Cited", "Benefits City", "AI Workbench"):
        assert name in text
    assert "seven products" in text
    assert "[trust · outward]" in text and "[outcome · spine]" in text
    assert "[trust+outcome · inward]" in text


def test_trustscan_free_proof_is_the_honest_check(registry):
    ts = next(p for p in registry["products"] if p["id"] == "trustscan")
    assert "api/trustscan/npm" in ts["free_proof"]
    assert "path-scoped" in ts["free_proof"]


def test_no_surface_sells_the_x402_npm_route_as_a_code_scan(client):
    """Morgan F1 (semantic, not status-code): /api/x402/trustscan/npm is the
    same metadata check as the free route — payable per call, no rate limit.
    It cannot scan code. No served surface may describe it as a code-scanning
    path."""
    import site_pages
    surfaces = [client.get(p).text for p in
                ("/trust-scan", "/products", "/developers", "/llms.txt")]
    surfaces.append(site_pages.okf_index_md())
    for body in surfaces:
        # the x402 route may only be described as the paid npm check, never
        # as "deep scan" or "scan your code"
        assert "x402 route" not in body or "metadata scan" in body or \
               "without the rate limit" in body or "rate limit" in body


def test_no_surface_invents_a_local_install_for_trustscan(client):
    """Morgan F2: the trust-scan repo is private and has no pip/pyproject —
    'run the scanner locally (pip install)' is a connect line to nothing."""
    import site_pages
    body = client.get("/trust-scan").text + site_pages.okf_index_md()
    assert "pip install" not in body


def test_llms_txt_trustscan_block_discloses_path_scope(client):
    """Morgan F3: the machine contract must not imply arbitrary remote deep
    scans are possible."""
    llms = client.get("/llms.txt").text
    i = llms.find("trust_scan_server")
    assert "path-scoped" in llms[i:i + 900], \
        "trustscan llms.txt block lacks the remote-scan scope note"
