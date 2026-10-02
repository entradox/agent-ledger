# tests/test_redteam_fixes.py
"""Regression pins for the red-team review fixes.

- H1: webhook delivery re-validates the target at send time and validates
  every redirect hop (rebinding/302 -> private = refused).
- H2: the budget cap check and the ledger append are atomic — N concurrent
  writes cannot all pass against the same stale total.
- M1: the satellite MCP byte-proxy strips AgentLedger credential headers
  before forwarding to a third-party host.
- M2: client_ip() trusts the RIGHTMOST X-Forwarded-For entry (edge-appended),
  never the attacker-controlled leftmost.
"""
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest


# ── M2: client IP resolution ───────────────────────────────────────────────

class _Req:
    def __init__(self, xff=None, peer="10.0.0.1"):
        self.headers = {}
        if xff is not None:
            self.headers["x-forwarded-for"] = xff

        class _C:
            host = peer
        self.client = _C()


def test_client_ip_takes_the_edge_appended_rightmost_xff(monkeypatch):
    from client_ip import client_ip
    # "spoofed, real" — attacker prepends; the edge appended its own sighting.
    assert client_ip(_Req(xff="1.2.3.4, 203.0.113.9")) == "203.0.113.9"
    assert client_ip(_Req(xff="198.51.100.7")) == "198.51.100.7"


def test_client_ip_falls_back_to_the_tcp_peer(monkeypatch):
    from client_ip import client_ip
    assert client_ip(_Req(peer="9.9.9.9")) == "9.9.9.9"


def test_client_ip_can_ignore_xff_entirely(monkeypatch):
    monkeypatch.setenv("AL_CLIENT_IP_FROM_XFF", "0")
    from client_ip import client_ip
    assert client_ip(_Req(xff="1.2.3.4, 5.6.7.8", peer="10.1.2.3")) == "10.1.2.3"


# ── M1: satellite proxy drops our credentials ──────────────────────────────

def test_satellite_proxy_strips_agentledger_credentials():
    import api_server
    banned = api_server._SAT_HOP_BY_HOP
    for h in (b"x-al-agent", b"x-al-secret", b"x-workspace-key",
              b"x-al-admin", b"al-api-version", b"cookie"):
        assert h in banned, f"{h} would be forwarded to a third party"


# ── H1: delivery-time SSRF re-validation ───────────────────────────────────

@pytest.fixture
def data_dir(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


def test_delivery_revalidates_the_target_when_private_is_not_allowed(data_dir, monkeypatch):
    """Registration-time validation is not enough: DNS answers can change
    between register() and deliver_once() (rebinding)."""
    monkeypatch.delenv("AL_ALLOW_PRIVATE_WEBHOOKS", raising=False)
    import alert_delivery
    entry = {"id": "wh_test", "url": "http://127.0.0.1:9/hook", "events": []}
    with pytest.raises(Exception):
        alert_delivery.deliver_once(entry, {"event": "x", "agent_id": "a",
                                            "message": "m"})


def test_redirect_to_a_private_target_is_refused(data_dir, monkeypatch):
    monkeypatch.delenv("AL_ALLOW_PRIVATE_WEBHOOKS", raising=False)
    import alert_delivery
    handler = alert_delivery._ValidatingRedirectHandler()
    with pytest.raises(Exception):
        handler.redirect_request(req=None, fp=None, code=302, msg="",
                                 headers={},
                                 newurl="http://169.254.169.254/latest/meta-data/")


def test_redirect_to_a_non_http_scheme_is_refused(data_dir, monkeypatch):
    monkeypatch.delenv("AL_ALLOW_PRIVATE_WEBHOOKS", raising=False)
    import alert_delivery
    handler = alert_delivery._ValidatingRedirectHandler()
    with pytest.raises(Exception):
        handler.redirect_request(req=None, fp=None, code=302, msg="",
                                 headers={}, newurl="file:///etc/passwd")


def test_redirect_to_a_public_target_is_followed(data_dir, monkeypatch):
    monkeypatch.delenv("AL_ALLOW_PRIVATE_WEBHOOKS", raising=False)
    import alert_delivery
    handler = alert_delivery._ValidatingRedirectHandler()
    with patch.object(alert_delivery, "_is_public_target", return_value=True), \
         patch.object(alert_delivery.urllib.request.HTTPRedirectHandler,
                      "redirect_request", return_value="newreq"):
        assert handler.redirect_request(None, None, 302, "", {},
                                        "https://example.com/hook") == "newreq"


# ── L3: bounded delivery fan-out ───────────────────────────────────────────

def test_dispatch_drops_under_congestion_instead_of_piling_threads(data_dir, monkeypatch):
    monkeypatch.setenv("AL_ALLOW_PRIVATE_WEBHOOKS", "1")
    import alert_delivery
    # Exhaust every slot so the next dispatch cannot spawn more work.
    for _ in range(32):
        alert_delivery._delivery_slots.acquire(blocking=False)
    try:
        entries = [{"id": f"wh_{i}", "url": "http://127.0.0.1:9/",
                    "events": ["alert.raised"]} for i in range(5)]
        with patch.object(alert_delivery, "load_registry",
                          return_value=entries):
            n = alert_delivery.dispatch("alert.raised", agent_id="a", message="m",
                                        workspace_id="ws_x")
        assert n == 0
    finally:
        for _ in range(32):
            alert_delivery._delivery_slots.release()


# ── H2: atomic cap enforcement under concurrency ───────────────────────────

@pytest.fixture
def le_env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, metrics
    for m in (metrics, ledger_engine):
        importlib.reload(m)
    yield ledger_engine
    shutil.rmtree(tmp, ignore_errors=True)


def test_concurrent_writes_cannot_all_pass_the_cap(le_env):
    """Before the lock, every thread read the same stale spend total, every
    check passed, and the cap was exceeded by N×entry."""
    le = le_env
    le.set_budget("race-agent", 1000)   # $10 monthly cap
    results, errors = [], []

    def spend():
        try:
            le.track("race-agent", "api_key", 400, "svc")
            results.append(1)
        except le.BudgetExceededError:
            errors.append(1)

    threads = [threading.Thread(target=spend) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = le._month_spend("race-agent")
    assert total <= 1000, f"cap exceeded: {total} > 1000"
    assert len(results) <= 2           # 400+400=800 fits; the 3rd must fail
    assert len(results) + len(errors) == 10


def test_proxy_upstream_url_rejects_traversal():
    import proxy
    url = proxy.upstream_url("openai", "../v1/admin")
    assert url.endswith("/__rejected__")
    assert ".." not in url


# ── Activation: the first write must not strand the caller ─────────────────

@pytest.fixture
def api_env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, workspace_engine, identity, metrics as _m
    for m in (_m, workspace_engine, identity, ledger_engine):
        importlib.reload(m)
    import routes_agents
    importlib.reload(routes_agents)
    yield workspace_engine, ledger_engine
    shutil.rmtree(tmp, ignore_errors=True)


def _shrink_default_caps(workspace_engine, workspace_id, monthly=50, daily=0):
    rec = workspace_engine.get_workspace(workspace_id)
    rec["default_budget"] = {"monthly_cents": monthly, "daily_cents": daily}
    (workspace_engine._workspaces_dir() / f"{workspace_id}.json").write_text(
        __import__("json").dumps(rec))


def test_default_caps_no_longer_reject_a_normal_first_write(api_env):
    """Measured live: a fresh workspace's first track 402'd on the $1/day
    default. The new default ($50/mo, no daily) must let it through."""
    ws, le = api_env
    assert ws.DEFAULT_MONTHLY_CAP_CENTS >= 350   # the docs demo a $3.50 write
    assert ws.DEFAULT_DAILY_CAP_CENTS == 0


def test_first_write_over_default_cap_does_not_squat_the_agent_id(api_env):
    """The 402 must happen BEFORE the claim — no secret minted, no slot used."""
    ws, le = api_env
    workspace_id, key = ws.create_workspace()
    _shrink_default_caps(ws, workspace_id, monthly=50, daily=0)

    from fastapi.testclient import TestClient
    import importlib, api_server
    importlib.reload(api_server)
    tc = TestClient(api_server.app)
    r = tc.post("/v1/track", headers={"AL-API-Version": "2026-09-01"},
                json={"agent_id": "squatter-check", "rail": "api_key",
                      "amount_cents": 400, "service": "s",
                      "workspace_key": key})
    assert r.status_code == 402
    assert not le.agent_exists("squatter-check"), \
        "a refused first write must not claim the agent_id"


def test_a_blocked_claim_still_returns_the_minted_secret(api_env):
    """If a claim-time write is blocked anyway (auto-priced, raced), the 402
    must carry the minted secret — otherwise the agent_id is orphaned."""
    ws, le = api_env
    workspace_id, key = ws.create_workspace()
    _shrink_default_caps(ws, workspace_id, monthly=1, daily=0)  # 1 cent cap

    from fastapi.testclient import TestClient
    import importlib, api_server
    importlib.reload(api_server)
    tc = TestClient(api_server.app)
    # model+tokens path prices AFTER claim, so it exercises the post-claim
    # 402 branch — the one that used to strand the secret.
    r = tc.post("/v1/track", headers={"AL-API-Version": "2026-09-01"},
                json={"agent_id": "priced-claim", "rail": "api_key",
                      "service": "s", "model": "gpt-4o",
                      "tokens_in": 100000, "tokens_out": 100000,
                      "workspace_key": key})
    assert r.status_code == 402
    err = r.json()["error"]
    assert err.get("agent_secret"), "402 on a claim must return the secret"
    assert le.agent_exists("priced-claim")
