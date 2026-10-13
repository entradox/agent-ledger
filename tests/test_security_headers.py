"""D-1664: every served surface carries standard hardening headers."""
import pytest

from api_server import app


@pytest.fixture(scope="module")
def client():
    from starlette.testclient import TestClient
    return TestClient(app)


EXPECTED = {
    "strict-transport-security": {"max-age=31536000; includeSubDomains"},
    "x-content-type-options": {"nosniff"},
    "x-frame-options": {"DENY"},
    # /demo deliberately pins the stricter no-referrer (key-handling page)
    "referrer-policy": {"strict-origin-when-cross-origin", "no-referrer"},
    "permissions-policy": None,  # presence only
}


@pytest.mark.parametrize("path", [
    "/", "/products", "/trust-scan", "/demo", "/developers",
    "/v1/products", "/llms.txt", "/status", "/definitely-not-a-page",
])
def test_security_headers_on_every_surface(client, path):
    r = client.get(path)
    for name, vals in EXPECTED.items():
        got = r.headers.get(name)
        assert got is not None, f"{name} missing on {path}"
        if vals:
            assert got in vals, f"{name} on {path}: {got!r}"


def test_csp_on_html_pages(client):
    for path in ("/", "/products", "/demo"):
        csp = client.get(path).headers.get("content-security-policy", "")
        assert "frame-ancestors 'none'" in csp, path
        assert "default-src" in csp, path
        assert "cdn.jsdelivr.net" not in csp, path


def test_docs_pages_get_cdn_whitelist(client):
    csp = client.get("/docs").headers.get("content-security-policy", "")
    assert "cdn.jsdelivr.net" in csp
    assert "frame-ancestors 'none'" in csp


def test_json_responses_carry_headers_but_no_csp(client):
    r = client.get("/v1/products")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert "content-security-policy" not in r.headers
