# tests/test_city_index.py
"""D-1644 — the umbrella index is generated from city_registry.json and every
product carries the blueprint-v2 frame as FIELDS: slot (trust|outcome|
trust+outcome), direction (outward|spine|inward), connect, free_proof.

The card's eval criteria, pinned as tests:

1. /llms.txt and /openapi.json expose the live products each with slot +
   direction + connect; agent-compass is absent.
2. The index is GENERATED from the registry — a fixture row changes the
   rendered index on the next request.
3. A wrong path answers the index: JSON for agent callers, an orienting page
   for browsers — never a bare 404.
4. One-pass classification: an agent reading the index alone can say which
   products are TRUST and which are OUTCOME.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest
from fastapi.testclient import TestClient

import api_server
import city_index

LIVE_SLOTS = {"trust", "outcome", "trust+outcome"}
LIVE_DIRECTIONS = {"outward", "spine", "inward"}


@pytest.fixture(scope="module")
def client():
    return TestClient(api_server.app)


# ── Criterion 1: every index surface carries the frame fields ───────────────

def test_every_registry_row_carries_the_frame_fields():
    for p in city_index.products():
        assert p["slot"] in LIVE_SLOTS, p["id"]
        assert p["direction"] in LIVE_DIRECTIONS, p["id"]
        assert p["connect"], f"{p['id']} has no connect surface an agent can call"
        assert p["free_proof"], f"{p['id']} has no free proof"


def test_the_indexed_set_is_the_live_seven_not_the_whole_registry():
    """Exactly the blueprint-v2 live set — benefits-city and workbench-kit
    present, agent-compass (parked, no live endpoint) absent."""
    ids = {p["id"] for p in city_index.products()}
    assert ids == {"agent-ledger", "agent-watch", "perimeter-watch", "cited",
                   "trustscan", "benefits-city", "workbench-kit"}
    assert "agent-compass" not in ids


def test_llms_txt_carries_slots_directions_and_connect_lines(client):
    body = client.get("/llms.txt").text
    assert "### TRUST" in body and "### OUTCOME" in body
    assert "INWARD" in body, "the inward keystone needs its own section"
    for p in city_index.products():
        assert p["id"] in body, f"{p['id']} missing from llms.txt"
        assert f"slot={p['slot']}" in body
        assert f"direction={p['direction']}" in body
    assert "{INDEX_PRODUCTS}" not in body, "unsubstituted token on the wire"
    assert "agent-compass" not in body


def test_openapi_carries_x_products_with_frame_fields(client):
    schema = client.get("/openapi.json").json()
    products = schema["x-products"]
    assert len(products) == len(city_index.products())
    for entry in products:
        assert entry["x-slot"] in LIVE_SLOTS, entry["id"]
        assert entry["x-direction"] in LIVE_DIRECTIONS, entry["id"]
        assert entry["x-connect"], entry["id"]
    assert schema["x-frame"]["frame"] == "trust-and-outcome"
    ids = {e["id"] for e in products}
    assert "benefits-city" in ids and "workbench-kit" in ids
    assert "agent-compass" not in ids


def test_v1_products_rows_carry_the_registry_fields(client):
    d = client.get("/v1/products").json()
    assert d["frame"] == "trust-and-outcome"
    assert d["questions"]["trust"] and d["questions"]["outcome"]
    for p in d["products"]:
        assert p["slot"] in LIVE_SLOTS, p["id"]
        assert p["direction"] in LIVE_DIRECTIONS, p["id"]
        assert p["connect"], p["id"]


def test_registry_and_catalog_cannot_drift():
    """Every catalog row must resolve a registry row (and vice versa) — an
    index entry with no backing product, or a product with no index entry,
    is exactly the stale-index defect this card exists to remove."""
    catalog_ids = {s["id"] for s in api_server.CITY_CATALOG}
    registry_ids = {p["id"] for p in city_index.products()}
    assert catalog_ids == registry_ids, (
        f"catalog/index drift: {catalog_ids ^ registry_ids}")


# ── Criterion 2: generated from the registry, provable with a fixture ────────

def test_a_fixture_row_changes_the_rendered_index(client, tmp_path, monkeypatch):
    fixture = json.loads(
        (Path(__file__).resolve().parent.parent / "city_registry.json")
        .read_text())
    fixture["products"].append({
        "id": "fixture-product", "title": "Fixture",
        "slot": "outcome", "direction": "outward",
        "tagline": "a test row", "connect": {"mcp": "https://x.test/mcp"},
        "free_proof": "none"})
    staged = tmp_path / "city_registry.json"
    staged.write_text(json.dumps(fixture))
    monkeypatch.setenv("CITY_REGISTRY_PATH", str(staged))

    assert "fixture-product" in client.get("/llms.txt").text
    payload = client.get("/v1/products").json()
    # Catalog rows still only carry real products — the enrichment join is
    # by id, so a fixture row appears in the INDEX payload (root + 404) but
    # not as a phantom catalog entry.
    indexed = client.get("/", headers={"Accept": "application/json"}).json()
    assert "fixture-product" in {p["id"] for p in indexed["products"]}


# ── Criterion 3: the wrong path IS an index ─────────────────────────────────

def test_wrong_path_with_json_accept_returns_the_index(client):
    r = client.get("/definitely-not-a-real-path",
                   headers={"Accept": "application/json"})
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    d = r.json()
    assert d["error"]["code"] == "not_found"
    products = d["index"]["products"]
    assert len(products) == len(city_index.products())
    assert all("slot" in p and "connect" in p for p in products)


def test_wrong_path_with_html_accept_orients(client):
    r = client.get("/definitely-not-a-real-path",
                   headers={"Accept": "text/html"})
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")
    assert "/llms.txt" in r.text and "/products" in r.text


def test_api_path_404_stays_a_plain_error(client):
    """A missing resource inside the API is an error, not a product
    discovery question — the index payload would be noise there."""
    r = client.get("/v1/definitely-missing",
                   headers={"Accept": "application/json"})
    assert r.status_code == 404
    assert "index" not in r.json()


# ── Criterion 4: one-pass classification ────────────────────────────────────

def test_an_agent_can_classify_every_product_in_one_pass(client):
    """J5 acceptance: read ONLY the root JSON block and answer 'which are
    TRUST, which are OUTCOME' — no prose parsing, no second surface."""
    d = client.get("/", headers={"Accept": "application/json"}).json()
    trust = {p["id"] for p in d["products"] if p["slot"] == "trust"}
    outcome = {p["id"] for p in d["products"] if p["slot"] == "outcome"}
    inward = {p["id"] for p in d["products"] if p["direction"] == "inward"}
    assert trust == {"trustscan", "agent-watch", "perimeter-watch"}
    assert outcome == {"agent-ledger", "benefits-city", "cited"}
    assert inward == {"workbench-kit"}
    # And each is connectable in the same pass.
    assert all(p["connect"] for p in d["products"])
    assert d["questions"]["trust"].lower().startswith("before")
    assert d["questions"]["outcome"].lower().startswith("after")
