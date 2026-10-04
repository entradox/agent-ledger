"""HEAD requests must behave as headers-only GET (RFC 9110 §9.3.2).

starlette 1.x stopped auto-adding HEAD to GET routes, so live HEAD probes
returned 405 while GET on the same path served 200 — seen on /sitemap.xml
where sitemap validators and crawler revalidation probes issue HEAD.
The _head_get_equivalence middleware rewrites HEAD to GET at the outermost
layer and drains the body, preserving the GET-computed headers.
"""
import pytest
from fastapi.testclient import TestClient

import api_server


@pytest.fixture(scope="module")
def client():
    return TestClient(api_server.app)


@pytest.mark.parametrize("path", [
    "/sitemap.xml", "/robots.txt", "/health", "/pricing",
    "/.well-known/x402.json", "/llms.txt",
])
def test_head_matches_get_status_and_headers(client, path):
    g = client.get(path)
    h = client.head(path)
    assert g.status_code == 200, f"GET {path} -> {g.status_code} (route missing?)"
    assert h.status_code == 200, f"HEAD {path} -> {h.status_code}"
    assert h.headers.get("content-type") == g.headers.get("content-type")
    assert h.headers.get("content-length") == g.headers.get("content-length")


def test_head_returns_no_body(client):
    h = client.head("/sitemap.xml")
    assert h.content == b""


def test_post_still_routes_normally(client):
    # The middleware must not swallow non-HEAD methods.
    r = client.post("/v1/track", json={})
    assert r.status_code in (400, 401, 403, 422)
