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


def authorize_agent_access(agent_id: str, *, agent_secret: Optional[str] = None,
                            workspace_key: Optional[str] = None) -> bool:
    """True if EITHER credential proves the caller may access agent_id's
    data — used for read authorization (Task 4). Not used for claiming a
    brand-new agent_id (that's ensure_agent_secret's workspace_key
    precondition, a different operation: claiming vs. accessing)."""
    if resolve_agent_secret(agent_id, agent_secret):
        return True
    if workspace_key:
        workspace_id = resolve_workspace_key(workspace_key)
        if workspace_id:
            from ledger_engine import DATA_DIR
            ws_file = DATA_DIR / "agents" / agent_id / "workspace_id.txt"
            if ws_file.exists() and ws_file.read_text().strip() == workspace_id:
                return True
    return False
