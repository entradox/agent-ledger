#!/usr/bin/env python3
"""AgentLedger — owner telemetry layer.

Thread-safe, append-only event log (DATA_DIR/metrics.jsonl) plus in-memory
counters for cheap "last 24h" rollups. No PII: callers pass an ip_hash
(sha256(ip + salt), truncated) or None — raw IPs are never recorded here.
"""
import json
import os
import threading
import time
from collections import deque, defaultdict
from pathlib import Path

DATA_DIR = Path(os.environ.get("AGENT_LEDGER_DATA", os.path.expanduser("~/.agent-ledger")))
METRICS_FILE = DATA_DIR / "metrics.jsonl"

WINDOW_SECONDS = 24 * 3600
MAX_UNIQUE_IPS_PER_KIND = 10_000

_lock = threading.Lock()
_totals = defaultdict(int)
# kind -> running sum of the "amount_cents" field, when present (revenue telemetry)
_amount_sums = defaultdict(int)
# deque of (ts, kind) — used to compute cheap last-24h rollups without
# re-reading the file on every request.
_recent: deque = deque()
# kind -> set of ip_hash seen (capped), in-memory only, never persisted
_unique_ips = defaultdict(set)


def _prune_recent(now: float):
    cutoff = now - WINDOW_SECONDS
    while _recent and _recent[0][0] < cutoff:
        _recent.popleft()


def record_event(kind: str, *, ip_bucket: str = None, **fields):
    """Append one JSON line to metrics.jsonl and bump in-memory counters.
    Never raises — callers wrap this in the hot request path.

    ip_bucket: key used for the unique-ip-hash set (defaults to `kind`).
    Lets callers track reach per-path (e.g. "path:/status") separately from
    the per-kind funnel counters.
    """
    now = time.time()
    ip_hash = fields.get("ip_hash")
    bucket = ip_bucket or kind
    amount = fields.get("amount_cents")
    try:
        with _lock:
            _totals[kind] += 1
            _recent.append((now, kind))
            _prune_recent(now)
            if ip_hash:
                ips = _unique_ips[bucket]
                if len(ips) < MAX_UNIQUE_IPS_PER_KIND:
                    ips.add(ip_hash)
            if isinstance(amount, int):
                _amount_sums[kind] += amount
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"kind": kind, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
        # ip_bucket is a named parameter, so it is NOT inside `fields` — without
        # this it lived only in the in-memory bucket key and never reached the
        # file, which is why the durable reach read found nothing to count. It is
        # a route label like "path:/start", never an address.
        if ip_bucket:
            rec["ip_bucket"] = ip_bucket
        rec.update(fields)
        with open(METRICS_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# ── Onboarding funnel (D-1163) ────────────────────────────────────────────
# A per-WORKSPACE step stream, deliberately separate from the `http` reach
# counters: reach answers "was the page seen", this answers "where did the
# signup stop". Keyed by workspace_id (an opaque random id), never by an IP
# hash and never by a credential — `record_onboarding` strips both.
ONBOARDING_STEPS = (
    "workspace_minted",    # POST /start created the workspace
    "key_revealed",        # the one-time key was actually shown
    "checkout_clicked",    # the upgrade button was pressed (client beacon)
    "checkout_completed",  # Stripe says it was paid
    "checkout_abandoned",  # Stripe says the session expired unpaid
    "agent_claimed",       # the workspace claimed its first agent_id
    "track_written",       # and wrote its first spend entry (same call)
)

_FORBIDDEN_ONBOARDING_FIELDS = ("ip_hash", "workspace_key", "agent_secret",
                                "agent_secret_hash", "workspace_key_hash")


def record_onboarding(step: str, workspace_id: str = "", **fields):
    """Record one onboarding step. Silently ignores an unknown step name so a
    typo can never widen the stream, and strips anything that could carry a
    credential or an IP into an event log that is read by humans."""
    if step not in ONBOARDING_STEPS:
        return
    for bad in _FORBIDDEN_ONBOARDING_FIELDS:
        fields.pop(bad, None)
    record_event("onboarding", step=step, workspace_id=workspace_id or "", **fields)


def onboarding_funnel() -> dict:
    """Per-step DISTINCT-workspace counts plus the drop-off between adjacent
    steps, computed by scanning metrics.jsonl. Called only from the
    admin-gated /v1/metrics, never on a request hot path."""
    seen = {step: set() for step in ONBOARDING_STEPS}
    try:
        with open(METRICS_FILE) as f:
            for line in f:
                if '"onboarding"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                step = rec.get("step")
                ws = rec.get("workspace_id") or ""
                if step in seen and ws:
                    seen[step].add(ws)
    except FileNotFoundError:
        pass
    except Exception:
        pass

    steps = []
    prev = None
    for step in ONBOARDING_STEPS:
        n = len(seen[step])
        entry = {"step": step, "workspaces": n,
                 # Present on every step, including the first, so a consumer
                 # never has to special-case index 0 to read the shape.
                 "dropped_from_previous": max(0, prev - n) if prev is not None else None,
                 "conversion_from_previous": (
                     round(n / prev, 3) if prev else None) if prev is not None else None}
        steps.append(entry)
        prev = n
    return {"steps": steps,
            "note": ("distinct workspaces that reached each step. "
                     "checkout_completed is real money; checkout_abandoned "
                     "arrives ~24h late (Stripe expiry), so a recent "
                     "abandonment can lag.")}


def reach_from_file() -> dict:
    """Distinct ip_hash per reach bucket, computed from metrics.jsonl.

    The in-memory `_unique_ips` set is process-lifetime, so it resets to zero on
    every deploy — which meant the reach number fell from 17 to 2 after a
    restart and read as "the traffic stopped" when nothing had stopped. Reach is
    the number used to judge whether distribution is working, so it has to
    survive a deploy. Reads the file (admin-only caller, ~1MB today).
    """
    buckets = defaultdict(set)
    try:
        with open(METRICS_FILE) as f:
            for line in f:
                if '"kind": "http"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                bucket = rec.get("ip_bucket")
                ip = rec.get("ip_hash")
                if not bucket and ip and rec.get("path"):
                    # Events written before ip_bucket was persisted still carry
                    # the path and an ip_hash, and the middleware's bucket rule
                    # was exactly f"path:{path}" — so history is recoverable
                    # instead of restarting the count at zero. Safe for paths
                    # that were never reach-bucketed: the caller only looks up
                    # REACH_PATHS keys.
                    bucket = f"path:{rec['path']}"
                if bucket and ip:
                    buckets[bucket].add(ip)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {b: len(ips) for b, ips in buckets.items()}


def snapshot() -> dict:
    """Return total + last-24h counts per kind, plus unique-ip-hash counts per
    bucket (kind, or a custom ip_bucket like "path:/status"). The unique set
    is process-lifetime (not strictly windowed to 24h) — cheap and good
    enough for a reach indicator."""
    now = time.time()
    with _lock:
        _prune_recent(now)
        last_24h = defaultdict(int)
        for ts, kind in _recent:
            last_24h[kind] += 1
        totals = dict(_totals)
        last_24h = dict(last_24h)
        unique_ips = {bucket: len(ips) for bucket, ips in _unique_ips.items()}
        amount_sums = dict(_amount_sums)
    return {"totals": totals, "last_24h": last_24h, "unique_ip_hashes": unique_ips,
            "amount_cents_sum": amount_sums}
