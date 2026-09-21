#!/usr/bin/env python3
"""Every indexable page must declare its canonical address (2026-09-21).

WHY THIS EXISTS
---------------
`aiagentscity.com` is reachable on TWO hosts that serve BYTE-IDENTICAL HTML: the
public domain and the raw Railway origin behind it. Verified 2026-09-21 — all 10
probed pages hashed identically on both hosts.

    aiagentscity.com/                    sha256 bd8f2a1f7d4b...
    agent-ledger-production-0ff8.up.railway.app/  sha256 bd8f2a1f7d4b...

With identical content on two hosts and NO rel=canonical, a crawler has nothing
telling it which address is the real one. Before this fix 11 of 12 indexable
pages emitted no canonical at all — only `/benefits/`, served by a *separate*
service, had one. Branded search for the product returned the Railway subdomain
rather than the domain, which is what this class of omission produces.

The fix is a single middleware, not a per-shell edit, because the shells drift
independently: a page added later would silently ship without a canonical. One
place means no page under this app can be missing one.

WHAT THIS PINS
--------------
1. A canonical is present on every HTML GET route that returns 200.
2. It names the PUBLIC origin, never the railway origin. A canonical that points
   at the railway host is worse than none: it tells Google the subdomain is
   authoritative. Asserted on the exact host, not on mere presence.
3. It names the page's OWN path — a copy-pasted canonical pointing every page at
   `/` would collapse the whole site into one indexed URL. Presence alone does
   not catch that.
4. Non-HTML surfaces stay untouched (negative control): adding a canonical to
   /stats or /sitemap.xml would corrupt a machine-readable document.

The canary: patch `PUBLIC_ORIGIN` to the railway host and test 2 must go RED, and
strip the middleware and test 1 must go RED. A guard that passes both ways pins
nothing.
"""
import importlib
import os
import re
import sys

import pytest
from fastapi.testclient import TestClient

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# The public origin these pages must claim. Deliberately NOT read from
# api_server.PUBLIC_ORIGIN: a test that reads the value it is checking agrees
# with a broken deployment by construction.
PUBLIC_HOST = "aiagentscity.com"
FORBIDDEN_HOST = "railway.app"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LEDGER_DATA", str(tmp_path))
    monkeypatch.setenv("AL_ADMIN_SECRET", "test-admin")
    monkeypatch.delenv("AL_PUBLIC_ORIGIN", raising=False)
    import ledger_engine
    import workspace_engine
    import api_server
    for m in (ledger_engine, workspace_engine, api_server):
        importlib.reload(m)
    return TestClient(api_server.app)


def _html_get_paths() -> list:
    """Every registered GET route that serves HTML — discovered, not listed.

    A hand-maintained list rots silently; this fails closed on the routes that
    actually exist.
    """
    import api_server
    out = []
    for route in api_server.app.routes:
        path = getattr(route, "path", None)
        if not path or "{" in path:
            continue
        methods = getattr(route, "methods", None) or set()
        if "GET" not in methods:
            continue
        out.append(path)
    return sorted(set(out))


CANONICAL_RE = re.compile(r'<link\s+rel="canonical"\s+href="([^"]+)"')


def test_every_html_page_declares_a_canonical(client):
    """No indexable page ships without one. This is the headline guard."""
    import api_server
    missing, checked = [], 0
    for path in _html_get_paths():
        r = client.get(path)
        if r.status_code != 200:
            continue
        if "text/html" not in r.headers.get("content-type", ""):
            continue
        checked += 1
        if not CANONICAL_RE.search(r.text):
            missing.append(path)
    assert checked >= 15, f"only {checked} HTML pages found — discovery is broken"
    assert not missing, (
        f"{len(missing)} indexable page(s) emit no rel=canonical: {missing}. "
        "Two hosts serve these pages byte-identically, so without a canonical "
        "the index can attribute them to the railway origin."
    )


def test_canonical_names_the_public_host_never_the_railway_origin(client):
    """A canonical pointing at the railway host is worse than none at all."""
    offenders = []
    for path in _html_get_paths():
        r = client.get(path)
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            continue
        m = CANONICAL_RE.search(r.text)
        if not m:
            continue
        url = m.group(1)
        if FORBIDDEN_HOST in url or PUBLIC_HOST not in url:
            offenders.append((path, url))
    assert not offenders, (
        f"canonical names the wrong origin: {offenders}. The canonical must name "
        f"{PUBLIC_HOST}; naming the railway origin tells Google the subdomain is "
        "authoritative, which is the defect this fix exists to remove."
    )


def test_canonical_names_the_pages_own_path(client):
    """Catches the copy-paste bug: every page claiming to be the homepage."""
    wrong = []
    for path in _html_get_paths():
        r = client.get(path)
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            continue
        m = CANONICAL_RE.search(r.text)
        if not m:
            continue
        got = m.group(1).rstrip("/").split(PUBLIC_HOST, 1)[-1] or "/"
        want = path.rstrip("/") or "/"
        if got != want:
            wrong.append((path, m.group(1)))
    assert not wrong, (
        f"canonical path does not match the page: {wrong}. Every page pointing "
        "at one URL collapses the site into a single indexed address."
    )


def test_the_private_credential_page_gets_noindex_and_never_a_canonical(client):
    """A credential-entry page must not advertise a public URL.

    `/dashboard` is where a user pastes a workspace key, and
    test_workspace_dashboard.py::test_the_dashboard_forbids_every_external_resource
    asserts no `https://` appears in its bytes at all — so a canonical URL
    injected here is a security regression, not a cosmetic one. That guard went
    RED on 2026-09-21 when this middleware was first written without an
    exclusion for this path; this test pins the exclusion so it cannot return.

    `/v1/dashboard` is deliberately NOT tested here: it is auth-gated, so a
    crawler gets 401 JSON rather than HTML and the question does not arise.
    """
    r = client.get("/dashboard")
    assert r.status_code == 200, f"/dashboard -> {r.status_code}"
    assert "text/html" in r.headers.get("content-type", "")
    assert "noindex" in r.headers.get("x-robots-tag", ""), \
        "/dashboard must be served with an x-robots-tag: noindex"
    assert not CANONICAL_RE.search(r.text), \
        "/dashboard must carry no canonical — it is private, and the page must " \
        "not reference an external origin at all"
    assert "https://" not in r.text, \
        "/dashboard leaked an absolute URL into a page that handles credentials"


@pytest.mark.parametrize("path", ["/stats", "/sitemap.xml", "/robots.txt",
                                  "/llms.txt", "/openapi.json"])
def test_machine_readable_surfaces_are_not_touched(client, path):
    """Negative control — a canonical in a JSON/XML/text body corrupts it."""
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    assert "text/html" not in r.headers.get("content-type", ""), \
        f"{path} must not be served as HTML"
    assert b'rel="canonical"' not in r.content, \
        f"{path} had a canonical injected into a machine-readable document"


def test_the_fix_is_actually_reachable_from_config(client, monkeypatch):
    """The origin is read from config, not hardcoded per page.

    A literal per-page copy goes stale the moment the deployment changes — the
    same drift rule this file already applies to X402_NETWORK.
    """
    import api_server
    assert hasattr(api_server, "PUBLIC_ORIGIN"), "PUBLIC_ORIGIN is gone"
    assert api_server.PUBLIC_ORIGIN.startswith("https://")
    assert FORBIDDEN_HOST not in api_server.PUBLIC_ORIGIN


# ── sitemap coverage ─────────────────────────────────────────────────────────
# The sitemap list is hand-maintained (a deliberate act, D-1314), so nothing
# noticed when seven real pages went missing from it. This makes an omission a
# RED test instead of a quiet zero — the same fix applied to REACH_PATHS.
def test_every_sitemap_entry_resolves(client):
    """A sitemap that lists a 404 wastes the crawler's budget on nothing."""
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert "application/xml" in r.headers.get("content-type", "")
    locs = re.findall(r"<loc>([^<]+)</loc>", r.text)
    assert locs, "sitemap listed no URLs at all"
    broken = []
    for loc in locs:
        path = loc.split(PUBLIC_HOST, 1)[-1] or "/"
        resp = client.get(path)
        if resp.status_code != 200:
            broken.append((path, resp.status_code))
    assert not broken, f"sitemap lists URLs that do not resolve: {broken}"


def test_indexable_pages_are_not_missing_from_the_sitemap(client):
    """The pages that carry the pitch and the price must be discoverable.

    Not every HTML route belongs in a sitemap — legal boilerplate and the
    credential page do not. But a crawler that reads only the sitemap must still
    find every page we would want a stranger to land on.
    """
    r = client.get("/sitemap.xml")
    listed = {u.split(PUBLIC_HOST, 1)[-1] for u in re.findall(r"<loc>([^<]+)</loc>", r.text)}
    must_be_listed = {
        "/", "/products", "/agent-ledger", "/pricing", "/upgrade", "/demo",
        "/start", "/quickstart", "/developers", "/compare", "/changelog",
        "/manifesto", "/status", "/docs",
        # satellite product landing pages — each is a real destination
        "/perimeter-watch", "/agent-watch", "/cited", "/trust-scan",
    }
    missing = sorted(must_be_listed - listed)
    assert not missing, (
        f"{len(missing)} indexable page(s) absent from the sitemap: {missing}. "
        "A crawler reading only the sitemap never learns these exist."
    )


def test_the_private_page_is_not_in_the_sitemap(client):
    """Negative control — /dashboard handles credentials and must not be advertised."""
    r = client.get("/sitemap.xml")
    assert "/dashboard" not in r.text


# ── duplicate-host handling ──────────────────────────────────────────────────
# Both hosts served byte-identical HTML with no canonical, so the index could
# pick the railway subdomain as authoritative. Now the railway host redirects,
# so there is exactly one address for every page.
RAILWAY_HOST = "agent-ledger-production-0ff8.up.railway.app"


@pytest.mark.parametrize("path", ["/", "/pricing", "/agent-ledger",
                                  "/sitemap.xml", "/llms.txt"])
def test_the_railway_host_redirects_to_the_public_origin(client, path):
    r = client.get(path, headers={"host": RAILWAY_HOST}, follow_redirects=False)
    assert r.status_code == 308, f"{path} -> {r.status_code}, expected a 308"
    loc = r.headers.get("location", "")
    assert loc.startswith(f"https://{PUBLIC_HOST}"), \
        f"{path} redirected to {loc!r}, not the public origin"
    assert RAILWAY_HOST not in loc


def test_the_redirect_preserves_the_query_string(client):
    """A shared link with a ref parameter must survive the hop."""
    r = client.get("/pricing?ref=newsletter", headers={"host": RAILWAY_HOST},
                   follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"].endswith("/pricing?ref=newsletter")


@pytest.mark.parametrize("path", ["/", "/pricing", "/sitemap.xml"])
def test_the_public_host_never_redirects_itself(client, path):
    """Negative control — a redirect loop would take the whole site down."""
    r = client.get(path, headers={"host": PUBLIC_HOST}, follow_redirects=False)
    assert r.status_code == 200, f"{path} -> {r.status_code} on its own host"


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1",
                                  "preview-abc.up.railway.app"])
def test_an_unrelated_host_is_not_redirected(client, host):
    """Only the known duplicate origin is caught — not every other host.

    A blanket `host != PUBLIC_HOST -> redirect` rule would break local dev,
    platform health checks, and every Railway preview deployment.
    """
    r = client.get("/", headers={"host": host}, follow_redirects=False)
    assert r.status_code == 200, f"{host} was redirected; it should not be"


def test_the_host_match_ignores_case_and_port(client):
    r = client.get("/pricing", headers={"host": RAILWAY_HOST.upper()},
                   follow_redirects=False)
    assert r.status_code == 308
    r = client.get("/pricing", headers={"host": RAILWAY_HOST + ":8080"},
                   follow_redirects=False)
    assert r.status_code == 308


# ── IndexNow key file ────────────────────────────────────────────────────────
def test_the_indexnow_key_file_is_served(client):
    """IndexNow rejects every submission (422) without this proof of ownership."""
    import api_server
    r = client.get("/indexnow.txt")
    assert r.status_code == 200
    assert r.text.strip() == api_server.INDEXNOW_KEY


def test_the_indexnow_route_did_not_shadow_a_real_surface(client):
    """A `/{param}.txt` route shadowed /llms.txt when first written.

    FastAPI resolves in registration order, so a catch-all .txt route added
    before an existing .txt route silently takes it over. This pins that every
    real .txt surface still answers as itself.
    """
    for path in ["/llms.txt", "/robots.txt"]:
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        import api_server
        assert r.text.strip() != api_server.INDEXNOW_KEY, \
            f"{path} is being served the IndexNow key — a route shadowed it"


def test_robots_txt_disallows_the_private_surfaces(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    body = r.text
    assert "/dashboard" in body, "robots.txt does not disallow the credential page"
    assert "Sitemap:" in body, "robots.txt does not point at the sitemap"
