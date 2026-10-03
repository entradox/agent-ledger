"""D-1553 — /pricing.md and /okf/index.md agent surfaces."""

import re

from fastapi.testclient import TestClient

import api_server
import pricing


def _client():
    return TestClient(api_server.app)


def test_pricing_md_serves_every_product_and_tier_price():
    r = _client().get("/pricing.md")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    body = r.text
    for prod in pricing.PRODUCTS.values():
        assert f"## {prod['name']}" in body
        for t in prod["tiers"]:
            assert f"**{t['name']} — " in body
    # prices come from PRODUCTS, not invented literals
    assert "$19/mo" in body and "$79/mo" in body and "$49 one-time" in body
    # the x402 rail states its real configured price
    import x402_verify
    assert x402_verify.X402_MINT_PRICE in body
    # no bare Stripe URLs — agents must enter through the workspace door
    assert "buy.stripe" not in body and "checkout.stripe" not in body


def test_okf_index_maps_real_surfaces():
    for p in ("/okf", "/okf/", "/okf/index.md"):
        r = _client().get(p)
        assert r.status_code == 200, p
        assert r.headers["content-type"].startswith("text/markdown"), p
    body = _client().get("/okf/index.md").text
    for surface in ("/openapi.json", "/server.json", "/llms.txt", "/skill.md",
                    "/.well-known/x402.json", "/status", "/sitemap.xml", "/pricing.md"):
        assert surface in body


def test_pricing_md_has_no_html():
    body = _client().get("/pricing.md").text
    assert "<div" not in body and "<script" not in body
