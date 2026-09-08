#!/usr/bin/env python3
"""AgentLedger — per-agent spend management engine.

Tracks spending across multiple payment rails (MPP, x402, metered API),
enforces budget caps, detects anomalies, and produces audit trails.
"""
from __future__ import annotations

import json
import os
import secrets
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DATA_DIR = Path(os.environ.get("AGENT_LEDGER_DATA", os.path.expanduser("~/.agent-ledger")))

# beta: free tier caps total claimed agents site-wide; Pro ($19/mo) unlimited
BETA_AGENT_CAP = 3
# sanity ceiling on a single entry — blocks fat-finger / abuse-sized amounts
MAX_AMOUNT_CENTS = 10_000_000  # $100,000


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

    def to_dict(self) -> dict:
        return {"agent_id": self.agent_id, "monthly_cap_cents": self.monthly_cap_cents,
                "daily_cap_cents": self.daily_cap_cents, "alert_threshold_pct": self.alert_threshold_pct}


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
    free-tier slot cap.
    """
    path = _secret_path(agent_id)
    if path.exists():
        real = path.read_text().strip()
        if not provided_secret or not secrets.compare_digest(provided_secret, real):
            raise AuthError(
                f"agent_id '{agent_id}' is already claimed — pass its agent_secret "
                "(returned when the agent_id was first used) to write to it")
        return real, False
    if check_cap and claimed_agent_count() >= BETA_AGENT_CAP:
        raise BetaCapExceededError(
            f"Beta limit: {BETA_AGENT_CAP} agents tracked. Upgrade to Pro ($19/mo) "
            "for unlimited agents — https://buy.stripe.com/14AbJ0clUeoE9QN3Nl2400e "
            "— or contact entradox@icloud.com")
    new_secret = secrets.token_urlsafe(24)
    _agent_dir(agent_id).mkdir(parents=True, exist_ok=True)
    path.write_text(new_secret)
    return new_secret, True


def track(agent_id: str, rail: str, amount_cents: int, service: str, **meta) -> SpendEntry:
    """Append a spend entry. Ownership (ensure_agent_secret) must already be
    verified by the caller — this function is also used by the trusted local
    CLI, which has no notion of secrets. Validates amount and blocks entries
    that would cross a set budget cap (rail=="tokens" rows are 0-cent burn
    bookkeeping and are exempt from both checks).
    """
    if rail != "tokens":
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
    return entry


def set_budget(agent_id: str, monthly_cents: int, daily_cents: int = 0, alert_pct: int = 80) -> Budget:
    budget = Budget(agent_id=agent_id, monthly_cap_cents=monthly_cents,
                    daily_cap_cents=daily_cents, alert_threshold_pct=alert_pct)
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

    return SpendReport(agent_id=agent_id, period=f"last_{days}d", total_spend_cents=total,
                       by_rail=by_rail, by_service=by_service, entry_count=len(entries),
                       budget_status=budget_status, anomalies=anomalies)


def list_agents() -> list:
    agents_dir = DATA_DIR / "agents"
    if not agents_dir.exists():
        return []
    agents = []
    for d in agents_dir.iterdir():
        if d.is_dir():
            ledger = d / "ledger.jsonl"
            if ledger.exists():
                entries = [json.loads(l) for l in open(ledger)]
                total = sum(e.get("amount_cents", 0) for e in entries)
                agents.append({"agent_id": d.name, "entries": len(entries),
                               "total_spend_cents": total})
    return sorted(agents, key=lambda x: x["total_spend_cents"], reverse=True)