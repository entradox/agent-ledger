# AgentLedger — Per-Agent Spend Management

> Track AI agent spend across x402/MPP/API-key rails, set budget caps, catch
> spending anomalies, keep audit trails. The "Datadog for agent spending" —
> the #1 verified community ask from agent builders.

[![Live API](https://img.shields.io/badge/API-live-success)](https://aiagentscity.com/health)
[![Landing](https://img.shields.io/badge/status-page-blue)](https://aiagentscity.com/status)
[![MCP Registry](https://img.shields.io/badge/MCP-io.github.entradox%2Fagent--ledger-purple)](https://registry.modelcontextprotocol.io)
[![Version](https://img.shields.io/badge/version-0.4.1-blue)](https://aiagentscity.com/server.json)

> 🎯 **Free tier: 3 agents per workspace, no card, no login.** `POST /start` mints the workspace and shows the key once.

## Connect AgentLedger

Connecting takes no auth handshake. Before your first write you need a
**workspace_key**, which you get one of two ways:

- **Autonomous agent with a wallet:** `POST /v1/billing/x402` with an
  `X-PAYMENT` header. The paying wallet becomes the workspace identity —
  no email, no login, no human in the loop.
- **Human:** open
  [`/start`](https://aiagentscity.com/start) — no
  signup, no login, no card. The workspace_key is shown once, right there, and
  the page carries the upgrade link for that workspace.

The key claims new `agent_id`s. Each claim mints that agent's own
`agent_secret`, which is what authenticates every later write — the
workspace_key is never needed again for that agent.

**Claude Code:**
```bash
claude mcp add --transport http agent-ledger https://aiagentscity.com/mcp/
```

**Codex:**
```bash
codex mcp add agent-ledger --url https://aiagentscity.com/mcp/
```

**Cursor** — merge into `mcp.json`:
```json
{
  "mcpServers": {
    "agent-ledger": {
      "url": "https://aiagentscity.com/mcp/"
    }
  }
}
```

**Any other MCP client:** point it at the streamable-http remote
`https://aiagentscity.com/mcp/` — no headers,
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
curl -X POST https://aiagentscity.com/v1/track \
  -H "Content-Type: application/json" \
  -H "AL-API-Version: 2026-09-01" \
  -d '{"agent_id":"YOUR_AGENT_ID","rail":"x402","amount_cents":100,"service":"search_query","workspace_key":"YOUR_WORKSPACE_KEY"}'

# Set a monthly budget cap — pass the agent_secret from above
curl -X POST https://aiagentscity.com/v1/budget \
  -H "Content-Type: application/json" \
  -H "AL-API-Version: 2026-09-01" \
  -d '{"agent_id":"YOUR_AGENT_ID","monthly_cents":5000,"agent_secret":"YOUR_SAVED_SECRET"}'

# Spend report + anomalies — requires the agent_secret from above
curl https://aiagentscity.com/v1/report/YOUR_AGENT_ID \
  -H "X-Agent-Secret: YOUR_SAVED_SECRET"
```

**Want to just look at it?** Every agent has a human-readable page (send the
secret as a header, no curl needed for the JSON):
`https://aiagentscity.com/v1/report/YOUR_AGENT_ID/html`

`/v1/report`, `/v1/tokens`, and `/v1/alerts` all require either
`X-Agent-Secret: <agent's secret>` or `X-Workspace-Key: <the workspace's key>`
— missing or wrong credential gets 401. Headers only: there is no cookie or
session arm, and no unauthenticated read path on REST or MCP. This is what
stops a stranger from reading or overwriting someone else's `agent_id`.

Note this is a deliberate break from AgentLedger's earlier "share the link,
anyone can read it" behavior — traded for real isolation between customers.

## Two lines to a metered, capped agent

```bash
pip install "aiagentscity-ledger[wrapper]"      # client only: the wrapper + the CLI
agent-ledger init --agent YOUR_AGENT_ID       # mints a workspace, claims the agent, writes .env
```

```python
from openai import OpenAI
import agentledger

client = agentledger.wrap(OpenAI(api_key=OPENAI_KEY),
                          agent_id="YOUR_AGENT_ID", agent_secret="<from .env>")
```

That is the whole integration. `wrap()` repoints the client's base URL at the proxy and
adds two identity headers — it does not patch or subclass the SDK, so streaming, tool
calls and retries behave exactly as before. Anthropic's client works identically. Your
provider key is forwarded untouched and never stored.

Not using a Python SDK? Point any OpenAI- or Anthropic-compatible client at
`<base>/proxy/openai/v1/` (or `/proxy/anthropic`) with `X-AL-Agent` and `X-AL-Secret`.

## Install (MCP clients)

```json
{
  "mcpServers": {
    "agent-ledger": {
      "url": "https://aiagentscity.com/mcp/"
    }
  }
}
```

Tools: `ledger_track`, `ledger_set_budget`, `ledger_report`, `ledger_alerts`,
`ledger_list_agents`, `ledger_api_docs`, `ledger_examples`.

Agent-facing API reference: [`llms.txt`](https://aiagentscity.com/llms.txt)
Runnable code recipes: [`recipes.md`](./recipes.md)

## Features

- **Multi-rail neutral** — x402, MPP, API keys, manual entries; not locked to one payment rail
- **Token→cost auto-pricing** — send tokens + model and the dollar amount is computed for you;
  an unpriced model is refused rather than silently recorded as free
- **Budget enforcement** — warns at 80% of cap, rejects the ledger write that would cross it
- **Any provider, config not code** — `providers.json` lists the vendors the proxy can forward
  to (OpenAI, Anthropic, DeepSeek and Moonshot ship as examples); adding one is a config edit
- **Proxy enforcement (the real thing)** — point your provider `base_url` at `/proxy/{provider}`
  and a call that would cross the cap is refused *before* the provider is contacted, so the money
  is never spent. Pass-through: your provider key is forwarded, never stored
- **Anomaly detection** — spending-spike alerts per agent
- **Alerts that arrive** — push budget warnings (80%), real budget blocks, and
  anomalies to an http(s) webhook you own; every delivery attempt is receipted,
  failures included
- **Audit trails** — every entry persisted with rail, service, and timestamp
- **Shareable report links** — `POST /v1/report/{agent_id}/share` mints a read-only,
  expiring URL that opens in a plain browser (no header, no credential); revoke the
  lot in one call
- **Credential recovery** — a lost `agent_secret` never bricks an agent_id: the
  workspace_key mints a replacement (`POST /v1/agents/{agent_id}/rotate-secret`) or
  revokes it while keeping the spend history
- **Per-workspace isolation** — your agents, your cap, your subscription; nothing is shared across customers
- **Free tier: 3 agents per workspace** —
  **Pro $19/mo** for unlimited tracked agents: [Get Pro](https://buy.stripe.com/14AbJ0clUeoE9QN3Nl2400e)

## Architecture

- Core engine (`ledger_engine.py`): per-agent JSONL ledgers, budget state, alert log,
  per-agent secret (`ensure_agent_secret` — a new claim requires a `workspace_key`,
  and mints the agent's own `agent_secret` for every write after it)
- Identity (`identity.py`): the single place a caller is resolved — `agent_secret`
  or `workspace_key`, both header-borne. Every gate (claim, read, billing) calls
  into it rather than rolling its own check.
- Workspaces (`workspace_engine.py`): per-customer identity + plan state, keyed by
  wallet address (x402, no human) or by nothing at all for a plain `POST /start`
  workspace
- REST API (`api_server.py`, FastAPI): app creation, meta endpoints, the human
  start/buy pages and router includes; endpoints live in `routes_agents.py` and
  `routes_billing.py`. Hosted streamable-http MCP at `/mcp/`
  - `/v1/agents` (full cross-tenant listing) is owner-only, requires `X-Al-Admin` header
  - Reads (`/v1/report`, `/v1/tokens`, `/v1/alerts`) require `X-Agent-Secret` or
    `X-Workspace-Key`; the MCP read tools take the same two credentials as
    parameters, so neither surface has an unauthenticated read path
  - Credential lifecycle: `POST /v1/agents/{agent_id}/rotate-secret` and
    `/revoke-secret` are **workspace_key-only**. An agent's own `agent_secret`
    deliberately cannot rotate itself — if it could, a leaked agent credential
    would permanently lock its real owner out. Revoke overwrites the stored
    secret rather than deleting it, so the agent_id stays claimed and cannot be
    picked up by another workspace
- Input validation: agent_id restricted to `[A-Za-z0-9._-]{1,64}` (path-traversal safe),
  rail whitelisted to mpp/x402/api_key/manual, amount + budget caps bounded [0, $100k]
- Stripe webhook is fail-closed: events are rejected unless the HMAC signature
  verifies against `STRIPE_WEBHOOK_SECRET_AL`; a verified `pro` checkout marks
  **that workspace** Pro via its `client_reference_id` (Pro $19/mo ⇒ unlimited
  agents for that workspace only).

## Contact

entradox@icloud.com