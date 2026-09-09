# AgentLedger — Per-Agent Spend Management

> Track AI agent spend across x402/MPP/API-key rails, set budget caps, catch
> spending anomalies, keep audit trails. The "Datadog for agent spending" —
> the #1 verified community ask from agent builders.

[![Live API](https://img.shields.io/badge/API-live-success)](https://agent-ledger-production-0ff8.up.railway.app/health)
[![Landing](https://img.shields.io/badge/status-page-blue)](https://agent-ledger-production-0ff8.up.railway.app/status)
[![MCP Registry](https://img.shields.io/badge/MCP-io.github.entradox%2Fagent--ledger-purple)](https://registry.modelcontextprotocol.io)
[![Version](https://img.shields.io/badge/version-0.3.0-blue)](https://agent-ledger-production-0ff8.up.railway.app/server.json)

## Connect AgentLedger

No OAuth, no API key, no signup — the first write for a new `agent_id`
mints its `agent_secret` right in the response. Point any MCP client at the
remote below and start tracking spend immediately.

**Claude Code:**
```bash
claude mcp add --transport http agent-ledger https://agent-ledger-production-0ff8.up.railway.app/mcp/
```

**Codex:**
```bash
codex mcp add agent-ledger --url https://agent-ledger-production-0ff8.up.railway.app/mcp/
```

**Cursor** — merge into `mcp.json`:
```json
{
  "mcpServers": {
    "agent-ledger": {
      "url": "https://agent-ledger-production-0ff8.up.railway.app/mcp/"
    }
  }
}
```

**Any other MCP client:** point it at the streamable-http remote
`https://agent-ledger-production-0ff8.up.railway.app/mcp/` — no headers,
no auth handshake required to connect.

**Try these prompts once connected:**
- "Track my Claude Code spend"
- "Alert when any agent exceeds $50/day"
- "Weekly P&L report"

## Try it in 30 seconds — no signup

```bash
# Track a spend — the FIRST call for a new agent_id returns an agent_secret.
# Save it: every later write (track/budget) to this agent_id must include it.
curl -X POST https://agent-ledger-production-0ff8.up.railway.app/v1/track \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"my-agent","rail":"x402","amount_cents":100,"service":"search_query"}'

# Set a monthly budget cap — pass the agent_secret from above
curl -X POST https://agent-ledger-production-0ff8.up.railway.app/v1/budget \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"my-agent","monthly_cents":5000,"agent_secret":"YOUR_SAVED_SECRET"}'

# Spend report + anomalies — open read, no secret needed
curl https://agent-ledger-production-0ff8.up.railway.app/v1/report/my-agent
```

Reads (`/v1/report`, `/v1/tokens`, `/v1/alerts`) never require a secret — only
writes to an `agent_id` do, and only after that `agent_id` has been claimed by
a first write. This is what stops a stranger from overwriting or corrupting
someone else's `agent_id`.

## Install (MCP clients)

```json
{
  "mcpServers": {
    "agent-ledger": {
      "url": "https://agent-ledger-production-0ff8.up.railway.app/mcp/"
    }
  }
}
```

Tools: `ledger_track`, `ledger_set_budget`, `ledger_report`, `ledger_alerts`,
`ledger_list_agents`, `ledger_api_docs`, `ledger_examples`.

Agent-facing API reference: [`llms.txt`](https://agent-ledger-production-0ff8.up.railway.app/llms.txt)
Runnable code recipes: [`recipes.md`](./recipes.md)

## Features

- **Multi-rail neutral** — x402, MPP, API keys, manual entries; not locked to one payment rail
- **Budget enforcement** — warns at 80% of cap, blocks spend when exceeded
- **Anomaly detection** — spending-spike alerts per agent
- **Audit trails** — every entry persisted with rail, service, and timestamp
- **Free during beta**

## Architecture

- Core engine (`ledger_engine.py`): per-agent JSONL ledgers, budget state, alert log,
  per-agent secret (`ensure_agent_secret` — claim-on-first-write, no signup)
- REST API (`api_server.py`, FastAPI): `/health`, `/v1/track`, `/v1/budget`,
  `/v1/report/{agent_id}`, `/v1/alerts/{agent_id}`, `/stats`,
  hosted streamable-http MCP at `/mcp/`
  - `/v1/agents` (full cross-tenant listing) is owner-only, requires `X-Al-Admin` header
- Input validation: agent_id restricted to `[A-Za-z0-9._-]{1,64}` (path-traversal safe),
  rail whitelisted to mpp/x402/api_key/manual, amount + budget caps bounded [0, $100k]
- Stripe webhook is fail-closed: events are rejected unless the HMAC signature
  verifies against `STRIPE_WEBHOOK_SECRET_AL`; a verified `pro` checkout sets
  `pro.flag` on the data volume, which lifts the free-tier agent cap for that
  instance (Pro $19/mo ⇒ unlimited agents).

## Contact

entradox@icloud.com