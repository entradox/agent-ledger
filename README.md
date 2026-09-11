# AgentLedger — Per-Agent Spend Management

> Track AI agent spend across x402/MPP/API-key rails, set budget caps, catch
> spending anomalies, keep audit trails. The "Datadog for agent spending" —
> the #1 verified community ask from agent builders.

[![Live API](https://img.shields.io/badge/API-live-success)](https://agent-ledger-production-0ff8.up.railway.app/health)
[![Landing](https://img.shields.io/badge/status-page-blue)](https://agent-ledger-production-0ff8.up.railway.app/status)
[![MCP Registry](https://img.shields.io/badge/MCP-io.github.entradox%2Fagent--ledger-purple)](https://registry.modelcontextprotocol.io)
[![Version](https://img.shields.io/badge/version-0.3.0-blue)](https://agent-ledger-production-0ff8.up.railway.app/server.json)

> 🎯 **Launch window: the first 50 workspaces created get Pro free for 1 year.** No card — sign in once and the grant is automatic.

## Connect AgentLedger

Connecting takes no auth handshake. Before your first write you need a
**workspace_key**, which you get one of two ways:

- **Autonomous agent with a wallet:** `POST /v1/billing/x402` with an
  `X-PAYMENT` header. The paying wallet becomes the workspace identity —
  no email, no login, no human in the loop.
- **Human owner:** sign in with Google at
  [`/login`](https://agent-ledger-production-0ff8.up.railway.app/login) — the
  key is shown once, right there. Requires Google OAuth to be configured on
  the deployment; if it isn't, `/login` returns 503 `login_not_configured`.

The key claims new `agent_id`s. Each claim mints that agent's own
`agent_secret`, which is what authenticates every later write — the
workspace_key is never needed again for that agent.

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

## Try it — get a key, then track

```bash
# Track a spend — the FIRST call for a new agent_id CLAIMS it, so it needs
# your workspace_key, and it returns that agent's agent_secret.
# Save the secret: every later write (track/budget) to this agent_id uses it
# instead, and needs no workspace_key.
curl -X POST https://agent-ledger-production-0ff8.up.railway.app/v1/track \
  -H "Content-Type: application/json" \
  -H "AL-API-Version: 2026-09-01" \
  -d '{"agent_id":"my-agent","rail":"x402","amount_cents":100,"service":"search_query","workspace_key":"YOUR_WORKSPACE_KEY"}'

# Set a monthly budget cap — pass the agent_secret from above
curl -X POST https://agent-ledger-production-0ff8.up.railway.app/v1/budget \
  -H "Content-Type: application/json" \
  -H "AL-API-Version: 2026-09-01" \
  -d '{"agent_id":"my-agent","monthly_cents":5000,"agent_secret":"YOUR_SAVED_SECRET"}'

# Spend report + anomalies — requires the agent_secret from above
curl https://agent-ledger-production-0ff8.up.railway.app/v1/report/my-agent \
  -H "X-Agent-Secret: YOUR_SAVED_SECRET"
```

**Want to just look at it?** Every agent has a human-readable page (send the
secret as a header, no curl needed for the JSON):
`https://agent-ledger-production-0ff8.up.railway.app/v1/report/my-agent/html`

`/v1/report`, `/v1/tokens`, and `/v1/alerts` all require either
`X-Agent-Secret: <agent's secret>` or `X-Workspace-Key: <the workspace's key>`
— missing or wrong credential gets 401. A logged-in dashboard session also
authorizes reads for that session's own workspace, which is how the agent
links on `/dashboard` work in a browser. This is what stops a stranger from
reading or overwriting someone else's `agent_id`.

Note this is a deliberate break from AgentLedger's earlier "share the link,
anyone can read it" behavior — traded for real isolation between customers.

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
- **Per-workspace isolation** — your agents, your cap, your subscription; nothing is shared across customers
- **Free tier: 3 agents per workspace** (first 50 workspaces get Pro free for 1 year) —
  **Pro $19/mo** for unlimited tracked agents: [Get Pro](https://buy.stripe.com/14AbJ0clUeoE9QN3Nl2400e)

## Architecture

- Core engine (`ledger_engine.py`): per-agent JSONL ledgers, budget state, alert log,
  per-agent secret (`ensure_agent_secret` — a new claim requires a `workspace_key`,
  and mints the agent's own `agent_secret` for every write after it)
- Identity (`identity.py`): the single place a caller is resolved — `agent_secret`,
  `workspace_key`, or a signed session cookie. Every gate (claim, read, session,
  billing) calls into it rather than rolling its own check.
- Workspaces (`workspace_engine.py`): per-customer identity + plan state, keyed by
  Google `sub` (human owner) or wallet address (x402, no human)
- REST API (`api_server.py`, FastAPI): app creation, meta endpoints and router
  includes only; endpoints live in `routes_agents.py`, `routes_billing.py`,
  `routes_auth.py`. Hosted streamable-http MCP at `/mcp/`
  - `/v1/agents` (full cross-tenant listing) is owner-only, requires `X-Al-Admin` header
  - Reads (`/v1/report`, `/v1/tokens`, `/v1/alerts`) require `X-Agent-Secret` or
    `X-Workspace-Key`; the MCP read tools take the same two credentials as
    parameters, so neither surface has an unauthenticated read path
- Input validation: agent_id restricted to `[A-Za-z0-9._-]{1,64}` (path-traversal safe),
  rail whitelisted to mpp/x402/api_key/manual, amount + budget caps bounded [0, $100k]
- Stripe webhook is fail-closed: events are rejected unless the HMAC signature
  verifies against `STRIPE_WEBHOOK_SECRET_AL`; a verified `pro` checkout marks
  **that workspace** Pro via its `client_reference_id` (Pro $19/mo ⇒ unlimited
  agents for that workspace only).

## Contact

entradox@icloud.com