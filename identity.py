#!/usr/bin/env python3
"""The one place every gate resolves 'who is this caller' through. Claim
gating (ledger_engine.ensure_agent_secret), read authorization
(routes_agents.py), and later session/billing checks (routes_auth.py,
routes_billing.py) all call into this module instead of each
reimplementing agent_secret/workspace_key comparison. See
docs/superpowers/specs/2026-09-10-workspace-identity-design.md section 0.
"""
import secrets as _secrets
from pathlib import Path
from typing import Optional


def resolve_workspace_key(raw_key: Optional[str]) -> Optional[str]:
    """Returns the workspace_id a raw workspace_key resolves to, or None."""
    if not raw_key:
        return None
    import workspace_engine
    workspace = workspace_engine.get_workspace_by_key(raw_key)
    return workspace["workspace_id"] if workspace else None


def resolve_agent_secret(agent_id: str, provided_secret: Optional[str]) -> bool:
    """True if provided_secret matches agent_id's real, persisted secret."""
    if not agent_id or not provided_secret:
        return False
    from ledger_engine import _secret_path
    path = _secret_path(agent_id)
    if not path.exists():
        return False
    return _secrets.compare_digest(provided_secret, path.read_text().strip())


def resolve_session(cookie_value: Optional[str]) -> Optional[str]:
    """Returns the workspace_id a signed session cookie resolves to, or None."""
    if not cookie_value:
        return None
    from session_auth import verify_session
    return verify_session(cookie_value)


def _agent_belongs_to_workspace(agent_id: str, workspace_id: Optional[str]) -> bool:
    """True if agent_id's workspace_id.txt names exactly this workspace."""
    if not workspace_id:
        return False
    from ledger_engine import DATA_DIR
    ws_file = DATA_DIR / "agents" / agent_id / "workspace_id.txt"
    return ws_file.exists() and ws_file.read_text().strip() == workspace_id


def authorize_agent_access(agent_id: str, *, agent_secret: Optional[str] = None,
                            workspace_key: Optional[str] = None,
                            session_cookie: Optional[str] = None) -> bool:
    """True if ANY of the three credentials proves the caller may access
    agent_id's data — used for read authorization (Task 4). Not used for
    claiming a brand-new agent_id (that's ensure_agent_secret's
    workspace_key precondition, a different operation: claiming vs.
    accessing).

    session_cookie is the browser path: the dashboard renders links to
    /v1/report/{agent_id}/html, and a browser following one of those sends
    only its al_session cookie. It resolves to a workspace_id and is then
    checked against the agent's workspace_id.txt exactly as workspace_key
    is — same file, same comparison — so a session never grants access to
    another workspace's agents.
    """
    if resolve_agent_secret(agent_id, agent_secret):
        return True
    if workspace_key and _agent_belongs_to_workspace(
            agent_id, resolve_workspace_key(workspace_key)):
        return True
    if session_cookie:
        try:
            session_workspace = resolve_session(session_cookie)
        except RuntimeError:
            # AL_SESSION_SECRET not configured — an unverifiable cookie is
            # treated as no credential (fail closed), not a 500 on a read.
            session_workspace = None
        if _agent_belongs_to_workspace(agent_id, session_workspace):
            return True
    return False
