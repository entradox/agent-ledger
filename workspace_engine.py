#!/usr/bin/env python3
"""Workspace identity & billing state — the per-customer layer sitting
above the existing per-agent ledger. Flat-file storage (same pattern as
agent secrets/ledgers today), scoped under DATA_DIR/workspaces/. See
docs/superpowers/specs/2026-09-10-workspace-identity-design.md.
"""
import hashlib
import json
import os
import re
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


# Workspace ids are minted as "ws_" + secrets.token_urlsafe(16), whose alphabet
# is [A-Za-z0-9_-]. Anything outside that is not an id we ever issued, so it is
# refused BEFORE it can be used as a filesystem path component. See _ws_path.
_WORKSPACE_ID_RE = re.compile(r"ws_[A-Za-z0-9_-]{1,200}")


def _workspaces_dir() -> Path:
    d = DATA_DIR / "workspaces"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ws_path(workspace_id: str) -> Path:
    """Path to a workspace record, refusing ids that are not ids.

    `_workspaces_dir() / f"{workspace_id}.json"` is unsafe on untrusted input:
    pathlib.__truediv__ DISCARDS the left operand when the right side is
    absolute, so `/data/workspaces` / "/tmp/x.json" is just `/tmp/x.json`. A
    workspace_id arrives from a payer-controlled query parameter
    (client_reference_id, capped at 200 chars by Stripe — far more than a path
    needs), so an absolute or traversing value escaped the workspaces
    directory and let mark_pro() rewrite an arbitrary JSON file.

    Workspace ids are minted as "ws_" + token_urlsafe(16), so the character set
    is known exactly: validate against it and refuse everything else. This is
    the single choke point — every read and write goes through here, so
    validating once covers the whole engine.
    """
    wid = str(workspace_id)
    if not _WORKSPACE_ID_RE.fullmatch(wid):
        raise WorkspaceError(f"invalid workspace id: {wid[:60]!r}")
    return _workspaces_dir() / f"{wid}.json"


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


def mark_pro(workspace_id: str, stripe_customer_id: str,
             stripe_subscription_id: str = "",
             pro_until: Optional[float] = None,
             period_source: str = "") -> None:
    """Grant Pro, optionally with the paid period's end.

    `pro_until` is the load-bearing argument for D-1269. Originally mark_pro
    set it to None unconditionally, and is_workspace_pro() reads None as
    "never expires" — so ONE $19 payment granted Pro permanently and a
    cancelled or non-paying customer kept it forever. The read path was always
    correct; the bug was never writing a clock.

    `pro_until=None` is still accepted and still means "no expiry", deliberately:
    it is the fail-open case. A caller that cannot determine the period end must
    never expire a paying customer, so unknown stays Pro. Only a caller that
    actually knows the period end passes one.
    """
    record = get_workspace(workspace_id)
    if record is None:
        raise WorkspaceError(f"workspace not found: {workspace_id}")
    record["plan"] = "pro"
    record["agent_cap"] = None
    # A paid subscription supersedes any scarcity grant: write the paid period
    # (or None = no expiry) so a first-50 workspace that later subscribes does
    # not inherit the grant's one-year clock.
    record["pro_until"] = pro_until
    record["stripe_customer_id"] = stripe_customer_id
    if stripe_subscription_id:
        record["stripe_subscription_id"] = stripe_subscription_id
    if period_source:
        record["pro_period_source"] = period_source
        record["pro_period_updated_at"] = time.time()
    _write_workspace(record)
    # Index by customer so a later lifecycle event — which carries ONLY a
    # customer id, no workspace id and often no client_reference_id — can find
    # the workspace it must downgrade. Without this the cancellation arrives and
    # there is no way to act on it.
    if stripe_customer_id:
        (_index_dir("by_stripe_customer") / _hash(stripe_customer_id)).write_text(workspace_id)


def iter_workspaces() -> list:
    """Every workspace record. Used by the subscription lifecycle to resolve a
    Stripe customer to a workspace when the by_stripe_customer index has no
    entry (a subscription created before that index existed).

    Reads the workspaces directory directly and skips anything unreadable: a
    single corrupt file must not make a cancellation event impossible to
    process, which would leave a cancelled customer on Pro forever.
    """
    out = []
    for f in _workspaces_dir().glob("*.json"):
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            continue
    return out


def get_workspace_by_stripe_customer(stripe_customer_id: str) -> Optional[dict]:
    return _lookup_by_index("by_stripe_customer", stripe_customer_id)


def set_pro_period(workspace_id: str, pro_until, *,
                   source: str = "", reason: str = "") -> None:
    """Move an existing Pro subscription's paid-period end.

    Used by a renewal (extend), by a cancellation (set to the period end so the
    customer keeps what they paid for), and by a payment failure (set the grace
    deadline). Idempotent: writing the same deadline twice is a no-op in effect.

    `pro_until` may be None, meaning NO EXPIRY. That is the fail-open path: a
    caller that cannot determine a period end must not guess one, because a
    guessed deadline in the past would silently downgrade a paying customer.
    """
    record = get_workspace(workspace_id)
    if record is None:
        raise WorkspaceError(f"workspace not found: {workspace_id}")
    record["pro_until"] = pro_until
    if source:
        record["pro_period_source"] = source
    if reason:
        record["pro_period_reason"] = reason
    record["pro_period_updated_at"] = time.time()
    _write_workspace(record)


def revoke_pro(workspace_id: str, *, reason: str = "", source: str = "") -> None:
    """Drop a workspace back to the free tier, preserving the audit trail.

    Deliberately does NOT touch `stripe_customer_id` — the customer link and the
    by_stripe_customer index must survive, or a customer who re-subscribes would
    become unreachable and a second cancellation could not be matched.
    """
    record = get_workspace(workspace_id)
    if record is None:
        raise WorkspaceError(f"workspace not found: {workspace_id}")
    record["plan"] = "free"
    record["agent_cap"] = WORKSPACE_FREE_AGENT_CAP
    record["pro_until"] = None
    record["pro_scarcity"] = False
    if reason:
        record["pro_revoked_reason"] = reason
    if source:
        record["pro_revoked_source"] = source
    record["pro_revoked_at"] = time.time()
    _write_workspace(record)


def clear_grace(workspace_id: str) -> None:
    """Remove the payment-failure grace marker after a successful renewal.

    Kept separate from set_pro_period so a caller cannot accidentally clear the
    marker on a workspace that is still in grace. Without this the record would
    keep reading `payment_failed` after the customer paid, which misreports a
    healthy subscriber as lapsed.
    """
    record = get_workspace(workspace_id)
    if record is None:
        raise WorkspaceError(f"workspace not found: {workspace_id}")
    record.pop("pro_period_reason", None)
    _write_workspace(record)


def is_workspace_pro(workspace_id: str) -> bool:
    """Pro via one of two routes, checked in this order:

    1. A Stripe subscription (mark_pro) — plan == "pro". If a pro_until is
       present it is the end of the paid period (or, after a failed renewal,
       the end of the grace window); past it the workspace is free.
    2. A scarcity grant — plan == "pro" WITH a pro_until, which expires one
       year after the workspace was created. Without this check a first-50
       workspace would be Pro forever, which is not what was offered.

    `pro_until is None` means NO EXPIRY. This is the fail-open branch and it is
    intentional (D-1269): an unknown period end must keep a paying customer Pro
    rather than silently downgrading them. As of D-1269 every subscription that
    receives a lifecycle event has a real pro_until written, so this branch
    covers legacy records and grants only.
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
