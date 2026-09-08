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

BASE = "https://agent-ledger-production-0ff8.up.railway.app"

BODY = """You're on AgentLedger Pro.

1. Track spend (any agent, any rail — x402 / mpp / api_key / manual):

curl -X POST {base}/v1/track \\
  -H "Content-Type: application/json" \\
  -d '{{"agent_id":"my-agent","rail":"x402","amount_cents":100,"service":"search_query",
       "tokens_in":4500,"tokens_out":1200,"model":"gpt-4o"}}'

2. Set a budget cap (warns at 80%, blocks when exceeded):

curl -X POST {base}/v1/budget \\
  -H "Content-Type: application/json" \\
  -d '{{"agent_id":"my-agent","monthly_cents":5000}}'

3. Pull your reports:

Dollar spend:    curl {base}/v1/report/my-agent
Token burn:      curl {base}/v1/tokens/my-agent
Budget alerts:   curl {base}/v1/alerts/my-agent

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
writer-bot on the mpp rail" — it calls ledger_track automatically.

Note: all features are free during beta — your Pro status is recorded and
locked in for when GA pricing activates.

Questions? entradox@icloud.com
"""


def send_onboarding_email(email: str, plan: str = "pro") -> None:
    if not SMTP_PASS:
        raise RuntimeError("ICLOUD_SMTP_APP_PASSWORD not set — cannot send onboarding email")
    body = BODY.format(base=BASE)
    msg = MIMEText(body, "plain")
    msg["Subject"] = f"AgentLedger — you're on the {plan} plan"
    msg["From"] = SMTP_USER
    msg["To"] = email
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)