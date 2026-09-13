# tests/test_secret_rotation.py
"""D-1216 — agent-secret recovery.

A lost agent_secret used to brick an agent_id permanently: ensure_agent_secret
raises AuthError for a claimed id whose secret you no longer hold, and the
workspace owner had no path back in. On a free tier of three agents that is a
third of a user's capacity destroyed by an ordinary event.

These tests pin the recovery path — and, just as importantly, pin what it must
NOT allow, because a rotation endpoint that any credential can call is a
takeover primitive rather than a recovery feature.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

AL_VERSION = "2026-09-01"
AGENT = "rotate-me"


@pytest.fixture
def env(monkeypatch):
    """Two workspaces (A owns the agent, B does not) and one claimed agent."""
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, workspace_engine, identity, routes_agents, api_server
    for m in (ledger_engine, workspace_engine, identity, routes_agents, api_server):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    tc = TestClient(api_server.app)
    _, key_a = workspace_engine.create_workspace(owner_email="a@example.com")
    _, key_b = workspace_engine.create_workspace(owner_email="b@example.com")
    r = tc.post("/v1/track", headers={"AL-API-Version": AL_VERSION},
                json={"agent_id": AGENT, "rail": "api_key", "amount_cents": 100,
                      "service": "s", "workspace_key": key_a})
    assert r.status_code == 200, r.text
    old_secret = r.json()["agent_secret"]
    yield tc, key_a, key_b, old_secret
    shutil.rmtree(tmp, ignore_errors=True)


def _write(tc, secret, cents=100):
    return tc.post("/v1/track", headers={"AL-API-Version": AL_VERSION},
                   json={"agent_id": AGENT, "rail": "api_key", "amount_cents": cents,
                         "service": "s", "agent_secret": secret})


# ── the recovery itself ─────────────────────────────────────────────────────

def test_rotate_with_workspace_key_issues_a_working_secret(env):
    tc, key_a, _, old = env
    r = tc.post(f"/v1/agents/{AGENT}/rotate-secret", headers={"X-Workspace-Key": key_a})
    assert r.status_code == 200, r.text
    new = r.json()["agent_secret"]
    assert new and new != old
    assert _write(tc, new).status_code == 200


def test_old_secret_dies_immediately_after_rotation(env):
    tc, key_a, _, old = env
    tc.post(f"/v1/agents/{AGENT}/rotate-secret", headers={"X-Workspace-Key": key_a})
    assert _write(tc, old).status_code == 401


def test_rotation_is_recorded_in_the_audit_trail(env):
    tc, key_a, _, _ = env
    tc.post(f"/v1/agents/{AGENT}/rotate-secret", headers={"X-Workspace-Key": key_a})
    import ledger_engine
    audit = ledger_engine._audit_path(AGENT)
    assert audit.exists()
    last = json.loads(audit.read_text().splitlines()[-1])
    assert last["action"] == "rotate_secret"
    assert last["agent_id"] == AGENT


def test_workspace_key_may_also_travel_in_the_body(env):
    tc, key_a, _, _ = env
    r = tc.post(f"/v1/agents/{AGENT}/rotate-secret", json={"workspace_key": key_a})
    assert r.status_code == 200, r.text
    assert _write(tc, r.json()["agent_secret"]).status_code == 200


# ── what it must NOT allow ──────────────────────────────────────────────────

def test_another_workspace_cannot_rotate_your_agent(env):
    tc, _, key_b, old = env
    r = tc.post(f"/v1/agents/{AGENT}/rotate-secret", headers={"X-Workspace-Key": key_b})
    assert r.status_code == 403
    assert _write(tc, old).status_code == 200  # the real owner is untouched


def test_the_agent_secret_alone_cannot_rotate(env):
    """Otherwise a leaked agent credential locks its real owner out for good."""
    tc, _, _, old = env
    r = tc.post(f"/v1/agents/{AGENT}/rotate-secret", headers={"X-Agent-Secret": old})
    assert r.status_code == 401


def test_rotate_never_claims_an_unknown_agent(env):
    tc, key_a, _, _ = env
    r = tc.post("/v1/agents/never-seen/rotate-secret", headers={"X-Workspace-Key": key_a})
    assert r.status_code == 404


def test_rotate_rejects_a_malformed_agent_id(env):
    tc, key_a, _, _ = env
    r = tc.post("/v1/agents/..%2Fescape/rotate-secret", headers={"X-Workspace-Key": key_a})
    assert r.status_code in (404, 422)


# ── revocation ──────────────────────────────────────────────────────────────

def test_revoke_kills_the_secret_but_keeps_the_ledger(env):
    tc, key_a, _, old = env
    r = tc.post(f"/v1/agents/{AGENT}/revoke-secret", headers={"X-Workspace-Key": key_a})
    assert r.status_code == 200 and r.json()["revoked"] is True
    assert _write(tc, old).status_code == 401
    rep = tc.get(f"/v1/report/{AGENT}", headers={"X-Workspace-Key": key_a})
    assert rep.status_code == 200
    assert rep.json()["entry_count"] == 1


def test_revoked_agent_cannot_be_hijacked_by_another_workspace(env):
    """The reason revoke OVERWRITES the secret file instead of unlinking it.

    agent_exists() is "a secret file is present". If revoke unlinked it, the id
    would look unclaimed and any valid workspace_key could claim it — inheriting
    this ledger. It must still be claimed-and-unwritable instead.
    """
    tc, key_a, _, _ = env
    tc.post(f"/v1/agents/{AGENT}/revoke-secret", headers={"X-Workspace-Key": key_a})
    r = _write(tc, "anything")
    assert r.status_code == 401
    assert "agent_secret_mismatch" in r.text
    import ledger_engine
    assert ledger_engine.agent_exists(AGENT) is True


def test_owner_can_rotate_back_in_after_revoke(env):
    tc, key_a, _, _ = env
    tc.post(f"/v1/agents/{AGENT}/revoke-secret", headers={"X-Workspace-Key": key_a})
    r = tc.post(f"/v1/agents/{AGENT}/rotate-secret", headers={"X-Workspace-Key": key_a})
    assert r.status_code == 200
    assert _write(tc, r.json()["agent_secret"]).status_code == 200


def test_revoke_requires_the_workspace_key(env):
    tc, _, _, old = env
    assert tc.post(f"/v1/agents/{AGENT}/revoke-secret").status_code == 401
    assert tc.post(f"/v1/agents/{AGENT}/revoke-secret",
                   headers={"X-Agent-Secret": old}).status_code == 401


def test_an_unauthenticated_caller_cannot_enumerate_claimed_agents(env):
    """Existence must not be revealed before authentication.

    Checking existence first made the response 404 for an unclaimed agent_id
    and 401 for a claimed one, so anyone could map real tenants by name —
    confirmed live on 2026-09-13, where a credential-less probe of
    'probe-agent' returned agent_not_claimed. Order is now authenticate,
    authorize, then reveal.
    """
    tc, _, _, _ = env
    r = tc.post("/v1/agents/totally-unknown-agent/rotate-secret")
    assert r.status_code == 401
    assert "workspace_key_required" in r.text
    # ...and the same for an agent that DOES exist: identical answer, no signal.
    r2 = tc.post(f"/v1/agents/{AGENT}/rotate-secret")
    assert r2.status_code == 401
    assert r2.text == r.text
