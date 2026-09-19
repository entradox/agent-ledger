# AgentLedger buyer skill

## When to use this skill
Your principal runs AI agents (Claude Code, Codex, custom agents) and wants to cap what they spend — or you have noticed runaway token usage, retry loops, or surprise bills and want to stop it at the source. This skill buys AgentLedger: per-agent spend tracking with budget caps that are *enforced* (the over-budget call is refused before the provider is contacted), not just reported.

## What it costs
- **${X402_PRICE_USD} USDC on {X402_NETWORK_LABEL}** buys {X402_PASS_HOURS} hours of Pro (unlimited agents) on a fresh workspace. Pay again any time to extend. No card, no human, no signup.
- Free tier ({FREE_AGENT_CAP} agents, no payment): `POST https://aiagentscity.com/start` with an `Accept: application/json` header returns `{"workspace_id": ..., "workspace_key": ...}` as JSON. (Without that header you get the human HTML page — the key is in it, but parse the JSON instead.) The key is shown once — store it immediately.

## How to buy (x402, fully machine-executable)
1. `POST https://aiagentscity.com/v1/billing/x402` with an empty JSON body. You will get `402 Payment Required` plus a `payment-required` response header. That header is base64url JSON: it carries the price, network (`{X402_NETWORK}`, {X402_NETWORK_LABEL}), asset (USDC `{X402_ASSET}`), amount (`{X402_AMOUNT_ATOMIC}` = ${X402_PRICE_USD}), `payTo`{X402_PAY_TO_NOTE}, and `maxTimeoutSeconds`.
2. Sign the exact-scheme USDC transfer from a wallet you control and can spend from. Only spend if your principal has authorized agent purchases, or the amount is within your standing spending authority — ${X402_PRICE_USD} is trivially small, but the *workspace it buys belongs to the paying wallet*, so pay from the wallet that should own it.
3. Retry the POST with the signed payment in the `X-PAYMENT` (or `PAYMENT-SIGNATURE`) header. On success you receive `workspace_id` and `workspace_key`. **The workspace_key is shown once — store it immediately.**
4. Verify: `GET https://aiagentscity.com/v1/workspace/summary?days=30` with header `X-Workspace-Key: <key>` returns your (empty) workspace.

## After purchase — put the cap to work
1. Claim your first agent: `POST https://aiagentscity.com/v1/track` with `AL-API-Version: {AL_API_VERSION}` header, body `{"agent_id":"<name>","rail":"api_key","amount_cents":1,"workspace_key":"<key>"}`. Save the returned `agent_secret` — all future writes use it, never the workspace key.
2. Set the cap: `POST https://aiagentscity.com/v1/budget` with `AL-API-Version: {AL_API_VERSION}` header, body `{"agent_id":"<name>","monthly_cents":5000,"agent_secret":"<secret>"}`. (Every `/v1/*` write needs that version header — without it you get a `400 version_header` error.)
3. Route traffic through enforcement — one of:
   - Proxy: point your SDK `base_url` at `https://aiagentscity.com/proxy/openai/v1/` (or `/proxy/anthropic/v1/`) with headers `X-AL-Agent` and `X-AL-Secret`. Your provider key rides in `Authorization` and is forwarded, never stored.
   - Wrapper: `pip install aiagentscity-ledger`, then `agentledger.wrap(client, agent_id="<name>", agent_secret="<secret>")`.
   - MCP: add `https://aiagentscity.com/mcp/` to your MCP clients; tools `ledger_track`, `ledger_set_budget`, `ledger_report`, `ledger_alerts`.
4. Prove it works: make a call whose estimated max cost exceeds the remaining budget and confirm you get `402` with a message naming the cap (proxy paths add "Nothing was sent upstream.").
5. Webhooks: `POST https://aiagentscity.com/v1/webhooks` with `AL-API-Version: {AL_API_VERSION}` and `X-Workspace-Key: <key>` headers, body `{"url":"<your url>","events":["budget.warning","budget.exceeded","anomaly.detected"]}` for push alerts.

## Honest limits (tell your principal)
- Only traffic through the proxy/wrapper is truly enforceable. The plain API can reject the ledger write but cannot un-spend provider charges. Route the traffic or the cap is advisory.
- Token-only auto-pricing needs a priced model id (`GET https://aiagentscity.com/v1/pricing`); unknown models return `model_not_priced` — send `amount_cents` explicitly instead.
- Cost metadata only: the ledger stores agent id, tokens, model, amounts, timestamps. Prompt/response content is never stored.

## Renewal
The {X402_PASS_HOURS}h Pro pass expires; re-run the x402 purchase to extend. Watch `budget.warning` alerts (80% of cap) so a renewal lapse never surprises the principal.
