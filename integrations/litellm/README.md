# AgentLedger × LiteLLM — budget guardrail

**LiteLLM caps your LLM calls. AgentLedger caps everything else the agent
touches — and now it caps the LiteLLM calls too.**

One `CustomGuardrail` subclass that plugs into any LiteLLM proxy:

- **Before the provider is charged** (`pre_call`): asks the ledger
  `ledger_check_spend` — priced by model + estimated tokens — and raises to
  block the call when the budget says no.
- **After the call completes** (`post_call`): computes the real cost
  (`litellm.completion_cost`) and records it via `ledger_track` on the
  `api_key` rail, so LiteLLM spend sits in the same ledger as the agent's
  x402 / MPP / manual spend, under the same cap.

## Why not just LiteLLM budgets?

LiteLLM virtual-key budgets only see traffic that flows through that
gateway. If your agent also buys API calls, pays an x402 endpoint, or spends
on any non-LLM rail, those dollars are invisible to LiteLLM. This guardrail
gives the *agent* one budget across all of it — LiteLLM traffic is just the
rail it happens to ride today.

## Setup

1. **Mint a workspace + agent secret** on AgentLedger (one call, no signup):

   ```jsonc
   POST https://aiagentscity.com/mcp/
   {"method": "tools/call", "params": {"name": "ledger_start", "arguments": {}}}
   // → {"workspace_key": "wk_live_...", ...}
   ```

   First `ledger_track` for an `agent_id` mints its `agent_secret`.

2. **Set the agent's budget:**

   ```jsonc
   {"name": "ledger_set_budget", "arguments": {
     "agent_id": "agent-coder-1", "monthly_cap_cents": 5000,
     "workspace_key": "wk_live_..."}}
   ```

3. **Wire the guardrail** — copy `agentledger_guardrail.py` onto your proxy's
   path, add `config.example.yaml`'s `guardrails:` block to your LiteLLM
   `config.yaml`, set the env vars (`AGENTLEDGER_WORKSPACE_KEY`,
   `AGENTLEDGER_AGENT_SECRET`).

4. **Map identity → agent_id** — put `metadata.agentledger_agent_id` on the
   virtual key or team (see `config.example.yaml`); falls back to key
   alias / user id / team id automatically.

## Failure posture

`AGENTLEDGER_BLOCK_ON_ERROR=1` fails **closed** (ledger unreachable = call
blocked). Default fails **open** — a ledger outage should never take your LLM
gateway down; spend just goes unrecorded for that window.

## Files

| File | What |
|---|---|
| `agentledger_guardrail.py` | the CustomGuardrail class + tiny streamable-HTTP MCP client |
| `config.example.yaml` | proxy config snippet + env var reference |
