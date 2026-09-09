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
        rec.update(fields)
        with open(METRICS_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


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
