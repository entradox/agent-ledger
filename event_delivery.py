#!/usr/bin/env python3
"""AgentLedger — agent-facing event delivery: wait-with-liveness (D-1625).

The gap this closes: `alert_delivery.py` pushes events to a URL the consumer
owns. An agent on stdio MCP — Claude Code, Codex, a desktop client — has no
URL. For that consumer class "alerts" were a feed it had to remember to poll,
and mostly didn't. This module is the pull side: the consumer long-polls a
server-held feed and reports its own liveness so the server knows a live
consumer from a corpse.

Mechanics are adapted from the bolter-agent binary (evidence:
findings/D-1625-bolter-rea-evidence.md — GET /api/v1/agent/wait?ppid=&lstart=&comm=
reports the listener's process identity; the server marks redelivered on
restart). Their ppid/lstart/comm triple becomes our (consumer_id, pid,
started) — the process identity a remote agent can honestly report.

Semantics:

* `consumer_id` names a durable consumer, not a process. Two processes with
  the same consumer_id are one consumer whose earlier instance died — and
  only one wait per consumer runs at a time, so overlapping instances get a
  typed 429 rather than a double-served batch.
* `cursor` is an absolute line offset into the agent's alerts.jsonl
  (append-only, so line offsets are stable; the feed is read whole and
  mtime-cached, never tail-windowed, so the offset stays honest at any file
  size). A client that sends `cursor=N` is acknowledging "I have processed
  through line N" — that is the only ack channel.
* Server tracks two positions per consumer: `acked` (client-confirmed) and
  `served` (high-water served). Normally events serve from `served` — each
  event goes out once. When the liveness triple changes, unacked events
  (acked..served) re-serve with `redelivered: true` — the dead instance may
  have crashed holding them.
* A consumer that never sends `cursor` acknowledges nothing; on restart its
  whole history re-serves as redelivered. That is correct — the server
  cannot tell a crashed-while-processing from a processed-but-forgot-to-ack.
* `pid` without `started` is treated as a NEW instance every call: PIDs get
  reused across reboots, so 'same pid, no started' must over-deliver rather
  than fake continuity.

Same posture as alert_delivery: payloads carry metadata only — never a
secret, never a prompt. The wait itself is bounded (timeout capped,
concurrent waiters capped per-agent and globally) so it cannot pin the
server's thread pool or let one tenant deny the endpoint to others.
"""
import collections
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import re

MAX_WAIT_TIMEOUT_SECONDS = 30.0
DEFAULT_WAIT_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.25
WAIT_BATCH_LIMIT = 50
# Waiters are thread-pool residents for up to `timeout` seconds. The cap is
# per-agent AND global: one tenant opening 4 waits cannot deny the endpoint
# to every other tenant.
MAX_WAITS_PER_AGENT = 4
MAX_CONCURRENT_WAITS = 16
CONSUMER_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,63})$")
MAX_CONSUMERS_PER_AGENT = 50

_permits_lock = threading.Lock()
_waits_per_agent = {}       # agent_id -> in-flight wait count
_waits_consumers = set()    # (agent_id, consumer_id) with a wait in flight
_registry_lock = threading.Lock()
_feed_cache = collections.OrderedDict()  # agent_id -> (ino, mtime, size, lines)


class ConsumerError(Exception):
    """Bad consumer params the caller must fix (bad id, bad cursor)."""


class WaitCongestedError(Exception):
    """Too many consumers waiting at once — caller should back off."""


def _data_dir() -> Path:
    return Path(os.environ.get("AGENT_LEDGER_DATA",
                               os.path.expanduser("~/.agent-ledger")))


def _alerts_path(agent_id: str) -> Path:
    return _data_dir() / "agents" / agent_id / "alerts.jsonl"


def _ledger_path(agent_id: str) -> Path:
    return _data_dir() / "agents" / agent_id / "ledger.jsonl"


def _consumers_path(agent_id: str) -> Path:
    return _data_dir() / "agents" / agent_id / "consumers.json"


# ── shared event envelope (wait responses AND webhook payloads) ────────────

def _event_id(agent_id: str, kind: str, timestamp: str, summary: str) -> str:
    """Deterministic event id — the same alert always names itself the same,
    so a consumer-side dedup needs no server round-trip."""
    raw = f"{agent_id}|{kind}|{timestamp}|{summary}"
    return "evt_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def envelope(*, kind: str, agent_id: str, summary: str, timestamp: str,
             alert_type: Optional[str] = None,
             alerts_since_last_write: int = 0,
             redelivered: bool = False) -> dict:
    """One event, shaped so a consumer-side router can triage it without
    waking a full session: stable id, kind, a one-line summary, the context
    needed to route (which agent, which alert type, when), a loop-breaker
    counter (how long the feed has been talking without the agent writing),
    and the redelivery flag."""
    return {
        "id": _event_id(agent_id, kind, timestamp, summary),
        "kind": kind,
        "summary": summary,
        "context": {"agent_id": agent_id,
                    "alert_type": alert_type,
                    "timestamp": timestamp},
        "alerts_since_last_write": alerts_since_last_write,
        "redelivered": redelivered,
    }


def alerts_since_last_write(agent_id: str) -> int:
    """How many alerts sit newer than the agent's last ledger write — the
    feed-side measure of 'talking without a response'. A consumer's router
    can throttle on this the way bolter's conversation counters throttle
    agent-to-agent loops: the longer the streak, the more a post has to earn
    its place."""
    last_write = 0.0
    ledger_path = _ledger_path(agent_id)
    try:
        ledger_exists = ledger_path.exists()
    except Exception:
        # exists() propagates PermissionError on py3.13 — an unreadable
        # dir must not escape as a raise
        ledger_exists = False
    if ledger_exists:
        try:
            lines = ledger_path.read_text().splitlines()
            for line in reversed(lines[-50:]):
                try:
                    e = json.loads(line)
                    ts = _parse_ts(e.get("timestamp"))
                    if ts:
                        last_write = ts
                        break
                except Exception:
                    continue
        except Exception:
            pass
    count = 0
    for line in _read_alert_lines(agent_id):
        try:
            a = json.loads(line)
        except Exception:
            continue
        ts = _parse_ts(a.get("timestamp"))
        if ts and ts > last_write:
            count += 1
        elif not ts:
            count += 1  # unparseable timestamp errs toward "unactioned"
    return count


def _parse_ts(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


# ── consumer registry ──────────────────────────────────────────────────────

def _load_consumers(agent_id: str) -> dict:
    path = _consumers_path(agent_id)
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("consumer registry is not a JSON object")
        return data
    except Exception:
        # A corrupt registry is evidence, not noise — quarantine it so the
        # damage is diagnosable instead of silently re-zeroing positions.
        # This covers bad JSON, bad UTF-8, a squatting directory, and a
        # non-object payload; an unstatable path can't be renamed either,
        # which the inner try absorbs — nothing here may raise.
        try:
            # nonce: two corruptions in the same second must not collide —
            # rename(dir -> existing file) fails and the squatter survives
            path.rename(path.with_name(
                f"{path.name}.corrupt-{int(time.time())}-"
                f"{os.urandom(3).hex()}"))
        except Exception:
            pass
        return {}


def _save_consumers(agent_id: str, consumers: dict) -> None:
    path = _consumers_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(consumers, indent=2))
    os.replace(tmp, path)  # atomic — a crash mid-write never halves the file


def _liveness(pid, started: str) -> tuple:
    """The consumer's process identity, normalized. A caller that reports a
    pid but no `started` gets a per-call nonce instead of a stable triple —
    PIDs are reused across reboots, so 'same pid, no started' must read as a
    new instance (at-least-once redelivery) rather than fake continuity. A
    caller that reports nothing at all is always the 'same' instance — the
    honest limit of an optional field."""
    if pid is not None:
        return (str(pid), (started or "")[:64] or f"nonce-{time.monotonic_ns()}")
    return ("", (started or "")[:64])


# ── the wait ───────────────────────────────────────────────────────────────

FEED_CACHE_MAX_AGENTS = 256


def _read_alert_lines(agent_id: str) -> list:
    """Every line of the agent's alerts.jsonl, absolute line offsets intact —
    the cursor IS a line offset, so a truncated tail window would silently
    move it. Cached on (inode, mtime, size): a 4 Hz poll loop stats the file
    each tick and only re-reads when it actually changed, so the per-tick
    cost is a stat, not an O(file) read. Bounded: the cache holds at most
    FEED_CACHE_MAX_AGENTS feeds, evicting the least-recently read."""
    path = _alerts_path(agent_id)
    try:
        st = path.stat()
    except Exception:
        _feed_cache.pop(agent_id, None)
        return []
    cached = _feed_cache.get(agent_id)
    if cached and cached[:3] == (st.st_ino, st.st_mtime_ns, st.st_size):
        _feed_cache.move_to_end(agent_id)
        return cached[3]
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return cached[3] if cached else []
    _feed_cache[agent_id] = (st.st_ino, st.st_mtime_ns, st.st_size, lines)
    _feed_cache.move_to_end(agent_id)
    while len(_feed_cache) > FEED_CACHE_MAX_AGENTS:
        _feed_cache.popitem(last=False)  # evict least-recently-read
    return lines


def _acquire_wait(agent_id: str, consumer_id: str) -> None:
    """Take a wait slot: per-agent cap (no tenant can starve the endpoint),
    global backstop (thread-pool bound), and one wait per consumer — two
    concurrent waits for the same consumer_id would race the registry and
    double-serve the same batch."""
    key = (agent_id, consumer_id)
    with _permits_lock:
        if key in _waits_consumers:
            raise WaitCongestedError(
                f"a wait is already in flight for consumer {consumer_id!r} — "
                "only one wait per consumer; back off and retry")
        if sum(_waits_per_agent.values()) >= MAX_CONCURRENT_WAITS:
            raise WaitCongestedError(
                f"too many consumers waiting at once "
                f"(>{MAX_CONCURRENT_WAITS}) — back off and retry")
        if _waits_per_agent.get(agent_id, 0) >= MAX_WAITS_PER_AGENT:
            raise WaitCongestedError(
                f"too many concurrent waits for this agent "
                f"(>{MAX_WAITS_PER_AGENT}) — back off and retry")
        _waits_per_agent[agent_id] = _waits_per_agent.get(agent_id, 0) + 1
        _waits_consumers.add(key)


def _release_wait(agent_id: str, consumer_id: str) -> None:
    key = (agent_id, consumer_id)
    with _permits_lock:
        _waits_per_agent[agent_id] -= 1
        if _waits_per_agent[agent_id] <= 0:
            del _waits_per_agent[agent_id]
        _waits_consumers.discard(key)


def wait(agent_id: str, *, consumer_id: str,
         cursor: Optional[int] = None,
         timeout: Optional[float] = None,
         pid=None,
         started: str = "", name: str = "") -> dict:
    """Long-poll the agent's event feed on behalf of one consumer.

    Returns {"events": [...], "cursor": int, "consumer_id": str,
    "waited": bool} — cursor is the position the client should ack on its
    next call once it has processed this batch.

    Raises ConsumerError on bad params, WaitCongestedError when the wait
    pool is full or this consumer already waits. Never raises for
    feed/registry I/O trouble — an empty result is honest there; a 500 is
    not.
    """
    consumer_id = (consumer_id or "").strip()
    if not CONSUMER_ID_RE.match(consumer_id):
        raise ConsumerError(
            "consumer_id is required (1-64 chars of [A-Za-z0-9._-], starting "
            "with alnum) — it names this consumer durably so redelivery can "
            "be detected across restarts")
    if cursor is not None and (not isinstance(cursor, int) or cursor < 0):
        raise ConsumerError("cursor must be a non-negative integer — it is "
                            "the line offset you have processed through")
    if timeout is None:
        timeout = DEFAULT_WAIT_TIMEOUT_SECONDS
    try:
        timeout = max(0.0, min(float(timeout), MAX_WAIT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        raise ConsumerError(f"timeout must be a number of seconds "
                            f"(0-{MAX_WAIT_TIMEOUT_SECONDS:g})")

    _acquire_wait(agent_id, consumer_id)
    try:
        return _wait_inner(agent_id, consumer_id, cursor=cursor,
                           timeout=timeout, pid=pid, started=started,
                           name=name)
    finally:
        _release_wait(agent_id, consumer_id)


def _wait_inner(agent_id: str, consumer_id: str, *, cursor: Optional[int],
                timeout: float, pid, started: str,
                name: str) -> dict:
    deadline = time.monotonic() + timeout
    liveness = _liveness(pid, started)
    # Read the feed once up front (mtime-cached, so cheap) — the ack clamp
    # and the poll loop both want it, and clamping BEFORE the first save
    # keeps a past-EOF cursor from ever reaching disk.
    lines = _read_alert_lines(agent_id)

    # Resolve positions ONCE before the poll loop; inside the loop we only
    # re-read the feed. A cursor in the request is an ack — clamp it to the
    # feed length and store it.
    with _registry_lock:
        consumers = _load_consumers(agent_id)
        if consumer_id not in consumers and \
                len(consumers) >= MAX_CONSUMERS_PER_AGENT:
            # LRU eviction — a per-process consumer_id mint habit can fill
            # the registry, but it must never lock the agent out forever.
            oldest = min(consumers,
                         key=lambda k: consumers[k].get("last_seen", 0.0))
            del consumers[oldest]
        rec = consumers.get(consumer_id) or {
            "acked": 0, "served": 0, "delivered": 0,
            "pid": "", "started": "", "name": "",
            "first_seen": time.time(), "last_seen": 0.0}
        # A reported triple differing from the stored one is a restart —
        # whichever direction the transition went (anonymous->identified,
        # identified->anonymous, or identified->different-identified): the
        # unacked backlog may have died with a different instance, so it
        # re-serves rather than silently continuing. Two fully anonymous
        # sides are 'same instance' — the honest limit of an optional field.
        restarted = (rec.get("pid", "") != liveness[0] or
                     rec.get("started", "") != liveness[1])
        if cursor is not None:
            rec["acked"] = max(int(rec.get("acked", 0)),
                               min(cursor, len(lines)))
        # Where this call serves from: an explicit ack position, the dead
        # instance's unacked backlog, or the live stream head.
        if cursor is not None:
            serve_from = rec["acked"]
        elif restarted:
            serve_from = int(rec.get("acked", 0))
        else:
            serve_from = int(rec.get("served", 0))
        rec["pid"], rec["started"] = liveness
        rec["name"] = (name or "")[:128]
        rec["last_seen"] = time.time()
        consumers[consumer_id] = rec
        try:
            _save_consumers(agent_id, consumers)
        except Exception:
            pass  # never raise for registry I/O — the feed still serves
        prior_served = int(rec.get("served", 0))
        acked = int(rec.get("acked", 0))

    waited = False
    while True:
        lines = _read_alert_lines(agent_id)
        if len(lines) > serve_from or time.monotonic() >= deadline:
            break
        waited = True
        time.sleep(POLL_INTERVAL_SECONDS)

    # The feed can only have shrunk if the file was rotated/truncated — a
    # cursor past EOF clamps forward rather than erroring; the consumer's
    # next ack re-anchors it.
    if serve_from > len(lines):
        serve_from = len(lines)

    batch = lines[serve_from:serve_from + WAIT_BATCH_LIMIT]
    since_write = alerts_since_last_write(agent_id)
    import alert_delivery  # event_for() — one place owns the type map
    events = []
    for i, line in enumerate(batch):
        idx = serve_from + i
        try:
            a = json.loads(line)
        except Exception:
            continue
        if not isinstance(a, dict):
            continue
        events.append(envelope(
            kind=alert_delivery.event_for(str(a.get("type", ""))),
            agent_id=agent_id,
            summary=str(a.get("message", ""))[:2000],
            timestamp=str(a.get("timestamp", "")),
            alert_type=str(a.get("type", "")),
            alerts_since_last_write=since_write,
            # Served-to-an-instance-before AND never acked = redelivered.
            redelivered=idx < prior_served and idx >= acked))
    new_served = max(prior_served, serve_from + len(batch))

    with _registry_lock:
        consumers = _load_consumers(agent_id)
        rec = consumers.get(consumer_id) or rec
        rec["served"] = new_served
        # An ack cursor can be typed past EOF — never store an acked above
        # the observed feed length or the consumer's redelivery floor is
        # permanently stranded beyond any real event.
        rec["acked"] = min(int(rec.get("acked", 0)), len(lines))
        rec["delivered"] = int(rec.get("delivered", 0)) + len(events)
        rec["last_seen"] = time.time()
        consumers[consumer_id] = rec
        try:
            _save_consumers(agent_id, consumers)
        except Exception:
            pass  # never raise for registry I/O — the feed still serves

    return {"events": events,
            "cursor": serve_from + len(batch),
            "consumer_id": consumer_id,
            "waited": waited,
            "_note": ("Pass cursor=<returned cursor> on your next call once "
                      "you have processed this batch — that is the ack. A "
                      "restarted consumer (same consumer_id, new pid/started) "
                      "receives its unacked events again with "
                      "redelivered:true.")}
