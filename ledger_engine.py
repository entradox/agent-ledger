#!/usr/bin/env python3
"""AgentLedger — per-agent spend management engine.

Tracks spending across multiple payment rails (MPP, x402, metered API),
enforces budget caps, detects anomalies, and produces audit trails.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import statistics
import re
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import metrics

DATA_DIR = Path(os.environ.get("AGENT_LEDGER_DATA", os.path.expanduser("~/.agent-ledger")))

# beta: free tier caps total claimed agents site-wide; Pro ($19/mo) unlimited
BETA_AGENT_CAP = 3

# scarcity window (v0.3.1): the first N agent_ids ever claimed get Pro free
# for a year — a one-time launch incentive, distinct from the paid Stripe
# Pro flag (pro_active()) which is site-wide.
SCARCITY_PRO_CAP = 50
SCARCITY_PRO_DURATION_SECONDS = 365 * 24 * 3600
# sanity ceiling on a single entry — blocks fat-finger / abuse-sized amounts
MAX_AMOUNT_CENTS = 10_000_000  # $100,000
# a day's spend above this multiple of the window's daily mean is a spike.
# Named because TWO surfaces depend on it agreeing: report() shows it and
# scan_spending_spikes() alerts on it. When they were separate literals they
# could drift, and an alert that disagrees with the report is worse than none.
SPIKE_MULTIPLIER = 2.5
# payment rails accepted on the ledger — anything else is a typo/abuse vector
VALID_RAILS = frozenset({"mpp", "x402", "api_key", "manual"})

# ── API hardening constants (launch-kit v0.3) ───────────────────────────────
# Single source of truth for the required AL-API-Version header (REST + MCP).
AL_API_VERSION = "2026-09-01"

MAX_IDEMPOTENCY_KEY_LEN = 255
IDEMPOTENCY_TTL_SECONDS = 24 * 3600
# a row with no stored response older than this is treated as an abandoned
# in-flight request (crashed process) rather than a live conflict
IDEMPOTENCY_INFLIGHT_STALE_SECONDS = 60

# typed error envelope: {"error": {"type": str, "message": str, "code": str?, "param": str?}}
ERROR_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "budget_error",
    403: "permission_error",
    404: "not_found_error",
    409: "conflict_error",
    422: "validation_error",
    500: "api_error",
}


def error_envelope(status_code: int, message: str, *, error_type: str = None,
                    code: str = None, param: str = None) -> dict:
    """Build the typed error envelope shared by every REST and MCP error
    response (launch-kit v0.3, single source so both surfaces agree)."""
    err = {"type": error_type or ERROR_TYPE_BY_STATUS.get(status_code, "api_error"),
           "message": message}
    if code:
        err["code"] = code
    if param:
        err["param"] = param
    return {"error": err}


class LedgerError(Exception):
    """Base class for engine-level rejections (translated to HTTP by callers)."""


class AuthError(LedgerError):
    """agent_id is already claimed and the provided secret didn't match."""


class ValidationError(LedgerError):
    """Malformed or out-of-range spend data."""


class BudgetExceededError(LedgerError):
    """This entry would push the agent over its monthly/daily cap — blocked."""


class BetaCapExceededError(LedgerError):
    """Beta agent-slot cap reached."""


class WorkspaceKeyRequiredError(LedgerError):
    """A brand-new agent_id claim did not present a valid workspace_key."""


class IdempotencyKeyTooLongError(ValidationError):
    """Idempotency-Key header exceeds MAX_IDEMPOTENCY_KEY_LEN."""


class IdempotencyConflictError(LedgerError):
    """Same Idempotency-Key is already in flight for this agent_id + op."""


@dataclass
class SpendEntry:
    agent_id: str
    rail: str
    amount_cents: int
    service: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"agent_id": self.agent_id, "rail": self.rail,
                "amount_cents": self.amount_cents, "service": self.service,
                "timestamp": self.timestamp, **self.meta}


@dataclass
class Budget:
    agent_id: str
    monthly_cap_cents: int
    daily_cap_cents: int
    alert_threshold_pct: int = 80
    # token-volume caps (2026-09-10): dollar caps above are a no-op for
    # rail="tokens" rows, since those are always 0-cent bookkeeping — a
    # token-metered agent had no hard-cap mechanism at all until this. 0
    # means "no cap set", same convention as the cent caps.
    monthly_token_cap: int = 0
    daily_token_cap: int = 0

    def to_dict(self) -> dict:
        return {"agent_id": self.agent_id, "monthly_cap_cents": self.monthly_cap_cents,
                "daily_cap_cents": self.daily_cap_cents, "alert_threshold_pct": self.alert_threshold_pct,
                "monthly_token_cap": self.monthly_token_cap, "daily_token_cap": self.daily_token_cap}


@dataclass
class SpendReport:
    agent_id: str
    period: str
    total_spend_cents: int
    by_rail: dict
    by_service: dict
    entry_count: int
    budget_status: dict
    anomalies: list
    plan: str = "free"
    pro_until: Optional[float] = None
    # Per-day spend, oldest first. It was already computed here for anomaly
    # detection and then thrown away; the dashboard and the CSV need the same
    # numbers, and recomputing them elsewhere is how two surfaces start
    # disagreeing about what a day cost.
    daily_series: list = field(default_factory=list)


def _agent_dir(agent_id: str) -> Path:
    return DATA_DIR / "agents" / agent_id


def _ledger_path(agent_id: str) -> Path:
    return _agent_dir(agent_id) / "ledger.jsonl"


def _budget_path(agent_id: str) -> Path:
    return _agent_dir(agent_id) / "budget.json"


def _alerts_path(agent_id: str) -> Path:
    return _agent_dir(agent_id) / "alerts.jsonl"


def _secret_path(agent_id: str) -> Path:
    return _agent_dir(agent_id) / "secret.txt"


def agent_exists(agent_id: str) -> bool:
    """An agent is 'claimed' once it has a minted secret (may predate any spend)."""
    return _secret_path(agent_id).exists()


# agent_id is a path component under DATA_DIR/agents/ — a bad one must never
# escape that directory (../ traversal, absolute paths, hidden/system names).
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,63})$")


def validate_agent_id(agent_id: str):
    """Raise ValidationError if agent_id is not a safe single path component."""
    if not isinstance(agent_id, str) or not AGENT_ID_RE.match(agent_id or ""):
        raise ValidationError(
            "agent_id must be 1-64 chars: letters, digits, '.', '_', '-' "
            "(must start alphanumeric; no '/', no '..')")


def claimed_agent_count() -> int:
    agents_dir = DATA_DIR / "agents"
    if not agents_dir.exists():
        return 0
    return sum(1 for d in agents_dir.iterdir() if d.is_dir() and (d / "secret.txt").exists())


def ensure_agent_secret(agent_id: str, provided_secret: Optional[str] = None,
                         *, workspace_key: Optional[str] = None,
                         check_cap: bool = True) -> tuple[str, bool]:
    """Claim-or-verify ownership of agent_id. An already-claimed agent_id is
    verified against provided_secret; a brand-new claim requires a valid
    workspace_key, which resolves the workspace the agent_id is claimed
    into. Returns (secret, created).

    Raises AuthError if agent_id is already claimed and the secret doesn't
    match; WorkspaceKeyRequiredError if a new claim's workspace_key is absent
    or invalid; BetaCapExceededError if the resolved workspace's agent_cap is
    already reached (skipped when check_cap=False).
    """
    validate_agent_id(agent_id)
    path = _secret_path(agent_id)
    if path.exists():
        real = path.read_text().strip()
        if not provided_secret or not secrets.compare_digest(provided_secret, real):
            raise AuthError(
                f"agent_id '{agent_id}' is already claimed — pass its agent_secret "
                "(returned when the agent_id was first used) to write to it")
        return real, False
    # NEW claim from here — requires a valid workspace_key (D-<next>: workspace
    # identity, 2026-09-10). The site-wide BETA_AGENT_CAP/scarcity-by-agent-id
    # logic that used to live here is retired in favor of per-workspace caps.
    # Resolved via identity.py (not workspace_engine directly) so every
    # caller across the codebase goes through the same resolution function.
    import identity
    import workspace_engine
    workspace_id = identity.resolve_workspace_key(workspace_key)
    workspace = workspace_engine.get_workspace(workspace_id) if workspace_id else None
    if workspace is None:
        raise WorkspaceKeyRequiredError(
            "a new agent_id requires a valid workspace_key — get one "
            "self-serve at POST /v1/billing/x402 (no human, no login), or "
            "at https://aiagentscity.com/start")

    # effective_agent_cap, not the raw field: an EXPIRED scarcity grant still
    # has agent_cap=None stored, so reading the field directly would leave a
    # first-50 workspace unbounded forever.
    agent_cap = workspace_engine.effective_agent_cap(workspace)
    if check_cap and agent_cap is not None:
        claimed_in_workspace = sum(
            1 for d in (DATA_DIR / "agents").glob("*")
            if d.is_dir() and (d / "workspace_id.txt").exists()
            and (d / "workspace_id.txt").read_text().strip() == workspace["workspace_id"])
        if claimed_in_workspace >= agent_cap:
            raise BetaCapExceededError(
                f"Free tier: {agent_cap} agents per workspace. Upgrade to Pro ($19/mo) "
                "for unlimited agents — POST /v1/billing/checkout with this workspace's "
                "credentials returns a payment link bound to this workspace "
                "(mint a workspace at POST /start if you do not have one). "
                "A bare Stripe link cannot be used here: it carries no workspace "
                "reference, so the payment would not upgrade anything.")

    new_secret = secrets.token_urlsafe(24)
    _agent_dir(agent_id).mkdir(parents=True, exist_ok=True)
    path.write_text(new_secret)
    (_agent_dir(agent_id) / "workspace_id.txt").write_text(workspace["workspace_id"])
    return new_secret, True


# ── Credential lifecycle: rotate / revoke (D-1216) ──────────────────────────
# A lost agent_secret used to brick an agent_id permanently: ensure_agent_secret
# raises AuthError for a claimed id whose secret you no longer hold, and the
# workspace owner had NO path back in. On a free tier of 3 agents that is a
# third of a user's capacity, destroyed by an ordinary event. These two
# primitives are the recovery path.

def _audit_path(agent_id: str) -> Path:
    return _agent_dir(agent_id) / "audit.jsonl"


def log_agent_audit(agent_id: str, action: str, workspace_id: str = "") -> None:
    """Append a credential-lifecycle event to the agent's audit trail.

    Never raises: a rotation that succeeded must not be reported as failed
    because its receipt could not be written.
    """
    try:
        entry = {"ts": datetime.now(timezone.utc).isoformat(),
                 "agent_id": agent_id, "action": action,
                 "workspace_id": workspace_id}
        with open(_audit_path(agent_id), "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def rotate_agent_secret(agent_id: str, workspace_id: str = "") -> str:
    """Mint a NEW agent_secret for an already-claimed agent_id, invalidating
    the previous one. Returns the new secret (shown once, same contract as a
    first claim).

    Never claims: an id with no secret on disk raises AuthError rather than
    quietly minting one, so this can never be used to squat a fresh agent_id
    without a workspace_key.
    """
    validate_agent_id(agent_id)
    if not agent_exists(agent_id):
        raise AuthError(
            f"agent_id '{agent_id}' is not claimed — rotate applies to an agent "
            "you already own; a new agent_id is claimed by its first write")
    new_secret = secrets.token_urlsafe(24)
    _secret_path(agent_id).write_text(new_secret)
    log_agent_audit(agent_id, "rotate_secret", workspace_id)
    return new_secret


def revoke_agent_secret(agent_id: str, workspace_id: str = "") -> None:
    """Invalidate an agent_id's secret WITHOUT deleting its ledger.

    The secret file is OVERWRITTEN with a fresh random value nobody ever
    learns, not unlinked. agent_exists() is defined as "a secret file is
    present", so unlinking would make the id look unclaimed — and an unclaimed
    agent_id is claimable by ANY valid workspace_key. A plain delete would
    therefore let one workspace revoke an agent and a different workspace
    claim that same agent_id and inherit its spend history. Overwriting keeps
    the id claimed and unwritable until its owner rotates it back.
    """
    validate_agent_id(agent_id)
    if not agent_exists(agent_id):
        raise AuthError(
            f"agent_id '{agent_id}' is not claimed — nothing to revoke")
    _secret_path(agent_id).write_text(secrets.token_urlsafe(24))
    log_agent_audit(agent_id, "revoke_secret", workspace_id)


# ── Pro tier state ──────────────────────────────────────────────────────────
# The Stripe webhook (checkout.session.completed, plan=pro) flips this flag on
# the persistent volume; AL_PRO_ACTIVE=1 is an operator override. Until then
# the free-tier agent cap is enforced for everyone — Pro must DO something.

def _pro_flag_path() -> Path:
    return DATA_DIR / "pro.flag"


def pro_active() -> bool:
    if os.environ.get("AL_PRO_ACTIVE", "").strip() == "1":
        return True
    return _pro_flag_path().exists()


def activate_pro() -> None:
    """Mark this instance Pro-active (idempotent)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _pro_flag_path().write_text(datetime.now(timezone.utc).isoformat())


# ── Scarcity window: per-agent pro_until (v0.3.1) ───────────────────────────
# SQLite, same additive CREATE TABLE IF NOT EXISTS pattern as the idempotency
# store above. Also guards the case of an `agents` table that predates the
# pro_until column (defensive — nothing ships that yet, but the check is the
# safe way to add a column to an existing DB without a destructive migration).

def _agents_db_path() -> Path:
    return DATA_DIR / "agents.db"


def _agents_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_agents_db_path()), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            agent_id TEXT PRIMARY KEY,
            pro_until REAL
        )
    """)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(agents)")}
    if "pro_until" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN pro_until REAL")
    return conn


def _get_pro_until(agent_id: str) -> Optional[float]:
    conn = _agents_conn()
    try:
        row = conn.execute("SELECT pro_until FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _delete_agent_row(agent_id: str) -> None:
    """Drop agent_id's row from the agents table. Called by the owner DELETE
    path alongside the agent-dir removal (D-819) — a dir deleted without its
    row is an orphan row, and orphan rows diverge from the claimed-agent
    population the scarcity counter and window gate both count."""
    conn = _agents_conn()
    try:
        conn.execute("DELETE FROM agents WHERE agent_id=?", (agent_id,))
        conn.commit()
    finally:
        conn.close()


def is_pro(agent_id: str) -> dict:
    """Plan info for agent_id. Site-wide Stripe Pro (pro_active()) takes
    precedence over a per-agent scarcity grant; an expired scarcity grant
    reads back as free. Returns {"plan": ..., "pro_until": float|None,
    "is_pro": bool}."""
    if pro_active():
        return {"plan": "pro_stripe", "pro_until": None, "is_pro": True}
    pro_until = _get_pro_until(agent_id)
    if pro_until is not None and pro_until > _time.time():
        return {"plan": "pro_scarcity", "pro_until": pro_until, "is_pro": True}
    return {"plan": "free", "pro_until": None, "is_pro": False}


def scarcity_claims_left() -> int:
    """Public aggregate count only — no agent_ids, no emails, no workspace ids.

    Counts WORKSPACES, because that is the population the launch window
    actually admits: workspace_engine.create_workspace() grants the scarcity
    Pro when `workspace_count() < WORKSPACE_SCARCITY_CAP`, and both llms.txt
    and /.well-known/agent.json describe the offer as "the first 50 workspaces
    ever created".

    This previously returned `SCARCITY_PRO_CAP - claimed_agent_count()` — the
    per-AGENT population from v0.3.1 (D-818), which was retired when the
    window moved to per-workspace identity. The two populations diverge, so
    the public number disagreed with the gate it advertises and moved whenever
    an agent (not a workspace) was added or deleted: purging two test agents
    on 2026-09-13 pushed it from 44 to 46.

    Imported locally on purpose: workspace_engine imports this module at import
    time, so a module-level import here would be circular. Slots are not
    returned when a grant expires — the 50 are all-time workspaces.
    """
    import workspace_engine
    return max(0, workspace_engine.WORKSPACE_SCARCITY_CAP
               - workspace_engine.workspace_count())


# ── Idempotency-Key store (launch-kit v0.3) ─────────────────────────────────
# SQLite alongside the JSONL ledger — additive, CREATE TABLE IF NOT EXISTS
# only. Scoped per (id=key, agent_id, op) so the same raw key used for a
# "track" and a "budget" call on the same agent_id never collides.

def _idempotency_db_path() -> Path:
    return DATA_DIR / "idempotency.db"


def _idempotency_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_idempotency_db_path()), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS idempotency (
            id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            op TEXT NOT NULL,
            response_json TEXT,
            status_code INTEGER,
            created_at REAL NOT NULL,
            PRIMARY KEY (id, agent_id, op)
        )
    """)
    return conn


def idempotency_begin(key: str, agent_id: str, op: str) -> tuple[str, Optional[dict]]:
    """Start (or replay) an idempotent write.

    Returns ("proceed", None) — caller should execute the write and call
    idempotency_store() with the result.
    Returns ("cached", {"response": ..., "status_code": ...}) — a prior
    request with this exact key/agent_id/op already completed within the
    24h TTL; the caller must return that cached response, not re-execute.

    Raises IdempotencyKeyTooLongError if key exceeds MAX_IDEMPOTENCY_KEY_LEN.
    Raises IdempotencyConflictError if another request with this key is
    still in flight (row exists, no stored response yet, and it's not
    stale enough to assume the other process crashed).
    """
    if key is None:
        return "proceed", None
    if len(key) > MAX_IDEMPOTENCY_KEY_LEN:
        raise IdempotencyKeyTooLongError(
            f"Idempotency-Key must be <= {MAX_IDEMPOTENCY_KEY_LEN} chars (got {len(key)})")
    now = _time.time()
    conn = _idempotency_conn()
    try:
        try:
            conn.execute(
                "INSERT INTO idempotency (id, agent_id, op, response_json, status_code, created_at) "
                "VALUES (?, ?, ?, NULL, NULL, ?)",
                (key, agent_id, op, now))
            conn.commit()
            return "proceed", None
        except sqlite3.IntegrityError:
            pass  # a row already exists for this key/agent_id/op — inspect it below

        row = conn.execute(
            "SELECT response_json, status_code, created_at FROM idempotency "
            "WHERE id=? AND agent_id=? AND op=?", (key, agent_id, op)).fetchone()
        if row is None:
            # raced with a delete between the failed insert and this select —
            # safe to just insert fresh
            conn.execute(
                "INSERT INTO idempotency (id, agent_id, op, response_json, status_code, created_at) "
                "VALUES (?, ?, ?, NULL, NULL, ?)", (key, agent_id, op, now))
            conn.commit()
            return "proceed", None

        response_json, status_code, created_at = row
        if response_json is not None:
            if now - created_at < IDEMPOTENCY_TTL_SECONDS:
                return "cached", {"response": json.loads(response_json), "status_code": status_code}
            # expired cache entry — reclaim the slot for a fresh write
            conn.execute("DELETE FROM idempotency WHERE id=? AND agent_id=? AND op=?",
                        (key, agent_id, op))
            conn.execute(
                "INSERT INTO idempotency (id, agent_id, op, response_json, status_code, created_at) "
                "VALUES (?, ?, ?, NULL, NULL, ?)", (key, agent_id, op, now))
            conn.commit()
            return "proceed", None

        # response_json is NULL — another request is (or was) in flight
        if now - created_at > IDEMPOTENCY_INFLIGHT_STALE_SECONDS:
            conn.execute("DELETE FROM idempotency WHERE id=? AND agent_id=? AND op=?",
                        (key, agent_id, op))
            conn.execute(
                "INSERT INTO idempotency (id, agent_id, op, response_json, status_code, created_at) "
                "VALUES (?, ?, ?, NULL, NULL, ?)", (key, agent_id, op, now))
            conn.commit()
            return "proceed", None
        raise IdempotencyConflictError(
            f"idempotency key already in flight for agent_id '{agent_id}' op '{op}'")
    finally:
        conn.close()


def idempotency_store(key: Optional[str], agent_id: str, op: str, response: dict, status_code: int) -> None:
    """Record the completed response for a key started with idempotency_begin.
    No-op if key is None (idempotency wasn't requested for this write)."""
    if key is None:
        return
    conn = _idempotency_conn()
    try:
        conn.execute(
            "UPDATE idempotency SET response_json=?, status_code=?, created_at=? "
            "WHERE id=? AND agent_id=? AND op=?",
            (json.dumps(response), status_code, _time.time(), key, agent_id, op))
        conn.commit()
    finally:
        conn.close()


def idempotency_release(key: Optional[str], agent_id: str, op: str) -> None:
    """Delete an in-flight (response-less) row after a FAILED write so a
    retry with the same key re-attempts instead of getting 409 for the
    stale-window. Never touches rows that already hold a stored response.
    (Failed writes are not cached — Morgan review 2026-09-09.)"""
    if key is None:
        return
    conn = _idempotency_conn()
    try:
        conn.execute(
            "DELETE FROM idempotency WHERE id=? AND agent_id=? AND op=? "
            "AND response_json IS NULL", (key, agent_id, op))
        conn.commit()
    finally:
        conn.close()


def track(agent_id: str, rail: str, amount_cents: int, service: str,
          force: bool = False, **meta) -> SpendEntry:
    """Append a spend entry. Ownership (ensure_agent_secret) must already be
    verified by the caller — this function is also used by the trusted local
    CLI, which has no notion of secrets. Validates amount and blocks entries
    that would cross a set budget cap (rail=="tokens" rows are 0-cent burn
    bookkeeping and are exempt from budget checks; amount bounds still apply).

    force=True records a charge that has ALREADY been spent, skipping the cap
    check. It exists for the proxy's post-hoc path: the provider has charged us
    and the call is over, so refusing the write does not prevent the spend, it
    only erases it from the totals the cap is enforced from. Never reachable
    from a caller-supplied value — it is a reconciliation tool, and anything
    that lets a client set it defeats the cap. (Adversarial review 2026-09-13.)
    """
    validate_agent_id(agent_id)
    if rail == "tokens":
        if amount_cents != 0:
            raise ValidationError("rail 'tokens' rows must carry amount_cents=0 — "
                                  "token burn is bookkeeping, not spend")
        total_tokens = meta.get("tokens_in", 0) + meta.get("tokens_out", 0)
        if total_tokens > 0:
            budget = get_budget(agent_id)
            if budget and not force:
                if budget.monthly_token_cap > 0:
                    projected = _month_tokens(agent_id) + total_tokens
                    if projected > budget.monthly_token_cap:
                        raise BudgetExceededError(
                            f"blocked: this {total_tokens}-token entry would put "
                            f"{agent_id} at {projected} tokens this month, over its "
                            f"monthly token cap of {budget.monthly_token_cap}")
                if budget.daily_token_cap > 0:
                    projected_daily = _today_tokens(agent_id) + total_tokens
                    if projected_daily > budget.daily_token_cap:
                        raise BudgetExceededError(
                            f"blocked: this {total_tokens}-token entry would put "
                            f"{agent_id} at {projected_daily} tokens today, over its "
                            f"daily token cap of {budget.daily_token_cap}")
    else:
        if rail not in VALID_RAILS:
            raise ValidationError(
                f"rail must be one of {sorted(VALID_RAILS)} (got '{rail}')")
        if amount_cents < 0:
            raise ValidationError("amount_cents must be >= 0")
        if amount_cents > MAX_AMOUNT_CENTS:
            raise ValidationError(
                f"amount_cents exceeds the per-entry ceiling of {MAX_AMOUNT_CENTS} "
                f"(${MAX_AMOUNT_CENTS/100:,.0f})")
        budget = get_budget(agent_id)
        if budget and not force:
            if budget.monthly_cap_cents > 0:
                projected = _month_spend(agent_id) + amount_cents
                if projected > budget.monthly_cap_cents:
                    raise BudgetExceededError(
                        f"blocked: this ${amount_cents/100:.2f} spend would put "
                        f"{agent_id} at ${projected/100:.2f}, over its monthly cap "
                        f"of ${budget.monthly_cap_cents/100:.2f}")
            if budget.daily_cap_cents > 0:
                projected_daily = _today_spend(agent_id) + amount_cents
                if projected_daily > budget.daily_cap_cents:
                    raise BudgetExceededError(
                        f"blocked: this ${amount_cents/100:.2f} spend would put "
                        f"{agent_id} at ${projected_daily/100:.2f} today, over its "
                        f"daily cap of ${budget.daily_cap_cents/100:.2f}")

    entry = SpendEntry(agent_id=agent_id, rail=rail, amount_cents=amount_cents,
                       service=service, meta=meta)
    agent_dir = _agent_dir(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    with open(_ledger_path(agent_id), "a") as f:
        f.write(json.dumps(entry.to_dict()) + "\n")
    if rail != "tokens":
        _check_budget(agent_id)
        # Spike detection runs on the write path so a customer hears about a
        # spike as it happens, not only when they open a report (D-1230).
        # Ordered AFTER _check_budget on purpose: both alert through
        # _log_alert(agent_id, ...) with workspace_id defaulting to None, so
        # whichever runs first stamps the alert's workspace_id for the whole
        # dispatch. Budget is the more urgent alert and owns that stamp.
        scan_spending_spikes(agent_id)
    else:
        _check_token_budget(agent_id)
    return entry


def set_budget(agent_id: str, monthly_cents: int, daily_cents: int = 0, alert_pct: int = 80,
               monthly_tokens: int = 0, daily_tokens: int = 0) -> Budget:
    validate_agent_id(agent_id)
    if monthly_cents < 0 or daily_cents < 0:
        raise ValidationError("budget caps must be >= 0")
    if monthly_cents > MAX_AMOUNT_CENTS or daily_cents > MAX_AMOUNT_CENTS:
        raise ValidationError(f"budget caps exceed the ceiling of {MAX_AMOUNT_CENTS} cents")
    if monthly_tokens < 0 or daily_tokens < 0:
        raise ValidationError("token budget caps must be >= 0")
    budget = Budget(agent_id=agent_id, monthly_cap_cents=monthly_cents,
                    daily_cap_cents=daily_cents, alert_threshold_pct=alert_pct,
                    monthly_token_cap=monthly_tokens, daily_token_cap=daily_tokens)
    _agent_dir(agent_id).mkdir(parents=True, exist_ok=True)
    json.dump(budget.to_dict(), open(_budget_path(agent_id), "w"), indent=2)
    return budget


def get_budget(agent_id: str) -> Optional[Budget]:
    path = _budget_path(agent_id)
    if not path.exists():
        return None
    d = json.load(open(path))
    return Budget(**d)


def _month_spend(agent_id: str) -> int:
    if not _ledger_path(agent_id).exists():
        return 0
    now = datetime.now(timezone.utc)
    total = 0
    for line in open(_ledger_path(agent_id)):
        try:
            entry = json.loads(line)
            entry_dt = datetime.fromisoformat(entry["timestamp"])
            if entry_dt.year == now.year and entry_dt.month == now.month:
                total += entry["amount_cents"]
        except (KeyError, ValueError):
            continue
    return total


def _today_spend(agent_id: str) -> int:
    if not _ledger_path(agent_id).exists():
        return 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = 0
    for line in open(_ledger_path(agent_id)):
        try:
            entry = json.loads(line)
            if entry["timestamp"].startswith(today):
                total += entry["amount_cents"]
        except (KeyError, ValueError):
            continue
    return total


def _month_tokens(agent_id: str) -> int:
    if not _ledger_path(agent_id).exists():
        return 0
    now = datetime.now(timezone.utc)
    total = 0
    for line in open(_ledger_path(agent_id)):
        try:
            entry = json.loads(line)
            if entry.get("rail") != "tokens":
                continue
            entry_dt = datetime.fromisoformat(entry["timestamp"])
            if entry_dt.year == now.year and entry_dt.month == now.month:
                total += entry.get("tokens_in", 0) + entry.get("tokens_out", 0)
        except (KeyError, ValueError):
            continue
    return total


def _today_tokens(agent_id: str) -> int:
    if not _ledger_path(agent_id).exists():
        return 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = 0
    for line in open(_ledger_path(agent_id)):
        try:
            entry = json.loads(line)
            if entry.get("rail") != "tokens":
                continue
            if entry["timestamp"].startswith(today):
                total += entry.get("tokens_in", 0) + entry.get("tokens_out", 0)
        except (KeyError, ValueError):
            continue
    return total


def _check_token_budget(agent_id: str):
    budget = get_budget(agent_id)
    if not budget or budget.monthly_token_cap <= 0:
        return
    monthly = _month_tokens(agent_id)
    pct = monthly / budget.monthly_token_cap * 100
    alerts_path = _alerts_path(agent_id)
    existing = []
    if alerts_path.exists():
        existing = [json.loads(l) for l in open(alerts_path)]
    types = set(a.get("type") for a in existing)
    if pct >= 100 and "token_budget_exceeded" not in types:
        _log_alert(agent_id, "token_budget_exceeded",
                   f"Monthly token budget exceeded: {monthly} of {budget.monthly_token_cap} tokens")
    elif pct >= budget.alert_threshold_pct and "token_budget_warning" not in types:
        _log_alert(agent_id, "token_budget_warning",
                   f"Monthly token budget {pct:.0f}% used: {monthly} of {budget.monthly_token_cap} tokens")


def _check_budget(agent_id: str):
    budget = get_budget(agent_id)
    if not budget:
        return
    monthly = _month_spend(agent_id)
    pct = (monthly / budget.monthly_cap_cents * 100) if budget.monthly_cap_cents > 0 else 0
    alerts_path = _alerts_path(agent_id)
    existing = []
    if alerts_path.exists():
        existing = [json.loads(l) for l in open(alerts_path)]
    types = set(a.get("type") for a in existing)
    if pct >= 100 and "budget_exceeded" not in types:
        _log_alert(agent_id, "budget_exceeded",
                   f"Monthly budget exceeded: ${monthly/100:.2f} of ${budget.monthly_cap_cents/100:.2f}")
    elif pct >= budget.alert_threshold_pct and "budget_warning" not in types:
        _log_alert(agent_id, "budget_warning",
                   f"Monthly budget {pct:.0f}% used: ${monthly/100:.2f} of ${budget.monthly_cap_cents/100:.2f}")


# How far back the spike scan looks. report() defaults to 30 days, and the
# anomaly a customer sees on the report is the one that must have alerted, so
# these have to stay in step.
SPIKE_SCAN_DAYS = 30


def scan_spending_spikes(agent_id: str, days: int = SPIKE_SCAN_DAYS) -> list:
    """Raise an alert for each spending spike in the window that hasn't been
    alerted yet. Returns the spike days alerted on this call.

    Why this is a scan and not a check inside track(): a spike is a property of
    a day relative to the window's mean, and the mean MOVES as later entries
    land. A spike that exists at read time may not have existed when the
    offending entry was written — so raising it only from the write path would
    keep missing exactly the cases customers notice. This reads the same
    daily totals report() reads, through the same entries_since() reader, so
    the feed and the report cannot disagree about what a spike is.

    Idempotent per UTC day: the alert is recorded with its date in the message
    and a day is never alerted twice, because _log_alert_daily() dedupes on the
    alert TYPE and that would silence every later spike forever.
    """
    try:
        entries = entries_since(agent_id, days)
        daily_totals = {}
        for e in entries:
            day = e["timestamp"][:10]
            daily_totals[day] = daily_totals.get(day, 0) + e["amount_cents"]
        if len(daily_totals) < 2:
            return []
        avg = statistics.mean(daily_totals.values())
        if avg <= 0:
            return []

        already = set()
        path = _alerts_path(agent_id)
        if path.exists():
            for line in path.read_text().splitlines()[-500:]:
                try:
                    a = json.loads(line)
                except Exception:
                    continue
                if a.get("type") == "spending_spike":
                    already.add(str(a.get("date") or ""))

        alerted = []
        for day, amount in daily_totals.items():
            if amount > avg * SPIKE_MULTIPLIER and day not in already:
                # date lives on the record; the message carries it too so the
                # feed needs no schema change to be readable.
                _log_alert(agent_id, "spending_spike",
                           f"Spending spike on {day}: ${amount/100:.2f} vs a "
                           f"daily average of ${avg/100:.2f}")
                _stamp_alert_date(agent_id, day)
                alerted.append(day)
        return sorted(alerted)
    except Exception:
        # An alert must never be able to fail a write. Same reason
        # _log_alert() swallows transport failure.
        return []


def _stamp_alert_date(agent_id: str, day: str) -> None:
    """Add the `date` field to the last written alert.

    _log_alert() owns the alert schema and has no date concept, so rather than
    change its signature for one caller the scan annotates the record it just
    wrote. Done this way so the scan's dedupe key and the stored record are the
    same value.
    """
    try:
        path = _alerts_path(agent_id)
        lines = path.read_text().splitlines()
        if not lines:
            return
        last = json.loads(lines[-1])
        if last.get("type") != "spending_spike" or last.get("date"):
            return
        last["date"] = day
        lines[-1] = json.dumps(last)
        path.write_text("\n".join(lines) + "\n")
    except Exception:
        pass


def read_alerts(agent_id: str, limit: int = 20) -> list:
    """The agent's alert feed, newest first.

    Each row carries a numeric `ts` alongside the stored ISO `timestamp`, so a
    caller merging several agents' feeds into one dashboard list can sort
    without re-parsing dates and without inventing a second date format.
    """
    path = _alerts_path(agent_id)
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines()[-200:]:
        try:
            a = json.loads(line)
        except Exception:
            continue
        if not isinstance(a, dict):
            continue
        ts = a.get("ts")
        if not isinstance(ts, (int, float)):
            try:
                ts = datetime.fromisoformat(
                    str(a.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = 0
        a["ts"] = ts
        a.setdefault("agent_id", agent_id)
        out.append(a)
    out.sort(key=lambda a: a["ts"], reverse=True)
    return out[:limit]


def _log_alert(agent_id: str, alert_type: str, message: str):
    """Write an alert to the agent's feed AND push it to the workspace's
    registered destinations (D-1218).

    Every alert in the product flows through here, which is exactly why the
    delivery hook lives here and not at the call sites: a new alert type
    cannot be silently un-delivered, which is the failure mode of hooking each
    raise individually. Delivery is fire-and-forget inside dispatch(), so a
    slow or dead endpoint can never slow down or break this write.
    """
    alert = {"agent_id": agent_id, "type": alert_type, "message": message,
             "timestamp": datetime.now(timezone.utc).isoformat()}
    alerts_path = _alerts_path(agent_id)
    alerts_path.parent.mkdir(parents=True, exist_ok=True)
    with open(alerts_path, "a") as f:
        f.write(json.dumps(alert) + "\n")
    try:
        import alert_delivery
        alert_delivery.dispatch(alert_delivery.event_for(alert_type),
                                agent_id=agent_id, message=message)
    except Exception:
        # An alert that was logged but not pushed is still a recorded alert.
        # Never let transport failure turn into a failed write.
        pass


def _log_alert_daily(agent_id: str, alert_type: str, message: str):
    """Log at most one alert of this type per UTC day.

    Used for events that fire on a REJECTED write: a client retrying in a loop
    must not turn the alert feed into a denial-of-attention attack, but a
    genuine second incident on a later day still has to surface.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = _alerts_path(agent_id)
    if path.exists():
        try:
            for line in path.read_text().splitlines()[-200:]:
                try:
                    a = json.loads(line)
                except Exception:
                    continue
                if (a.get("type") == alert_type
                        and str(a.get("timestamp", "")).startswith(today)):
                    return
        except Exception:
            pass
    _log_alert(agent_id, alert_type, message)


def entries_since(agent_id: str, days: int = 30,
                  include_tokens: bool = False) -> list:
    """Raw ledger rows for an agent inside the window, oldest first.

    The single reader. The report, the workspace summary, the dashboard and the
    CSV export all call this rather than walking ledger.jsonl themselves, so
    they cannot drift into disagreeing about what was spent.

    Zero-cent rail="tokens" rows are burn bookkeeping, not spend, and are
    excluded unless include_tokens is set — those rows exist so token burn has
    somewhere to live without pretending to be money.
    """
    ledger = _ledger_path(agent_id)
    if not ledger.exists():
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
    out = []
    for line in open(ledger):
        try:
            e = json.loads(line)
            if not include_tokens and e.get("rail") == "tokens":
                continue
            if datetime.fromisoformat(e["timestamp"]).timestamp() >= cutoff:
                out.append(e)
        except (KeyError, ValueError):
            continue
    return out


def report(agent_id: str, days: int = 30) -> SpendReport:
    validate_agent_id(agent_id)
    entries = entries_since(agent_id, days)

    by_rail = {}
    by_service = {}
    total = 0
    for e in entries:
        total += e["amount_cents"]
        by_rail[e["rail"]] = by_rail.get(e["rail"], 0) + e["amount_cents"]
        by_service[e["service"]] = by_service.get(e["service"], 0) + e["amount_cents"]

    anomalies = []
    daily_totals = {}
    for e in entries:
        day = e["timestamp"][:10]
        daily_totals[day] = daily_totals.get(day, 0) + e["amount_cents"]
    if len(daily_totals) >= 2:
        avg = statistics.mean(daily_totals.values())
        for day, amount in daily_totals.items():
            if avg > 0 and amount > avg * SPIKE_MULTIPLIER:
                anomalies.append({"type": "spending_spike", "date": day,
                                  "amount_cents": amount, "avg_cents": round(avg)})

    budget_status = {}
    budget = get_budget(agent_id)
    if budget:
        monthly = _month_spend(agent_id)
        budget_status = {"monthly_cap_cents": budget.monthly_cap_cents,
                         "monthly_spend_cents": monthly,
                         "pct_used": round(monthly / budget.monthly_cap_cents * 100, 1) if budget.monthly_cap_cents else 0,
                         "exceeded": monthly > budget.monthly_cap_cents}
        if budget.monthly_token_cap > 0 or budget.daily_token_cap > 0:
            monthly_tokens = _month_tokens(agent_id)
            budget_status["monthly_token_cap"] = budget.monthly_token_cap
            budget_status["daily_token_cap"] = budget.daily_token_cap
            budget_status["monthly_tokens_used"] = monthly_tokens
            budget_status["token_pct_used"] = (round(monthly_tokens / budget.monthly_token_cap * 100, 1)
                                               if budget.monthly_token_cap else 0)
            budget_status["token_exceeded"] = (budget.monthly_token_cap > 0
                                               and monthly_tokens > budget.monthly_token_cap)

    plan_info = is_pro(agent_id)
    series = [{"date": day, "spend_cents": cents}
              for day, cents in sorted(daily_totals.items())]
    return SpendReport(agent_id=agent_id, period=f"last_{days}d", total_spend_cents=total,
                       by_rail=by_rail, by_service=by_service, entry_count=len(entries),
                       budget_status=budget_status, anomalies=anomalies,
                       plan=plan_info["plan"], pro_until=plan_info["pro_until"],
                       daily_series=series)


def list_agents() -> list:
    """Every claimed agent_id (has a minted secret), including slots that were
    claimed but never wrote a ledger entry — "squatted" slots that burn a
    beta cap slot invisibly unless surfaced with has_data=false."""
    agents_dir = DATA_DIR / "agents"
    if not agents_dir.exists():
        return []
    agents = []
    for d in agents_dir.iterdir():
        if not d.is_dir() or not (d / "secret.txt").exists():
            continue
        plan_info = is_pro(d.name)
        ledger = d / "ledger.jsonl"
        if ledger.exists():
            entries = [json.loads(l) for l in open(ledger)]
            total = sum(e.get("amount_cents", 0) for e in entries)
            agents.append({"agent_id": d.name, "entries": len(entries),
                           "total_spend_cents": total, "has_data": True,
                           "plan": plan_info["plan"], "pro_until": plan_info["pro_until"]})
        else:
            agents.append({"agent_id": d.name, "entries": 0,
                           "total_spend_cents": 0, "has_data": False,
                           "plan": plan_info["plan"], "pro_until": plan_info["pro_until"]})
    return sorted(agents, key=lambda x: x["total_spend_cents"], reverse=True)


def workspace_agents(workspace_id: str) -> list:
    """The agent_ids claimed in this workspace, sorted.

    Membership is answered by each agent's own workspace_id.txt — the same file
    every other ownership gate reads (identity.agent_belongs_to_workspace), so a
    dashboard cannot show an agent the write path would refuse, or hide one it
    would accept.
    """
    import identity
    if not workspace_id:
        return []
    return sorted(a["agent_id"] for a in list_agents()
                  if identity.workspace_of_agent(a["agent_id"]) == workspace_id)


def token_totals(agent_id: str, days: int = 30) -> tuple:
    """(tokens_in, tokens_out) for the window.

    Reads ONLY rail="tokens" rows, and the counts are at the TOP level of the
    entry (track() promotes **meta to keys — see the note in the tokens route).
    Summing every row's token keys would double-count: the proxy writes the burn
    onto BOTH the dollar row and its own 0-cent tokens row.
    """
    tin = tout = 0
    for e in entries_since(agent_id, days, include_tokens=True):
        if e.get("rail") != "tokens":
            continue
        tin += int(e.get("tokens_in") or 0)
        tout += int(e.get("tokens_out") or 0)
    return tin, tout


def workspace_summary(workspace_id: str, days: int = 30) -> dict:
    """Everything the dashboard shows, in one read.

    Per-agent rows plus workspace totals, a daily series summed across agents,
    and the recent alert feed. Built from the same report() every other surface
    uses, so the dashboard and a per-agent report can never disagree.
    """
    import time as _t
    agent_ids = workspace_agents(workspace_id)
    rows, by_rail, by_service, daily = [], {}, {}, {}
    spend_total = tokens_in_total = tokens_out_total = 0

    for aid in agent_ids:
        r = report(aid, days)
        tin, tout = token_totals(aid, days)
        budget = r.budget_status or {}
        cap = budget.get("monthly_cap_cents") or 0
        used = budget.get("monthly_spend_cents") or 0
        pct = round(used / cap * 100, 1) if cap else 0
        if budget.get("exceeded") or budget.get("token_exceeded"):
            status = "exceeded"
        elif pct >= 80:
            status = "warning"
        else:
            status = "ok"
        entries = entries_since(aid, days, include_tokens=True)
        last_ts = entries[-1]["timestamp"] if entries else None

        rows.append({
            "agent_id": aid,
            "tier": r.plan,
            "spend_cents_30d": r.total_spend_cents,
            "tokens_in_30d": tin,
            "tokens_out_30d": tout,
            "budget": {"monthly_cents": cap, "used_pct": pct, "status": status},
            "last_event_ts": last_ts,
            "anomaly": bool(r.anomalies),
        })
        spend_total += r.total_spend_cents
        tokens_in_total += tin
        tokens_out_total += tout
        for k, v in r.by_rail.items():
            by_rail[k] = by_rail.get(k, 0) + v
        for k, v in r.by_service.items():
            by_service[k] = by_service.get(k, 0) + v
        for point in r.daily_series:
            daily[point["date"]] = daily.get(point["date"], 0) + point["spend_cents"]

    alerts = []
    for aid in agent_ids:
        alerts.extend(read_alerts(aid, limit=20))
    alerts.sort(key=lambda a: a.get("ts") or 0, reverse=True)

    return {
        "workspace_id": workspace_id,
        "days": days,
        "agents": rows,
        "totals": {"spend_cents_30d": spend_total,
                   "tokens_in_30d": tokens_in_total,
                   "tokens_out_30d": tokens_out_total,
                   "by_rail": by_rail, "by_service": by_service},
        "daily_series": [{"date": d, "spend_cents": c}
                         for d, c in sorted(daily.items())],
        "alerts": alerts[:20],
    }


def export_rows(agent_ids: list, days: int = 30) -> list:
    """Flat rows for CSV: one line per ledger entry, timestamps included.

    Only rows that represent money or burn — every rail is exported, because
    "which rail did this come from" is exactly what someone opening the CSV is
    trying to find out.
    """
    out = []
    for aid in agent_ids:
        for e in entries_since(aid, days, include_tokens=True):
            out.append({
                "timestamp": e.get("timestamp", ""),
                "agent_id": aid,
                "rail": e.get("rail", ""),
                "service": e.get("service", ""),
                "amount_cents": e.get("amount_cents", 0),
                "tokens_in": e.get("tokens_in", ""),
                "tokens_out": e.get("tokens_out", ""),
                "model": e.get("model", ""),
            })
    out.sort(key=lambda r: r["timestamp"])
    return out