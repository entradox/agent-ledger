# tests/test_umbrella_city.py
"""D-1239 — the AI Agent City umbrella: "/" lists every product, each product
has its own page, /about folds into the root, and no page leaks an
infrastructure host or the operator's identity off a product's own page."""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest


@pytest.fixture(autouse=True)
def data_dir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="agent-ledger-city-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, workspace_engine, identity, api_server
    for m in (ledger_engine, workspace_engine, identity, api_server):
        importlib.reload(m)
    yield
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def page():
    from fastapi.testclient import TestClient
    import api_server
    return TestClient(api_server.app)


PRODUCT_ROUTES = ["/agent-ledger", "/perimeter-watch", "/cited", "/agent-watch", "/trust-scan"]

# these six must survive the root cutover untouched (frame card D-1239 constraint)
PRE_EXISTING_LIVE_ROUTES = ["/quickstart", "/demo", "/dashboard", "/status",
                            "/start", "/llms.txt", "/openapi.json", "/privacy",
                            "/terms", "/security", "/compare"]


def test_root_lists_all_five_products(page):
    body = page.get("/").text
    for name in ("AgentLedger", "Perimeter Watch", "Cited", "Agent Watch", "TrustScan"):
        assert name in body, f"{name} missing from the umbrella index"


def test_each_product_has_its_own_page(page):
    for route in PRODUCT_ROUTES:
        r = page.get(route)
        assert r.status_code == 200, f"{route} did not return 200"


def test_about_redirects_to_root(page):
    r = page.get("/about", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/"


def test_pre_existing_live_routes_still_200(page):
    for route in PRE_EXISTING_LIVE_ROUTES:
        r = page.get(route)
        assert r.status_code == 200, f"{route} regressed to non-200 after the umbrella cutover"


def test_no_generated_page_advertises_a_railway_host(page):
    for route in ["/"] + PRODUCT_ROUTES:
        body = page.get(route).text
        assert "up.railway.app" not in body, f"{route} leaks an infrastructure host"


def test_umbrella_root_has_no_operator_identity(page):
    """The 2026-09-14 directive: strip company name and email off any page
    that is not a specific product's own page. AgentLedger's own page keeps
    its operator line (its own compliance surface); the umbrella index and
    the other four product pages must not carry it."""
    for route in ["/", "/perimeter-watch", "/cited", "/agent-watch", "/trust-scan"]:
        body = page.get(route).text
        assert "Parmanand LLC" not in body, f"{route} still names the operator"
        assert "entradox@icloud.com" not in body, f"{route} still leaks the contact email"
