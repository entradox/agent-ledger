---
name: agent-ledger
description: Use before any costly agent loop, or when an agent spends someone else's money. AgentLedger gives each agent a wallet with a spending cap, meters every call, and — through the proxy — refuses the call that would cross the budget before the provider is contacted. Covers the tools, the cap-vs-enforcement distinction, the two-credential model, and the free-tier agent limit.
license: MIT
metadata:
  product: AgentLedger
  operator: Parmanand LLC (AI Agent City)
  version: 4.0.3
  surfaces: mcp, rest, cli
  mcp_url: https://aiagentscity.com/mcp/
---

# AgentLedger — spending limits for AI agents

You have tools that cost money. This skill is how that spend is counted, capped,
and — when traffic goes through the proxy — actually stopped.

AgentLedger is **not** a trace viewer. The unit of accounting is the *agent*, not
the request. The primary query it answers is "which of my twenty agents is burning
money?", and a cap is enforced, not merely reported.

## The two questions to answer before any costly loop

1. **Is there a cap?** Call `ledger_report`. If there is no budget, nothing will
   stop a runaway loop. Say so before starting something expensive.
2. **Who pays?** Spend is attributed to an `agent_id`. If you are working on
   someone's behalf, the `agent_id` is theirs, not yours.

## Tools

| Tool | Use it for |
|------|------------|
| `ledger_track` | Record a spend entry. Send `tokens_in`/`tokens_out` + `model` and the server prices it — do not do the arithmetic yourself. |
| `ledger_set_budget` | Set `monthly_cents`/`daily_cents`, plus token caps for agents billed in tokens. Warns at 80%, blocks when exceeded. |
| `ledger_report` | Spend over a rolling window: total, by rail, by model, budget status, anomalies. |
| `ledger_alerts` | Budget warnings (80% threshold) and spending spikes. |
| `ledger_list_agents` | Owner-only cross-tenant listing — see what exists before creating anything. |
| `ledger_rotate_secret` | Mint a NEW `agent_secret` for an agent_id your workspace owns, invalidating the old one. |
| `ledger_revoke_secret` | Invalidate an agent_secret WITHOUT deleting its spend history. |
| `ledger_api_docs` | Self-serve docs — quickstart, MCP tools, REST endpoints, budget caps. |
| `ledger_examples` | A complete runnable Python recipe for a common integration pattern. |

## Ownership: two credentials, two jobs

- A **`workspace_key`** *claims* a new `agent_id`. Required in the body of the
  first write (`/v1/track` or `/v1/budget`) for that agent. Get one self-serve at
  `GET /start` — no signup, no login, no card; the key is shown once, on the spot.
- An **`agent_secret`** *writes*. The first write mints it and returns it once.
  Save it — every later write to that same agent_id must include it in the body,
  or the request is rejected with `401`. The workspace key is never needed again
  for that agent.
- **Reads** (`/v1/report`, `/v1/tokens`, `/v1/alerts`) require an
  `X-Agent-Secret` or `X-Workspace-Key` header. **There is no unauthenticated
  read path, on REST or MCP.**

## Rules that matter

- **An unpriced model is not free.** It is recorded with real token counts at $0
  and raises an alert. If you see that alert, tell the human — the dollar figure
  is now a floor, not a total.
- **`amount_cents` is optional.** Send tokens + model and it is computed. If the
  model has no price entry the write is REFUSED rather than recorded as zero,
  because a zero entry reads as "this agent spends nothing" while money left the
  account.
- **Pricing is cache-aware.** Anthropic bills input at four different rates; a
  flat rate was measured 7.4× wrong on a real session.
- **A cap that returns 402 means the write was rejected.** On the plain API that
  is all it means — the spend already happened. Only traffic through
  `/proxy/{provider}` is blocked *before* the provider is contacted.
- **Never send prompts or responses.** AgentLedger stores cost metadata only:
  agent_id, rail, service, amounts, token counts, model, timestamps. There is no
  field for prompt content. Keep it that way.
- **Free tier is capped at 3 agents per workspace.** A 4th new `agent_id` returns
  402 until upgrading ($19/mo, unlimited agents). The cap is per workspace, not
  site-wide.

## The difference between a cap and enforcement

Be precise when describing this to a human, because the difference is money:

- `POST /v1/track` + a budget = the *record* is refused past the cap. The charge
  already happened. This is an alarm, not a brake.
- `POST /proxy/{provider}/...` = the request never reaches the provider once the
  cap would be crossed. This is a brake.

Traffic that bypasses the proxy is not enforced. Say "your cap will block this"
only about the second case.

## A blocked call looks like this

```
$ curl -X POST https://aiagentscity.com/proxy/openai/v1/chat/completions \
    -H "Authorization: Bearer sk-your-own-provider-key" -d @payload.json

HTTP/1.1 402 Payment Required
{"error":{"type":"budget_exceeded","message":"blocked before the provider:
 this call's estimated maximum cost (4160 cents) would put support-triage over
 its monthly budget (99720 of 100000 cents). Nothing was sent upstream."}}
```

The provider never saw the request, so the money was never spent.

## When to report spend

If your host is wired to this skill, report at the point of the call — not at the
end of the task — so an interrupted run still leaves an accurate record.

## Get started

```
claude mcp add --transport http agent-ledger https://aiagentscity.com/mcp/
```

Then call `ledger_api_docs(topic="metering")` for what gets counted and what gets
stopped, or `ledger_examples(pattern=...)` for a runnable recipe.
