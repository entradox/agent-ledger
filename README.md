# AgentLedger — Per-Agent Spend Management

> Track AI agent spend across x402/MPP/API-key rails, set budget caps, catch
> spending anomalies, keep audit trails. The "Datadog for agent spending" —
> the #1 verified community ask from agent builders.

[![Live API](https://img.shields.io/badge/API-live-success)](https://agent-ledger-production-0ff8.up.railway.app/health)
[![Landing](https://img.shields.io/badge/status-page-blue)](https://agent-ledger-production-0ff8.up.railway.app/status)
[![MCP Registry](https://img.shields.io/badge/MCP-io.github.entradox%2Fagent--ledger-purple)](https://registry.modelcontextprotocol.io)

## Try it in 30 seconds — no signup

```bash
# Track a spend
curl -X POST https://agent-ledger-production-0ff8.up.railway.app/v1/track \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"my-agent","rail":"x402","amount_cents":100,"service":"search_query"}'

# Set a monthly budget cap
curl -X POST https://agent-ledger-production-0ff8.up.railway.app/v1/budget \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"my-agent","monthly_cents":5000}'

# Spend report + anomalies
curl https://agent-ledger-production-0ff8.up.railway.app/v1/report/my-agent
```

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
`ledger_list_agents`.

Agent-facing API reference: [`llms.txt`](https://agent-ledger-production-0ff8.up.railway.app/llms.txt)

## Features

- **Multi-rail neutral** — x402, MPP, API keys, manual entries; not locked to one payment rail
- **Budget enforcement** — warns at 80% of cap, blocks spend when exceeded
- **Anomaly detection** — spending-spike alerts per agent
- **Audit trails** — every entry persisted with rail, service, and timestamp
- **Free during beta**

## Architecture

- Core engine (`ledger_engine.py`): per-agent JSONL ledgers, budget state, alert log
- REST API (`api_server.py`, FastAPI): `/health`, `/v1/track`, `/v1/budget`,
  `/v1/report/{agent_id}`, `/v1/alerts/{agent_id}`, `/v1/agents`, `/stats`,
  hosted streamable-http MCP at `/mcp/`

## Contact

entradox@icloud.com