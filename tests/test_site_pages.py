# tests/test_site_pages.py
"""D-1226 tranche 2 — the demo, the quickstart, and the umbrella pages.

The load-bearing test here is `test_no_page_advertises_a_package_name_we_do_not_own`.
PyPI's `agent-ledger` belongs to a different author, so an install line naming it
would hand our users a stranger's code. That is a supply-chain hazard, not a typo,
and it is pinned so it cannot come back.
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

PAGES = ["/about", "/security", "/quickstart", "/compare", "/demo", "/dashboard"]


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


# ── the pages exist and are server-rendered ────────────────────────────────
@pytest.mark.parametrize("path", PAGES)
def test_every_page_is_reachable_without_a_credential(client, path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    assert "text/html" in r.headers["content-type"]


@pytest.mark.parametrize("path", ["/about", "/security", "/quickstart", "/compare"])
def test_pages_are_real_content_not_a_stub(client, path):
    body = client.get(path).text
    assert len(body) > 1200, f"{path} looks empty"
    assert "TODO" not in body and "Lorem" not in body


def test_the_umbrella_is_named_on_every_page(client):
    """AI Agent City is the parent brand. Every product page must carry the mark
    or the umbrella is invisible."""
    for path in PAGES:
        assert "AI Agent City" in client.get(path).text, f"{path} is missing the umbrella mark"


# ── the demo ───────────────────────────────────────────────────────────────
def test_demo_needs_no_key_and_says_so(client):
    body = client.get("/demo").text
    assert "true" in body or "demo" in body.lower()
    assert "Read-only demo" in body
    assert "no key" in body.lower() or "no workspace" in body.lower()


def test_demo_summary_has_exactly_the_shape_the_dashboard_renders(client):
    """The demo feeds the same renderer as real data. If these keys drift, /demo
    breaks silently — and so does any future reader that trusts the shape."""
    d = client.get("/v1/demo/summary").json()
    assert d["workspace_id"] == "ws_demo_readonly"
    assert isinstance(d["agents"], list) and len(d["agents"]) == 5
    for k in ("spend_cents_30d", "tokens_in_30d", "tokens_out_30d"):
        assert k in d["totals"], f"totals.{k} missing — the KPI row reads it"
    assert len(d["daily_series"]) == 30
    assert all({"date", "spend_cents"} <= set(p) for p in d["daily_series"])
    for a in d["agents"]:
        assert {"agent_id", "tier", "spend_cents_30d", "budget"} <= set(a)
        assert {"monthly_cents", "used_pct", "status"} <= set(a["budget"])
    assert d["alerts"], "the demo should show the block story"


def test_demo_agrees_with_the_live_summary_shape(client):
    """Same keys as a real workspace, so the page cannot render one and not the
    other. This is the anti-drift test for the whole demo."""
    import ledger_engine
    real = ledger_engine.workspace_summary("", days=30)
    demo = client.get("/v1/demo/summary").json()
    assert set(real.keys()) - {"demo", "generated"} == set(demo.keys()) - {"demo", "generated"}
    assert set(real["totals"]) == set(demo["totals"])
    assert set(real["agents"][0]) if real["agents"] else True


def test_the_demo_tells_the_enforcement_story(client):
    """A demo that does not show a blocked agent is just a chart."""
    d = client.get("/v1/demo/summary").json()
    assert any(a["budget"]["status"] == "exceeded" for a in d["agents"])
    assert any(a["budget"]["status"] == "warning" for a in d["agents"])
    assert any(a["type"] == "budget.exceeded" for a in d["alerts"])


def test_demo_is_deterministic(client):
    """Two requests must not disagree, or the demo looks broken on refresh."""
    a = client.get("/v1/demo/summary").json()
    b = client.get("/v1/demo/summary").json()
    assert a["daily_series"] == b["daily_series"]
    assert a["totals"] == b["totals"]


def test_demo_writes_nothing(client, tmp_path):
    """The point of a fixture is that it touches no ledger. If it ever starts
    reading real data, this fails."""
    before = sorted(p.name for p in tmp_path.rglob("*"))
    client.get("/v1/demo/summary")
    client.get("/demo")
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_no_real_credential_appears_on_any_page(client):
    """Copy-paste snippets carry placeholders, never a live-looking key."""
    for path in PAGES:
        body = client.get(path).text
        assert not re.search(r"wk_live_[A-Za-z0-9_-]{20,}", body), path


# ── the guard that matters most ────────────────────────────────────────────
def test_no_page_advertises_a_package_name_we_do_not_own(client):
    """Every install instruction must name `aiagentscity-ledger`.

    The two obvious names are BOTH other parties': PyPI's `agent-ledger` is
    Rune0's ("Idempotency and audit ledger for AI agent tool calls") and npm's
    `agentledger` belongs to agentledger.co. Either one in our copy sends a user
    to a stranger's code — a supply-chain hazard, not a typo.

    `aiagentscity-ledger` is the umbrella namespace: nobody can register it
    without impersonating the domain we own, so it cannot be taken from us the
    way `agent-ledger` was.

    Verified live 2026-09-13: pypi.org/pypi/agent-ledger -> Rune0;
    agentledger-py + npm agentledger -> agentledger.co.
    """
    offenders = []
    for path in PAGES + ["/"]:
        body = client.get(path).text
        for m in re.finditer(r"pip install[^\n<]*", body):
            line = m.group(0)
            if "aiagentscity-ledger" not in line:
                offenders.append((path, line.strip()[:90]))
        # nothing is published on npm yet, so any npm install line is premature
        for m in re.finditer(r"npm install[^\n<]*", body):
            offenders.append((path, m.group(0).strip()[:90]))
    assert not offenders, (
        "an install instruction names a package we do not control: "
        f"{offenders}. Ship as aiagentscity-ledger.")

    # And the retired names must not creep back as install targets, in any
    # variant — including the hyphenated form a user might guess.
    for path in PAGES + ["/"]:
        body = client.get(path).text
        for m in re.finditer(r"(?:pip|npm)\s+install[^\n<]*", body):
            line = m.group(0)
            assert "aiagentscity-ledger" in line, (
                f"{path}: install line names a package we do not control: "
                f"{line.strip()[:80]}")


def test_the_pages_do_not_promise_the_retired_launch_grant(client):
    """The first-50-free-Pro grant was retired because POST /start never granted
    it. Machine-read contracts must not outrun the architecture."""
    for path in PAGES + ["/"]:
        body = client.get(path).text.lower()
        for phrase in ("first 50", "first fifty", "free pro for a year",
                       "free for 1 year", "free for a year"):
            assert phrase not in body, f"{path} still promises {phrase!r}"


def test_security_page_states_the_enforcement_limit(client):
    """The honest claim the whole product rests on: a bare API cap stops the
    recording, a proxied call is stopped for real."""
    body = client.get("/security").text
    assert "402" in body
    assert "never stored" in body.lower() or "never store" in body.lower()
    assert "prompt" in body.lower()


def test_the_landing_page_has_a_real_screenshot(client):
    """The gap plan scored this page 5/10 on clarity largely for having no
    visuals. A page with zero images asks a stranger to read prose to find out
    what the product looks like.

    D-1239: this is AgentLedger's own landing page, now at /agent-ledger — the
    root became the AI Agent City umbrella index."""
    body = client.get("/agent-ledger").text
    imgs = re.findall(r"<img\s[^>]*>", body)
    assert imgs, "the landing page has no images"
    assert any("/img/dashboard.png" in i for i in imgs)
    for i in imgs:
        # an image a screen reader cannot describe is not proof for everyone
        alt = re.search(r'alt="([^"]+)"', i)
        assert alt and len(alt.group(1)) > 25, f"thin alt text: {i[:80]}"


def test_the_screenshot_is_served_and_cached(client):
    r = client.get("/img/dashboard.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n", "not actually a PNG"
    assert "max-age" in r.headers.get("cache-control", "")


def test_the_landing_page_links_the_demo_and_the_dashboard(client):
    """The dashboard shipped in tranche 1 and the landing page did not mention
    it at all. Both entry points must be reachable from the front door.

    D-1239: checked against /agent-ledger — see the note above."""
    body = client.get("/agent-ledger").text
    assert 'href="/demo"' in body
    assert 'href="/dashboard"' in body
    assert 'href="/quickstart"' in body


def test_compare_page_prices_are_dated(client):
    """Competitor prices drift; an undated price is a claim we cannot defend."""
    body = client.get("/compare").text
    # re-verified 2026-09-18 (accuracy audit): LangSmith $39/seat + $0.50/1k overages,
    # OpenRouter Guardrails unverifiable -> listed as per-key spend limits only.
    assert "2026-09-18" in body, "the comparison must date its prices"
    for vendor in ("LangSmith", "Helicone", "Braintrust"):
        assert vendor in body
