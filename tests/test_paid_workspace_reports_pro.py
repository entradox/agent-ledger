# tests/test_paid_workspace_reports_pro.py
"""A workspace that PAID must be told it is Pro — on the surface a buyer reads.

Bug found 2026-09-16 by the first real x402 mainnet payment:

    POST /v1/billing/x402 settled $0.01, minted a workspace, and called
    workspace_engine.mark_pro() — so `effective_agent_cap` became None and the
    write path accepted agents past the free 3-agent cap (verified: agents 2-5
    all claimed). But GET /v1/report/{agent_id} returned
    {"plan": "free", "pro_until": null} for that same workspace, because
    ledger_engine.is_pro() only ever read the per-AGENT pro_until column, which
    the x402 grant path does not write.

The failure mode is the worst kind for a paid product: enforcement and the
customer-facing report disagreed, and the REPORT was the lie. Nothing errored,
no test failed, and the only person who could notice was a paying customer.

These tests assert the OUTCOME a buyer experiences — "does my own report say I
am Pro?" — not that is_pro() was called with particular arguments.
"""
import importlib
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    monkeypatch.delenv("AL_PRO_ACTIVE", raising=False)
    import ledger_engine, workspace_engine, identity, routes_billing, api_server
    for m in (workspace_engine, ledger_engine, identity, routes_billing, api_server):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    yield TestClient(api_server.app), ledger_engine, workspace_engine, identity
    shutil.rmtree(tmp, ignore_errors=True)


def _pay_and_admit(c, workspace_engine):
    """Mint a workspace via the real x402 route and admit one agent into it."""
    workspace_id, raw_key = workspace_engine.create_workspace(
        wallet_address="0xPAYER", grant_scarcity=False)
    # The x402 route's own grant, verbatim (routes_billing.py).
    workspace_engine.mark_pro(workspace_id, "",
                              pro_until=time.time() + 24 * 3600,
                              period_source="x402_pass")
    r = c.post("/v1/track",
               headers={"AL-API-Version": "2026-09-01"},
               json={"agent_id": "paid-agent", "rail": "x402",
                     "amount_cents": 1, "workspace_key": raw_key})
    assert r.status_code == 200, r.text
    return workspace_id, raw_key, r.json()["agent_secret"]


def test_paid_workspace_report_says_pro_not_free(env):
    """THE regression. The agent's own report must not call a paid workspace free."""
    c, ledger_engine, workspace_engine, identity = env
    ws_id, _raw_key, secret = _pay_and_admit(c, workspace_engine)

    r = c.get("/v1/report/paid-agent", headers={"X-Agent-Secret": secret})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["plan"] != "free", (
        "a workspace that paid via x402 reports plan='free' — the enforcement "
        "gate says Pro while the customer-facing report denies it")
    assert body["plan"].startswith("pro")
    assert body["pro_until"] is not None, "a paid pass must carry its expiry clock"


def test_report_plan_agrees_with_enforcement_cap(env):
    """The report and the WRITE GATE must never disagree about the same workspace.

    This is the assertion that would have caught the original bug without any
    knowledge of how Pro is stored: compare the customer-visible plan against
    the field the write path actually enforces.
    """
    c, ledger_engine, workspace_engine, identity = env
    ws_id, _raw_key, secret = _pay_and_admit(c, workspace_engine)

    record = workspace_engine.get_workspace(ws_id)
    enforced_unbounded = workspace_engine.effective_agent_cap(record) is None
    reported_pro = ledger_engine.is_pro("paid-agent")["is_pro"]

    assert enforced_unbounded == reported_pro, (
        f"enforcement says unbounded agents={enforced_unbounded} but the report "
        f"says is_pro={reported_pro} — the two must agree")


def test_paid_workspace_admits_agents_past_the_free_cap(env):
    """End-to-end: what the buyer actually bought. Free caps at 3 agents."""
    c, _le, workspace_engine, _identity = env
    ws_id, raw_key = workspace_engine.create_workspace(
        wallet_address="0xPAYER2", grant_scarcity=False)
    workspace_engine.mark_pro(ws_id, "",
                              pro_until=time.time() + 24 * 3600,
                              period_source="x402_pass")

    claimed = 0
    for i in range(1, 6):  # 5 > the free cap of 3
        r = c.post("/v1/track",
                   headers={"AL-API-Version": "2026-09-01"},
                   json={"agent_id": f"cap-{i}", "rail": "api_key",
                         "amount_cents": 1, "workspace_key": raw_key})
        if r.status_code == 200:
            claimed += 1
    assert claimed > 3, f"only {claimed} agents admitted; Pro did not lift the cap"


def test_free_agent_still_reports_free(env):
    """Guard against the fix over-reaching: an unpaid agent must still be free.

    Without this, a fix that returns Pro for everything would pass every test
    above. The free path is the control.

    `grant_scarcity=False` mirrors POST /start (the human self-serve route) —
    `create_workspace()` DEFAULTS to grant_scarcity=True, which hands the
    launch-window Pro to the first 50 workspaces ever created. Using the default
    here made this test assert on a genuinely-Pro workspace, which is how the
    control caught itself.
    """
    c, ledger_engine, workspace_engine, identity = env
    ws_id, raw_key = workspace_engine.create_workspace(
        wallet_address="0xFREE", grant_scarcity=False)
    r = c.post("/v1/track",
               headers={"AL-API-Version": "2026-09-01"},
               json={"agent_id": "free-agent", "rail": "api_key",
                     "amount_cents": 1, "workspace_key": raw_key})
    assert r.status_code == 200, r.text

    assert ledger_engine.is_pro("free-agent")["plan"] == "free"
    body = c.get("/v1/report/free-agent",
                 headers={"X-Agent-Secret": r.json()["agent_secret"]}).json()
    assert body["plan"] == "free"
    assert body["pro_until"] is None


def test_expired_pass_reports_free(env):
    """A Pro pass that has run out must read as free — the clock is load-bearing."""
    c, ledger_engine, workspace_engine, identity = env
    ws_id, raw_key = workspace_engine.create_workspace(
        wallet_address="0xEXPIRED", grant_scarcity=False)
    workspace_engine.mark_pro(ws_id, "",
                              pro_until=time.time() - 60,  # already expired
                              period_source="x402_pass")
    r = c.post("/v1/track",
               headers={"AL-API-Version": "2026-09-01"},
               json={"agent_id": "expired-agent", "rail": "api_key",
                     "amount_cents": 1, "workspace_key": raw_key})
    assert r.status_code == 200, r.text

    assert ledger_engine.is_pro("expired-agent")["is_pro"] is False, (
        "an expired Pro pass must not report as active")


def test_agent_with_no_workspace_is_free_not_an_error(env):
    """An agent claimed outside any workspace must not break the report."""
    c, ledger_engine, workspace_engine, identity = env
    # No workspace_key -> cannot claim, so write the agent dir directly the way
    # legacy/self-hosted agents exist, then assert the report still renders.
    import os
    from pathlib import Path as _P
    data = _P(os.environ["AGENT_LEDGER_DATA"]) / "agents" / "orphan-agent"
    data.mkdir(parents=True, exist_ok=True)
    (data / "secret.txt").write_text("s3cret")
    (data / "ledger.jsonl").write_text("")

    info = ledger_engine.is_pro("orphan-agent")
    assert info["plan"] == "free", "an agent with no workspace must read free"
    assert info["is_pro"] is False
