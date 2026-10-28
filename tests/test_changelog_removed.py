# tests/test_changelog_removed.py
"""D-4058 — the public release log is gone.

The /changelog page published internal engineering notes: that the only x402
payer ever measured was an indexer sweeping cheap endpoints, that the human
paid door had been retired (410 endpoint_retired), and a list of red-team
defects. Those are internal facts, not marketing.

Three invariants keep it off the site:

1. No nav, footer, homepage block or About link points at it.
2. The route answers 301 to / and serves no content.
3. It is not in PAGES_META, so the sitemap and the markdown twin skip it.
"""
import importlib
import os
import sys

import pytest
from fastapi.testclient import TestClient

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

LINK = 'href="/changelog"'
SURFACES = ("/", "/about", "/developers", "/products", "/compare", "/manifesto")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LEDGER_DATA", str(tmp_path))
    monkeypatch.setenv("AL_ADMIN_SECRET", "test-admin")
    import ledger_engine
    import workspace_engine
    import api_server
    for m in (ledger_engine, workspace_engine, api_server):
        importlib.reload(m)
    return TestClient(api_server.app)


@pytest.mark.parametrize("path", SURFACES)
def test_no_surface_links_to_the_release_log(client, path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    assert LINK not in r.text, f"{path} still links to /changelog"


def test_route_redirects_and_serves_no_content(client):
    r = client.get("/changelog", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/"
    assert "Shipping fast" not in r.text


def test_page_is_not_registered():
    import city_site
    assert "/changelog" not in city_site.PAGES_META
    assert "/changelog" not in [p for p, _label, _style in city_site.TABS]
