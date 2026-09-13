# tests/test_share_links.py
"""D-1217 — shareable report links.

The report page is the product's only human-facing surface, and a browser
cannot send the `X-Agent-Secret` header it required. So "share this link with
the agent's owner" — which the API docs advertised — returned raw JSON to
anyone who clicked it. These tests pin the fix and, just as importantly, pin
how narrow the token is: read-only, one agent, expiring, revocable.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

AL_VERSION = "2026-09-01"
AGENT = "share-me"
OTHER = "other-agent"


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, workspace_engine, identity, share_tokens, routes_agents, api_server
    for m in (ledger_engine, workspace_engine, identity, share_tokens,
              routes_agents, api_server):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    tc = TestClient(api_server.app)
    _, key = workspace_engine.create_workspace(owner_email="a@example.com")
    r = tc.post("/v1/track", headers={"AL-API-Version": AL_VERSION},
                json={"agent_id": AGENT, "rail": "api_key", "amount_cents": 100,
                      "service": "openai", "workspace_key": key})
    assert r.status_code == 200, r.text
    secret = r.json()["agent_secret"]
    # a second agent in the same workspace, to prove scope
    r2 = tc.post("/v1/track", headers={"AL-API-Version": AL_VERSION},
                 json={"agent_id": OTHER, "rail": "api_key", "amount_cents": 500,
                       "service": "openai", "agent_secret": None, "workspace_key": key})
    yield tc, key, secret
    shutil.rmtree(tmp, ignore_errors=True)


def _share_url(tc, agent=AGENT, **hdr_and_params):
    headers = hdr_and_params.pop("headers", None)
    r = tc.post(f"/v1/report/{agent}/share", headers=headers or {"X-Workspace-Key": "unset"},
                params=hdr_and_params or None)
    return r


def _mint(tc, key, agent=AGENT, ttl=None):
    params = {"ttl_days": ttl} if ttl else None
    r = tc.post(f"/v1/report/{agent}/share",
                headers={"X-Workspace-Key": key}, params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ── the acceptance criterion: a plain browser can open it ──────────────────

def test_share_url_renders_html_with_no_headers_at_all(env):
    """The whole point. A clean session, no credentials, real HTML with real
    numbers — this is the test that was impossible before D-1217."""
    tc, key, _ = env
    body = _mint(tc, key)
    parsed = urlparse(body["url"])
    assert parsed.path == f"/v1/report/{AGENT}/html"
    token = parse_qs(parsed.query)["t"][0]

    r = tc.get(parsed.path, params={"t": token})   # note: no headers
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert AGENT in r.text
    assert "$1.00" in r.text          # the minted spend is on the page
    assert "{" != r.text.strip()[0]   # not the JSON error body


def test_the_header_path_still_works_unchanged(env):
    tc, key, secret = env
    assert tc.get(f"/v1/report/{AGENT}/html",
                  headers={"X-Agent-Secret": secret}).status_code == 200
    assert tc.get(f"/v1/report/{AGENT}/html").status_code == 401


def test_report_page_when_shared_shows_the_same_page_as_the_header_path(env):
    tc, key, secret = env
    body = _mint(tc, key)
    token = parse_qs(urlparse(body["url"]).query)["t"][0]
    by_header = tc.get(f"/v1/report/{AGENT}/html", headers={"X-Agent-Secret": secret}).text
    by_token = tc.get(f"/v1/report/{AGENT}/html", params={"t": token}).text
    assert by_header == by_token


# ── it must fail closed, and fail like a web page ──────────────────────────

def test_a_mutated_token_gets_a_friendly_html_error_not_json(env):
    tc, key, _ = env
    token = parse_qs(urlparse(_mint(tc, key)["url"]).query)["t"][0]
    broken = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    r = tc.get(f"/v1/report/{AGENT}/html", params={"t": broken})
    assert r.status_code == 403
    assert r.headers["content-type"].startswith("text/html")
    assert "not valid" in r.text
    assert '"error"' not in r.text


def test_an_expired_token_is_rejected(env, monkeypatch):
    tc, key, _ = env
    import share_tokens
    token = parse_qs(urlparse(_mint(tc, key, ttl=1)["url"]).query)["t"][0]
    future = __import__("time").time() + 2 * 86400
    monkeypatch.setattr(share_tokens.time, "time", lambda: future)
    r = tc.get(f"/v1/report/{AGENT}/html", params={"t": token})
    assert r.status_code == 403
    assert "expired" in r.text.lower()


def test_revoke_kills_every_outstanding_link(env):
    tc, key, _ = env
    token = parse_qs(urlparse(_mint(tc, key)["url"]).query)["t"][0]
    assert tc.get(f"/v1/report/{AGENT}/html", params={"t": token}).status_code == 200
    rv = tc.post(f"/v1/report/{AGENT}/share/revoke", headers={"X-Workspace-Key": key})
    assert rv.status_code == 200 and rv.json()["revoked"] is True
    r = tc.get(f"/v1/report/{AGENT}/html", params={"t": token})
    assert r.status_code == 403
    assert "revoked" in r.text.lower()


# ── the token is narrow ────────────────────────────────────────────────────

def test_a_link_for_one_agent_cannot_open_another_agents_report(env):
    tc, key, _ = env
    token = parse_qs(urlparse(_mint(tc, key, agent=AGENT)["url"]).query)["t"][0]
    r = tc.get(f"/v1/report/{OTHER}/html", params={"t": token})
    assert r.status_code == 403
    assert "$5.00" not in r.text          # never leaks the other agent's numbers


def test_a_share_token_is_not_a_write_credential(env):
    tc, key, _ = env
    token = parse_qs(urlparse(_mint(tc, key)["url"]).query)["t"][0]
    r = tc.post("/v1/track", headers={"AL-API-Version": AL_VERSION},
                json={"agent_id": AGENT, "rail": "api_key", "amount_cents": 999,
                      "service": "s", "agent_secret": token})
    assert r.status_code == 401


def test_a_share_token_cannot_rotate_or_revoke_the_agent(env):
    tc, key, _ = env
    token = parse_qs(urlparse(_mint(tc, key)["url"]).query)["t"][0]
    assert tc.post(f"/v1/agents/{AGENT}/rotate-secret",
                   headers={"X-Workspace-Key": token}).status_code == 401


# ── minting itself is gated ────────────────────────────────────────────────

def test_minting_requires_a_credential(env):
    tc, _, _ = env
    assert tc.post(f"/v1/report/{AGENT}/share").status_code == 401
    assert tc.post(f"/v1/report/{OTHER}/share").status_code == 401


def test_minting_works_with_the_agents_own_secret_too(env):
    tc, _, secret = env
    r = tc.post(f"/v1/report/{AGENT}/share", headers={"X-Agent-Secret": secret})
    assert r.status_code == 200


def test_ttl_is_capped(env):
    tc, key, _ = env
    r = tc.post(f"/v1/report/{AGENT}/share",
                headers={"X-Workspace-Key": key}, params={"ttl_days": 500})
    assert r.status_code == 422
    assert "90" in r.text
