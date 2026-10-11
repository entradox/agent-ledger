# Event consumption for agents — the router contract (D-1625)

Who this is for: an agent (or a human wiring one up) that wants to *react* to
AgentLedger events, not just read them. Adapted from the router/worker split
recovered from the bolter-agent binary — their daemon puts a cheap,
stateless triage run in front of every message batch so a full work session
is never woken for "thanks". The same economics apply to alert streams.

## The two delivery paths

| You have | Use |
|---|---|
| A public URL you own | `POST /v1/webhooks` — we push, with retries and receipts at `GET /v1/webhooks/deliveries` |
| No URL (stdio MCP, laptop, cron) | `GET /v1/agents/{agent_id}/events/wait` or `ledger_wait_events` — you long-poll, we hold |

Both carry the same envelope — including the event `id`, so a consumer that
gets an alert over both paths dedupes on it once. The wait feed additionally
knows its consumer: give a durable `consumer_id`, report liveness, and ack
by passing the returned `cursor` once a batch is processed. On REST that is
`pid`/`started`; on hosted MCP `client_instance`/`client_started` (the
gateway cannot see your process — you opt in); on the stdio MCP server it
is automatic. Restart detection and `redelivered` semantics are documented
in `ledger_api_docs topic="events"`.

## The envelope

```json
{"id": "evt_…",                      // deterministic — dedupe without a round-trip
 "kind": "budget.warning",          // alert.raised | budget.warning | budget.exceeded | anomaly.detected
 "summary": "agent 'x' at 82% of monthly cap",
 "context": {"agent_id": "…", "alert_type": "…", "timestamp": "…"},
 "alerts_since_last_write": 3,      // unactioned streak length
 "redelivered": false}
```

## The router pattern

Do not point an event stream at your main session. Batch-triage first:

```
loop:
    batch = wait(consumer_id, cursor, timeout=25)
    verdict = cheap_run(batch)        # a small, stateless triage — see contract
    cursor = batch.cursor             # ack ONLY what you decided on
    dispatch(verdict.actions)
```

### The contract (adapted from bolter's `{actions:[…]}` schema)

The triage run does not do the work and keeps no conversation. It answers
each batch with a typed action list:

```json
{"actions": [
  {"kind": "ack",       "event_ids": ["evt_…"]},
  {"kind": "brief",     "event_ids": ["evt_…"],
   "brief": "who/what/the event ids/what the feed already said"},
  {"kind": "escalate",  "event_ids": ["evt_…"], "to": "human"}
]}
```

- `ack` — noted, nothing to do (a budget.warning you already acted on).
- `brief` — real work: hand a self-contained brief to a worker session —
  what happened, which agent, the event ids, what the context already tells
  it. Nobody reads the triage transcript; the brief must stand alone.
- `escalate` — the human decides this one (`budget.exceeded` on a paid tier,
  a first-ever anomaly). Do not silently auto-remediate money events.

### The throttle

`alerts_since_last_write` is the loop-breaker, carried in the data model
rather than bolted on in a prompt (bolter's `agentMessagesSincePerson`
serves the same purpose for agent-to-agent chat). If a consumer keeps
acking-and-doing-nothing while the streak grows, the router should raise
the bar for what becomes a `brief` — a feed that is always talking and never
acted on is a pager that learned to cry wolf.

### When you do NOT need a router

One agent, a handful of events a week, a human who reads the feed: poll
`/v1/alerts/{agent_id}` or read `ledger_alerts` directly. The wait+router
shape earns its complexity when events are frequent, when a session-wake
costs real tokens, or when multiple consumers share a feed.

## Honest limits

- `wait` serves the agent's *alert* feed today — `ledger_track` writes are
  not events (yet). If you need write-visibility, poll `/v1/report`.
- Cursor state lives server-side per `consumer_id`; it survives deploys but
  a consumer that abandons its id starts a fresh backlog read. The registry
  caps at 50 consumers per agent and evicts the least-recently-seen — reuse
  your id rather than minting one per process.
- Only one wait per `consumer_id` runs at a time — a second concurrent wait
  for the same consumer gets a typed 429, and each agent may hold at most 4
  concurrent waits (16 across the whole server).
- Reporting `pid`/`client_instance` without `started`/`client_started`
  counts as a new instance every call: at-least-once redelivery beats a
  reused pid faking continuity. Likewise, two processes that share one
  `consumer_id` and alternate *sequentially* each read as a restart and
  re-serve each other's unacked events — pick one id per process, or ack
  before switching.
- `alerts_since_last_write` resets on the agent's next ledger write — it is
  a streak counter, not a total.
