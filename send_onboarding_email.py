#!/usr/bin/env python3
"""AgentLedger onboarding email — sent after a Stripe checkout completes.

iCloud SMTP (env: ICLOUD_SMTP_HOST/PORT/USER/ICLOUD_SMTP_APP_PASSWORD).
Caller (stripe_webhook) wraps in try/except — mail failure never breaks fulfillment.
"""
import os
import smtplib
from email.mime.text import MIMEText

SMTP_HOST = os.environ.get("ICLOUD_SMTP_HOST", "smtp.mail.me.com")
SMTP_PORT = int(os.environ.get("ICLOUD_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("ICLOUD_SMTP_USER", "entradox@icloud.com")
SMTP_PASS = os.environ.get("ICLOUD_SMTP_APP_PASSWORD", "")

BASE = "https://aiagentscity.com"

BODY = """You're on the AgentLedger {plan} plan.

{plan} lifts the 3-agent cap: track up to {agents} agents on this workspace (the
cap is skipped automatically on this instance from the moment your checkout
completed).

1. Track spend (any agent, any rail — x402 / mpp / api_key / manual):

curl -X POST {base}/v1/track \\
  -H "Content-Type: application/json" \\
  -d '{{"agent_id":"YOUR_AGENT_ID","rail":"x402","amount_cents":100,"service":"search_query",
       "tokens_in":4500,"tokens_out":1200,"model":"gpt-4o"}}'

The FIRST call for a new agent_id returns an "agent_secret" in the
response. Save it — every later write to that agent_id must include it
(or the write is rejected with 401). Reads (report/tokens/alerts) also
require a credential — the agent_secret or your workspace_key, sent as the
X-Agent-Secret / X-Workspace-Key header.

2. Set a budget cap (warns at 80%, blocks spend that would cross it):

curl -X POST {base}/v1/budget \\
  -H "Content-Type: application/json" \\
  -d '{{"agent_id":"YOUR_AGENT_ID","monthly_cents":5000,"agent_secret":"YOUR_SAVED_SECRET"}}'

3. Pull your reports:

Dollar spend:    curl {base}/v1/report/YOUR_AGENT_ID
Token burn:      curl {base}/v1/tokens/YOUR_AGENT_ID
Budget alerts:   curl {base}/v1/alerts/YOUR_AGENT_ID

4. Wire the MCP server into any MCP client (Claude, Cursor) — paste into your
MCP config file:

{{
  "mcpServers": {{
    "agent-ledger": {{
      "url": "{base}/mcp/"
    }}
  }}
}}

Then just ask your agent in plain language: "Track a $3.50 spend for
writer-bot on the mpp rail" — it calls ledger_track automatically (same
agent_secret rules apply).

Questions? support@aiagentscity.com
"""


def send_onboarding_email(email: str, plan: str = "starter") -> None:
    if not SMTP_PASS:
        raise RuntimeError("ICLOUD_SMTP_APP_PASSWORD not set — cannot send onboarding email")
    # D-1441 CLOSURE (2026-09-26). This body used to hardcode a "Pro" greeting and an
    # unbounded agents promise for EVERY payer, while the webhook actually grants
    # `starter` (10 agents) to a $19 settlement. So a $19 customer received, in writing,
    # a promise of unlimited agents that the product then enforced against: their 11th
    # agent claim is refused. The subject line already said "starter" (it interpolates
    # `plan`), so the email contradicted ITSELF as well. The banned strings are listed
    # by pattern in tests/test_tier_label_truth.py rather than quoted here, because
    # quoting them in this comment would trip that guard.
    #
    # The ladder is a code fact, not copy: free = 3 (workspace_engine.WORKSPACE_FREE_AGENT_CAP),
    # starter = 10 (STARTER_AGENT_CAP), team = 50 (TEAM_AGENT_CAP), and only the x402
    # "pro" pass is unbounded. The "Pro" name is reserved for that pass, so using it for
    # a subscription is the specific mislabel this fix removes.
    #
    # The default was "pro" for the same reason and is now "starter" — the cheapest paid
    # plan, so a caller that omits the argument understates rather than overstates. An
    # overstatement here is a promise emailed to a paying customer.
    agents = {"starter": "10", "team": "50", "pro": "unlimited"}.get(plan, "10")
    body = BODY.format(base=BASE, plan=plan.capitalize(), agents=agents)
    msg = MIMEText(body, "plain")
    msg["Subject"] = f"AgentLedger — you're on the {plan} plan"
    msg["From"] = SMTP_USER
    msg["To"] = email
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)