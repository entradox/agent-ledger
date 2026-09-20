# Founder gap spec — AI Agent City (INTERNAL)

> Moved here 2026-09-20: this page was accidentally published at
> aiagentscity.com/spec. It is internal planning material and must never be
> served publicly. Public surfaces are stranger-clean, external-facing only.

Method: benchmarked vs openrouter.ai, typesafe.ai, skyfire.xyz (2026-09-18).
Priorities: P0 = this week · P1 = this month · P2 = next.

## Site-wide gaps

### P0 — Metrics-with-receipts hero
They lead with proof, we lead with prose. TypeSafe opens with "193.6x faster,
444.6x cheaper" with a (proof) link and a side-by-side video; OpenRouter opens
with 400T+ tokens, 10M+ users. Spec: the "Live, not slides" strip becomes
three metrics, each linking its receipt — x402 settlement → BaseScan tx hash;
402 enforcement → dated live-test output; 7.4× pricing → the real session data.
No claim without a receipt link.

### P0 — "Built on AI Agent City" agent wall
OpenRouter ships "Featured Agents" (250k+ apps, 4.2M users — Replit, Kilo Code,
notably Hermes Agent). Our own agent is featured on their wall and absent from
ours. Spec: ecosystem wall on the homepage — start with agents we run ourselves
(Hermes), then open submissions: name, what it does, which products it uses.

### P0 — Homepage onboarding strip
OpenRouter: Signup → Buy credits → Get API key on the homepage. Spec: homepage
strip — 1. Get a workspace key (one press, shown once) → 2. Connect via MCP
(copy-paste) → 3. Set a cap (one number). Each step copy-pasteable in place.

### P1 — The Agent Economy Index
OpenRouter's model rankings are a destination; nobody ranks the agent economy.
Spec: public anonymized index at /index — agents tracked, USDC settled via x402,
median cost per 1k agent calls, week-over-week trends. Updated daily.

### P1 — Human billing page
Spec: /billing — card checkout for Pro ($19/mo), invoices, credit balance,
cancel in one click. The agent rail stays; the human rail stops being embarrassing.

### P1 — Key management UI
Spec: dashboard → Keys: list, label, rotate, revoke. Losing a key stops being
a support ticket.

### P1 — Skeptic-grade FAQ per product
Spec: real FAQ on /agent-ledger and each product page — "What if my agent
bypasses the proxy?" (answered: then it's not enforced — we say so), "Is the
$0.01 x402 price subsidized?", "What do you store about my prompts?" (nothing —
and here's the test that proves it).

### P1 — /docs, not just llms.txt
Spec: /docs — Python + TypeScript + curl quickstarts per product, data policy,
rate limits, uptime.

### P2 — /blog with a pulse
Spec: weekly ship notes, benchmark posts, agent-economy analysis from our own
index. The changelog is the log; the blog is the story.

### P2 — Community
Spec: agent-developer Discord, featured-agent submission flow feeding the
homepage wall, monthly "what agents built" roundup.

### P2 — /status in the nav
Spec: footer + developers page link it; add per-product uptime.

## Per-product gaps

### AgentLedger (vs OpenRouter · Skyfire budgets)
- TypeScript SDK — wrapper is Python-only; ship aiagentscity-ledger for TS.
- Budget templates — one-click packs ("Researcher: $50/mo", "Support triage: $200/mo").
- Native Slack/Discord alerts — webhooks exist; one-click integrations don't.
- Bypass detection — warn when an agent's traffic stops hitting the proxy.

### Agent Watch (vs Skyfire KYA)
- The connect page it promises — "launching soon" has expired. Ship it.
- Agent identity cards — portable identity card per caller.
- Weekly email digest.

### Perimeter Watch
- Client-ready PDF reports — one-click "send the client a report".
- Scheduled scans + history.
- Hosted dashboard — a URL the agency owner can open without an agent.

### TrustScan
- Public scan page — paste a domain or agent ID, get a verdict with evidence.
- Embeddable trust badge.
- Pre-transaction API — one call, verdict + evidence, built to gate x402 payments.

### Cited (vs OpenRouter rankings)
- Shareable report links.
- Visibility over time — trend charts ("your AI visibility is +18% this month").
- The fix list — the three actions that change it, not just diagnosis.
