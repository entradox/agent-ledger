---
name: agent-ledger
description: Use when you are spending someone's money — API calls, paid tools, or any agent run with a budget. Gives every agent a wallet with a spending limit, so runaway loops stop before they hit the card. Track spend per agent, read a live P&L, set caps, and route provider calls through the proxy so a cap actually blocks the charge.
---

# AgentLedger — spending limits for AI agents

You have tools that cost money. This skill is how that spend is counted, capped,
and — when traffic goes through the proxy — actually stopped.

## The two questions to answer before any costly loop

1. **Is there a cap?** Call `ledger_report` (or read the budget). If
   `budget_status` is missing, there is no cap and nothing will stop a runaway
   loop. Say so before starting something expensive.
2. **Who pays?** Spend is attributed to an `agent_id`. If you are working on
   someone's behalf, the agent_id is theirs, not yours.

## Tools

| Tool | Use it for |
|------|------------|
| `ledger_track` | Record a spend entry. Send `tokens_in`/`tokens_out` + `model` and the server prices it — do not do the arithmetic yourself. |
| `ledger_set_budget` | Set `monthly_cents` / `daily_cents`, and token caps for agents billed in tokens. |
| `ledger_report` | Current spend, by-service and by-model breakdown, budget status, anomalies. |
| `ledger_alerts` | Budget warnings (80%), rejections, anomalies, unpriced models. |
| `ledger_list_agents` | See what exists before creating anything. |
| `ledger_rotate_secret` / `ledger_revoke_secret` | Recover from a lost or leaked `agent_secret`. Workspace key only. |
| `ledger_api_docs` | Full API docs. Topic `metering` covers what gets counted and what gets stopped. |

## Rules that matter

- **An unpriced model is not free.** It is recorded with real token counts at $0
  and raises an alert. If you see that alert, tell the human — the dollar figure
  is now a floor, not a total.
- **`amount_cents` is optional.** Send tokens + model and it is computed. If the
  model has no price entry the write is REFUSED rather than recorded as zero,
  because a zero entry reads as "this agent spends nothing" while money left the
  account.
- **A cap that returns 402 means the write was rejected.** Before the proxy, that
  is all it means: the spend already happened. Only traffic through
  `/proxy/{provider}` is blocked *before* the provider is contacted.
- **Never send prompts or responses.** AgentLedger stores cost metadata only —
  agent_id, rail, service, amounts, token counts, model, timestamps. Keep it that
  way; do not paste conversation content into a tracking call.

## The difference between a cap and enforcement

Be precise when you describe this to a human, because the difference is money:

- `POST /v1/track` + a budget = the *record* is refused past the cap. The charge
  already happened. This is an alarm, not a brake.
- `POST /proxy/{provider}/...` = the request never reaches the provider once the
  cap would be crossed. This is a brake.

Traffic that bypasses the proxy is not enforced. Say "your cap will block this"
only about the second case.

## When to report spend

If Claude Code is wired to this skill, `Stop` reports the session automatically —
you do not need to track anything by hand. For work outside that path (a script,
another tool, a paid API call), track it at the point of the call, not at the end
of the task, so an interrupted run still leaves an accurate record.
