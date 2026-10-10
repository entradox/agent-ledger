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
        # Never let a caller-supplied field retype or backdate the record:
        # kind/ts/ip_bucket are written by us and stay ours (review D-1626).
        rec.update({k: v for k, v in fields.items() if k not in rec})
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
    "x402_paid",           # x402 settled -> a NEW workspace was minted+Pro'd
    "x402_repeat_paid",    # x402 settled -> an EXISTING wallet paid again
    "workspace_returned",  # wrote again >=24h after its first-ever write
)

# How long after a workspace's first write a later write counts as "returned"
# (stickiness, not just a one-time poke). One place so the track route and any
# future caller agree on the definition.
RETURNING_WORKSPACE_SECONDS = 24 * 3600

_FORBIDDEN_ONBOARDING_FIELDS = ("ip_hash", "workspace_key", "agent_secret",
                                "agent_secret_hash", "workspace_key_hash")


def record_onboarding(step: str, workspace_id: str = "", **fields):
    """Record one onboarding step. Silently ignores an unknown step name so a
    typo can never widen the stream, and strips anything that could carry a
    credential or an IP into an event log that is read by humans.

    Demo workspaces are excluded: `agent-ledger demo` mints a real workspace to
    exercise real enforcement, and if that landed in the funnel the launch
    numbers would count demonstrations as demand.
    """
    if step not in ONBOARDING_STEPS:
        return
    if workspace_id and is_demo_workspace(workspace_id):
        return
    for bad in _FORBIDDEN_ONBOARDING_FIELDS:
        fields.pop(bad, None)
    record_event("onboarding", step=step, workspace_id=workspace_id or "", **fields)


def is_demo_workspace(workspace_id: str) -> bool:
    """True if this workspace was minted for demonstration, not by a customer.

    Reads the workspace record directly rather than keeping a second list, so
    the tag cannot drift from the workspace it describes. An unreadable or
    missing record returns False — never silently hide a real workspace's
    funnel events on a transient read error.
    """
    if not workspace_id:
        return False
    try:
        import workspace_engine
        rec = workspace_engine.get_workspace(workspace_id)
    except Exception:
        return False
    return bool(rec and rec.get("demo"))


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


# ── Setup/connect funnel (D-1626) ─────────────────────────────────────────
# `npx @aiagentscity/setup` reports one setup_event per harness outcome plus a
# 'started' per run, optionally stamped with a connect_attempt_id the connect
# page mints into the suggested command. This is the read side: file-scan
# like the other durable funnels, admin-only caller.
_SETUP_SUCCESS_OUTCOMES = ("registered", "already_registered", "skill_installed")


def setup_connect_funnel() -> dict:
    """Aggregate setup_event / setup_attempt_minted records into the connect
    funnel: page-render attempt → CLI event → success outcome.

    An attempt id only counts when the site actually minted it (a
    `setup_attempt_minted` record written at render). Events carrying an id
    no render produced land in `unmatched` — visible as a forgery /
    pre-instrumentation signal rather than silently inflating the ratio.
    This is the difference between telemetry and an honor system: a forged
    `registered` still costs the caller a real page fetch per fake id.
    """
    outcomes = defaultdict(int)
    harnesses = defaultdict(int)
    products = defaultdict(int)
    failure_reasons = defaultdict(int)
    human = {"human": 0, "agent": 0}
    minted = set()
    minted_ips = set()
    seen_success = set()
    seen_any = set()
    try:
        with open(METRICS_FILE) as f:
            for line in f:
                if '"setup_' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                kind = rec.get("kind")
                if kind == "setup_attempt_minted":
                    if rec.get("connect_attempt_id"):
                        minted.add(rec["connect_attempt_id"])
                        if rec.get("ip_hash"):
                            minted_ips.add(rec["ip_hash"])
                    continue
                if kind != "setup_event":
                    continue
                outcome = rec.get("outcome")
                if not outcome:
                    continue
                outcomes[outcome] += 1
                if rec.get("harness"):
                    harnesses[rec["harness"]] += 1
                if rec.get("product"):
                    products[rec["product"]] += 1
                if rec.get("failure_reason"):
                    failure_reasons[rec["failure_reason"]] += 1
                human["human" if rec.get("is_human_initiated") else "agent"] += 1
                attempt = rec.get("connect_attempt_id")
                if attempt:
                    seen_any.add(attempt)
                    if outcome in _SETUP_SUCCESS_OUTCOMES:
                        seen_success.add(attempt)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    reported = minted & seen_any
    succeeded = minted & seen_success
    unmatched = seen_any - minted
    return {
        "events": sum(outcomes.values()),
        "by_outcome": dict(sorted(outcomes.items())),
        "by_harness": dict(sorted(harnesses.items())),
        "by_product": dict(sorted(products.items())),
        "failure_reasons": dict(sorted(failure_reasons.items())),
        "initiated_by": human,
        "attempts": {
            # minted: page renders that surfaced the command.
            # reported: minted ids a CLI run actually came back with.
            # succeeded: minted ids that reached a terminal success outcome.
            "minted": len(minted),
            # distinct salted-IP digests behind the mints — a bot/probe burst
            # inflates `minted` but barely moves this.
            "minted_distinct_ips": len(minted_ips),
            "reported": len(reported),
            "succeeded": len(succeeded),
            "report_rate": round(len(reported) / len(minted), 3) if minted else None,
            "success_rate": round(len(succeeded) / len(reported), 3) if reported else None,
            # ids seen in events that no page minted: forged traffic, or
            # events from before minting shipped. Counted, never trusted.
            "unmatched": len(unmatched),
            "note": ("attempt ids are minted per GET render of / and "
                     "/developers and recorded; only minted ids join the "
                     "funnel — a fabricated id shows up in `unmatched` "
                     "instead. Honest limits: outcomes are self-reported by "
                     "the CLI, so minted ids CAN be harvested by fetching "
                     "the page (directional traffic, not audited "
                     "conversion); `reported` only covers the two minting "
                     "surfaces — llms.txt / agents.txt / quickstart events "
                     "arrive with a null id and live in by_outcome only; "
                     "mint and report must reach the same metrics.jsonl — "
                     "a redeploy or second replica between them lands a "
                     "genuine install in `unmatched`."),
        },
    }


def funnel_from_file() -> dict:
    """Per-kind event counts read from metrics.jsonl, not from the in-memory
    counters.

    `_totals` is process-lifetime: it starts at zero on every boot, so the
    funnel silently reported `track_ok: 3` while 88 track_ok events sat on disk
    going back six days. A brand-new deploy is exactly when an operator looks
    at this page, and it showed a number that was confidently wrong — the
    "distribution stopped" reading, when nothing had stopped. Same defect and
    same fix as reach_from_file(): read the durable record.

    last_24h stays in-memory on purpose. The counter and the file agree on
    anything recorded since this process started, which is the whole window
    that number covers; reading the tail of the file for it would buy nothing.

    Called only from the admin-gated /v1/metrics.
    """
    counts = defaultdict(int)
    try:
        with open(METRICS_FILE) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                kind = rec.get("kind")
                if kind:
                    counts[kind] += 1
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return counts


def amount_sums_from_file() -> dict:
    """Sum of `amount_cents` per kind, read from metrics.jsonl.

    The companion to funnel_from_file(). Leaving this in-memory while the count
    went durable would have produced "2 completions, $0.00" after a deploy —
    a new confidently-wrong number created by fixing the one next to it.
    """
    sums = defaultdict(int)
    try:
        with open(METRICS_FILE) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                kind = rec.get("kind")
                amount = rec.get("amount_cents")
                if kind and isinstance(amount, int):
                    sums[kind] += amount
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return sums


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


# ── Proxy latency (D-1250) — the overhead AgentLedger itself adds, not the
# provider's round trip. Measured around _pre_call_check() in routes_proxy.py:
# that is the budget/estimate computation this product does before forwarding,
# so it is the honest number for "how much slower does routing through us
# make a call", not the LLM provider's own response time. ────────────────────
LATENCY_MAX_SAMPLES = 2000


def record_latency(op: str, ms: float):
    """Append one latency sample. Never raises — same contract as record_event."""
    record_event("latency", op=op, ms=round(ms, 2))


def latency_percentiles(op: str) -> dict:
    """p50/p95/p99 over the most recent LATENCY_MAX_SAMPLES samples for `op`,
    read from metrics.jsonl. Returns sample_count=0 (no invented numbers) if
    nothing has been measured yet. Called only from /reliability, not a hot path."""
    samples = []
    try:
        with open(METRICS_FILE) as f:
            for line in f:
                if '"kind": "latency"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("op") == op and isinstance(rec.get("ms"), (int, float)):
                    samples.append(rec["ms"])
    except FileNotFoundError:
        pass
    except Exception:
        pass
    samples = samples[-LATENCY_MAX_SAMPLES:]
    if not samples:
        return {"op": op, "sample_count": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None}
    samples.sort()

    def _pct(p):
        idx = min(len(samples) - 1, int(round(p * (len(samples) - 1))))
        return round(samples[idx], 2)

    return {"op": op, "sample_count": len(samples),
            "p50_ms": _pct(0.50), "p95_ms": _pct(0.95), "p99_ms": _pct(0.99)}


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
