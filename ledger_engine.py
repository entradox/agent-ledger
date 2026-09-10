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
                         *, check_cap: bool = True) -> tuple[str, bool]:
    """Claim-or-verify ownership of agent_id. No signup required — the first
    write to a new agent_id mints a secret and returns it; every later write
    to that same agent_id must present it. Returns (secret, created).

    Raises AuthError if agent_id is already claimed and the secret doesn't
    match, or BetaCapExceededError if this would be a new agent past the
    free-tier slot cap (skipped when Pro is active — site-wide Stripe Pro,
    this specific agent_id already holding a live scarcity-window grant, or a
    new claim that is itself inside the scarcity window, which is about to be
    granted Pro below).

    A brand-new agent_id claimed while the all-time claimed count is still
    under SCARCITY_PRO_CAP is stamped with a one-year pro_until (scarcity
    window, v0.3.1) — see is_pro().
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
    pre_claim_count = claimed_agent_count()
    # Eligibility order is load-bearing (D-818): the scarcity window is
    # evaluated BEFORE the free-tier cap. Checking the cap first shadowed the
    # whole launch promise — a brand-new agent_id whose claim fell inside the
    # window (pre_claim_count < SCARCITY_PRO_CAP) got 402 and could never mint
    # the pro_until grant that claim was supposed to receive.
    inside_scarcity_window = pre_claim_count < SCARCITY_PRO_CAP
    if (check_cap and not pro_active() and not is_pro(agent_id)["is_pro"]
            and not inside_scarcity_window
            and pre_claim_count >= BETA_AGENT_CAP):
        raise BetaCapExceededError(
            f"Beta limit: {BETA_AGENT_CAP} agents tracked. Upgrade to Pro ($19/mo) "
            "for unlimited agents — https://buy.stripe.com/14AbJ0clUeoE9QN3Nl2400e "
            "— or contact entradox@icloud.com")
    new_secret = secrets.token_urlsafe(24)
    _agent_dir(agent_id).mkdir(parents=True, exist_ok=True)
    path.write_text(new_secret)
    if inside_scarcity_window:
        pro_until = _time.time() + SCARCITY_PRO_DURATION_SECONDS
        _set_pro_until(agent_id, pro_until)
        remaining = SCARCITY_PRO_CAP - (pre_claim_count + 1)
        metrics.record_event("pro_scarcity_claimed", remaining=remaining)
        if remaining == 0:
            metrics.record_event("pro_scarcity_exhausted")
    return new_secret, True


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


def _set_pro_until(agent_id: str, pro_until: float) -> None:
    conn = _agents_conn()
    try:
        conn.execute(
            "INSERT INTO agents (agent_id, pro_until) VALUES (?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET pro_until=excluded.pro_until",
            (agent_id, pro_until))
        conn.commit()
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
    """Public aggregate count only — no agent_ids or emails. A slot is
    consumed by a CLAIM (secret minted), and an agent_id can hold at most one
    slot, so this counts the claimed-agent store — the exact population the
    ensure_agent_secret() window gate admits against (D-819). Deliberately
    NOT a count of SQLite pro_until rows: that population diverges from
    claims in both directions (orphan rows left behind by a deleted agent
    under-report; legacy pre-v0.3.1 claims with a secret dir but no row are
    invisible), which let the gate and this counter disagree. Slots are not
    returned when a grant expires — the 50 are all-time claimed agents."""
    return max(0, SCARCITY_PRO_CAP - claimed_agent_count())


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


def track(agent_id: str, rail: str, amount_cents: int, service: str, **meta) -> SpendEntry:
    """Append a spend entry. Ownership (ensure_agent_secret) must already be
    verified by the caller — this function is also used by the trusted local
    CLI, which has no notion of secrets. Validates amount and blocks entries
    that would cross a set budget cap (rail=="tokens" rows are 0-cent burn
    bookkeeping and are exempt from budget checks; amount bounds still apply).
    """
    validate_agent_id(agent_id)
    if rail == "tokens":
        if amount_cents != 0:
            raise ValidationError("rail 'tokens' rows must carry amount_cents=0 — "
                                  "token burn is bookkeeping, not spend")
        total_tokens = meta.get("tokens_in", 0) + meta.get("tokens_out", 0)
        if total_tokens > 0:
            budget = get_budget(agent_id)
            if budget:
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
        if budget:
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


def _log_alert(agent_id: str, alert_type: str, message: str):
    alert = {"agent_id": agent_id, "type": alert_type, "message": message,
             "timestamp": datetime.now(timezone.utc).isoformat()}
    alerts_path = _alerts_path(agent_id)
    alerts_path.parent.mkdir(parents=True, exist_ok=True)
    with open(alerts_path, "a") as f:
        f.write(json.dumps(alert) + "\n")


def report(agent_id: str, days: int = 30) -> SpendReport:
    validate_agent_id(agent_id)
    ledger = _ledger_path(agent_id)
    entries = []
    if ledger.exists():
        cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
        for line in open(ledger):
            try:
                e = json.loads(line)
                if e.get("rail") == "tokens":
                    continue  # zero-cent token-count rows are burn data, not spend
                ts = datetime.fromisoformat(e["timestamp"]).timestamp()
                if ts >= cutoff:
                    entries.append(e)
            except (KeyError, ValueError):
                continue

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
            if avg > 0 and amount > avg * 2.5:
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
    return SpendReport(agent_id=agent_id, period=f"last_{days}d", total_spend_cents=total,
                       by_rail=by_rail, by_service=by_service, entry_count=len(entries),
                       budget_status=budget_status, anomalies=anomalies,
                       plan=plan_info["plan"], pro_until=plan_info["pro_until"])


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