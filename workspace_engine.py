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
# Reused from ledger_engine, where the retired per-agent scarcity grant used
# the same one-year duration — imported rather than re-literal'd so the two
# can never drift apart.
from ledger_engine import SCARCITY_PRO_DURATION_SECONDS  # noqa: E402


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
                      wallet_address: Optional[str] = None,
                      grant_scarcity: bool = True
                      ) -> tuple[str, Optional[str]]:
    """Idempotent per identity dimension: calling again with the same
    google_sub or wallet_address returns the SAME workspace (no duplicate
    minted) and `None` for the key.

    It returns None rather than a fresh key deliberately. The raw key only
    ever exists at mint time (only its hash is stored — that is the entire
    point), so there is nothing to hand back on a lookup. The earlier
    behavior — silently reissuing, and thereby INVALIDATING, the key on
    every call — made an identity lookup a destructive side effect: every
    Google login broke the user's existing key, and every repeat x402
    payment (including a replayed one) did the same. Reissue is now an
    explicit, separately-named action (see reissue_key below), never an
    implicit consequence of "who is this caller".
    """
    if google_sub:
        existing = get_workspace_by_google_sub(google_sub)
        if existing:
            return existing["workspace_id"], None
    if wallet_address:
        existing = get_workspace_by_wallet(wallet_address)
        if existing:
            return existing["workspace_id"], None

    workspace_id = "ws_" + secrets.token_urlsafe(16)
    raw_key = "wk_live_" + secrets.token_urlsafe(32)
    pre_count = workspace_count()
    # grant_scarcity=False is the human self-serve path (POST /start). It exists
    # because the launch-window grant hands over the ENTIRE paid tier — no agent
    # cap, one year, free — so minting every anonymous visitor into it made the
    # $19 upgrade button decorative and put the whole promotion one loop away
    # from anyone who wanted it. A human now gets the free tier the page
    # advertises (3 agents, every feature); Pro is what $19 buys. The agent path
    # (POST /v1/billing/x402) still earns the grant, because paying a wallet is
    # a real commitment signal and not an anonymous HTTP request.
    is_scarcity = grant_scarcity and pre_count < WORKSPACE_SCARCITY_CAP
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
        # The scarcity window is "Pro free for a YEAR", not forever. Mirrors
        # the per-agent pro_until this replaced (ledger_engine's
        # SCARCITY_PRO_DURATION_SECONDS). A Stripe-paid workspace has no
        # pro_until — subscriptions don't expire this way.
        "pro_until": (time.time() + SCARCITY_PRO_DURATION_SECONDS) if is_scarcity else None,
    }
    _write_workspace(record)
    (_index_dir("by_key_hash") / _hash(raw_key)).write_text(workspace_id)
    if google_sub:
        (_index_dir("by_google_sub") / _hash(google_sub)).write_text(workspace_id)
    if wallet_address:
        (_index_dir("by_wallet") / _hash(wallet_address)).write_text(workspace_id)
    return workspace_id, raw_key


def reissue_key(workspace_id: str) -> str:
    """Mint a new workspace_key and INVALIDATE the old one by deleting its
    index entry. Destructive and irreversible for anyone still holding the
    previous key — so it is only ever called where reissue is the caller's
    genuine, disclosed intent.

    As of this phase nothing calls it automatically: there is no "I lost my
    key" recovery flow yet (key rotation/revocation is explicitly out of
    scope for this phase — see the design spec's "Explicitly out of scope").
    It exists as the building block that flow will use, kept separate from
    create_workspace precisely so an identity lookup can never reissue by
    accident.
    """
    record = get_workspace(workspace_id)
    if record is None:
        raise WorkspaceError(f"workspace not found: {workspace_id}")

    # Delete old key hash index entry to invalidate the old key
    old_key_hash = record["workspace_key_hash"]
    old_key_hash_file = _index_dir("by_key_hash") / old_key_hash
    if old_key_hash_file.exists():
        old_key_hash_file.unlink()

    # Generate and store new key
    raw_key = "wk_live_" + secrets.token_urlsafe(32)
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
    # A paid subscription supersedes any scarcity grant: clear the expiry so
    # a first-50 workspace that later subscribes doesn't inherit the grant's
    # one-year clock.
    record["pro_until"] = None
    record["stripe_customer_id"] = stripe_customer_id
    _write_workspace(record)


def is_workspace_pro(workspace_id: str) -> bool:
    """Pro via one of two routes, checked in this order:

    1. A Stripe subscription (mark_pro) — plan == "pro" with no pro_until.
       Subscriptions don't expire on a timer; cancellation is a separate,
       out-of-scope webhook.
    2. A scarcity grant — plan == "pro" WITH a pro_until, which expires one
       year after the workspace was created. Without this check a first-50
       workspace would be Pro forever, which is not what was offered.
    """
    record = get_workspace(workspace_id)
    if not record or record.get("plan") != "pro":
        return False
    pro_until = record.get("pro_until")
    if pro_until is None:
        return True
    return time.time() < pro_until


def effective_agent_cap(record: dict) -> Optional[int]:
    """The agent cap actually in force for a workspace record, right now.

    `agent_cap` is None (unbounded) for Pro workspaces. For a SCARCITY Pro
    workspace that is what the stored field says forever — so the grant's
    one-year expiry has to be applied here, or the expiry would be purely
    cosmetic and a first-50 workspace would keep unlimited agents for life.
    An expired scarcity grant falls back to the free-tier cap.
    """
    if record.get("agent_cap") is not None:
        return record["agent_cap"]
    pro_until = record.get("pro_until")
    if pro_until is not None and time.time() >= pro_until:
        return WORKSPACE_FREE_AGENT_CAP
    return None
