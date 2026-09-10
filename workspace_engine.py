#!/usr/bin/env python3
"""Workspace identity & billing state — the per-customer layer sitting
above the existing per-agent ledger. Flat-file storage (same pattern as
agent secrets/ledgers today), scoped under DATA_DIR/workspaces/. See
docs/superpowers/specs/2026-09-10-workspace-identity-design.md.
"""
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

DATA_DIR = Path(os.environ.get("AGENT_LEDGER_DATA", os.path.expanduser("~/.agent-ledger")))
WORKSPACE_SCARCITY_CAP = 50
WORKSPACE_FREE_AGENT_CAP = 3


class WorkspaceError(Exception):
    """Raised for invalid workspace lookups callers must handle explicitly."""


def _workspaces_dir() -> Path:
    d = DATA_DIR / "workspaces"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ws_path(workspace_id: str) -> Path:
    return _workspaces_dir() / f"{workspace_id}.json"


def _index_dir(name: str) -> Path:
    d = _workspaces_dir() / f"index_{name}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def workspace_count() -> int:
    return sum(1 for f in _workspaces_dir().glob("*.json"))


def _write_workspace(record: dict) -> None:
    _ws_path(record["workspace_id"]).write_text(json.dumps(record, indent=2))


def get_workspace(workspace_id: str) -> Optional[dict]:
    p = _ws_path(workspace_id)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def _lookup_by_index(index_name: str, key: str) -> Optional[dict]:
    idx_file = _index_dir(index_name) / _hash(key)
    if not idx_file.exists():
        return None
    return get_workspace(idx_file.read_text().strip())


def get_workspace_by_key(raw_key: str) -> Optional[dict]:
    return _lookup_by_index("by_key_hash", raw_key)


def get_workspace_by_google_sub(google_sub: str) -> Optional[dict]:
    return _lookup_by_index("by_google_sub", google_sub)


def get_workspace_by_wallet(wallet_address: str) -> Optional[dict]:
    return _lookup_by_index("by_wallet", wallet_address)


def create_workspace(*, owner_email: Optional[str] = None,
                      google_sub: Optional[str] = None,
                      wallet_address: Optional[str] = None) -> tuple[str, str]:
    """Idempotent per identity dimension: calling again with the same
    google_sub or wallet_address returns the SAME workspace (no duplicate
    minted), with a freshly generated key each time (old key invalidated —
    acceptable for now since there's no rotation UI yet; a repeat call is
    effectively a "lost my key" recovery path)."""
    if google_sub:
        existing = get_workspace_by_google_sub(google_sub)
        if existing:
            return existing["workspace_id"], _reissue_key(existing["workspace_id"], "by_google_sub", google_sub)
    if wallet_address:
        existing = get_workspace_by_wallet(wallet_address)
        if existing:
            return existing["workspace_id"], _reissue_key(existing["workspace_id"], "by_wallet", wallet_address)

    workspace_id = "ws_" + secrets.token_urlsafe(16)
    raw_key = "wk_live_" + secrets.token_urlsafe(32)
    pre_count = workspace_count()
    is_scarcity = pre_count < WORKSPACE_SCARCITY_CAP
    record = {
        "workspace_id": workspace_id,
        "owner_email": owner_email,
        "google_sub": google_sub,
        "wallet_address": wallet_address,
        "workspace_key_hash": _hash(raw_key),
        "stripe_customer_id": None,
        "plan": "pro" if is_scarcity else "free",
        "agent_cap": None if is_scarcity else WORKSPACE_FREE_AGENT_CAP,
        "created_at": time.time(),
        "pro_scarcity": is_scarcity,
    }
    _write_workspace(record)
    (_index_dir("by_key_hash") / _hash(raw_key)).write_text(workspace_id)
    if google_sub:
        (_index_dir("by_google_sub") / _hash(google_sub)).write_text(workspace_id)
    if wallet_address:
        (_index_dir("by_wallet") / _hash(wallet_address)).write_text(workspace_id)
    return workspace_id, raw_key


def _reissue_key(workspace_id: str, index_name: str, identity_value: str) -> str:
    raw_key = "wk_live_" + secrets.token_urlsafe(32)
    record = get_workspace(workspace_id)
    record["workspace_key_hash"] = _hash(raw_key)
    _write_workspace(record)
    (_index_dir("by_key_hash") / _hash(raw_key)).write_text(workspace_id)
    return raw_key


def mark_pro(workspace_id: str, stripe_customer_id: str) -> None:
    record = get_workspace(workspace_id)
    if record is None:
        raise WorkspaceError(f"workspace not found: {workspace_id}")
    record["plan"] = "pro"
    record["agent_cap"] = None
    record["stripe_customer_id"] = stripe_customer_id
    _write_workspace(record)


def is_workspace_pro(workspace_id: str) -> bool:
    record = get_workspace(workspace_id)
    return bool(record and record["plan"] == "pro")
