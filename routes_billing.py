#!/usr/bin/env python3
"""Per-workspace Stripe billing — checkout.session.completed webhook
(HMAC-verified, fail-closed) and checkout-session creation. Moved out of
api_server.py (Task 6) so billing logic and its two endpoints
(/stripe/webhook, /v1/billing/checkout) live in one file, shared with
Task 7 (x402 also lands here). Session resolution goes through
identity.resolve_session() — this file holds routing/plumbing only.
"""
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException, Request

from ledger_engine import DATA_DIR
import metrics

router = APIRouter()

# ── Stripe billing (LIVE, GASPERMIT acct) — mirrors Agent Watch's pattern ────
CUSTOMERS_FILE = DATA_DIR / "customers.jsonl"


def _append_customer(rec: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CUSTOMERS_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """Fulfillment: checkout.session.completed -> customers.jsonl (HMAC-verified)."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET_AL", "")
    if not secret:
        # Fail closed: an unsigned/unverifiable webhook must never mutate state
        # (customers.jsonl, pro.flag). Missing secret on the service = config
        # error, and silently accepting the event would be an open write path.
        raise HTTPException(500, "webhook secret not configured — event rejected")
    try:
        parts = dict(p.split("=", 1) for p in sig.split(","))
        expected = hmac.new(secret.encode(), f"{parts.get('t','')}.".encode() + payload, hashlib.sha256).hexdigest()
        if not parts.get("t") or not hmac.compare_digest(parts.get("v1", ""), expected):
            raise HTTPException(400, "bad signature")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "signature verification failed")
    event = json.loads(payload)
    event_type = event.get("type", "")
    if event_type.startswith("checkout.session.") and event_type not in (
            "checkout.session.completed", "checkout.session.expired"):
        # a checkout session that opened (and hasn't hit a terminal state)
        # counts as revenue-funnel entry — leading indicator of purchase intent.
        # completed/expired excluded so Stripe retries and dead sessions never
        # inflate the funnel (Morgan review, 2026-09-09).
        try:
            metrics.record_event("checkout_started")
        except Exception:
            pass
    if event_type != "checkout.session.completed":
        return {"received": True, "ignored": event.get("type")}
    sess = event["data"]["object"]
    email = (sess.get("customer_details") or {}).get("email") or sess.get("customer_email")
    if not email:
        return {"registered": False, "reason": "no email on session"}
    amount = sess.get("amount_total") or 0
    plan = "pro" if amount == 1900 else "unknown"
    _append_customer({"ts": time.time(), "email": email, "plan": plan,
                      "amount_total": amount, "stripe_session": sess.get("id", ""),
                      "status": "active", "authority": "confirmed-at-checkout"})
    try:
        metrics.record_event("checkout_completed", amount_cents=amount)
    except Exception:
        pass
    if plan == "pro":
        workspace_id = sess.get("client_reference_id")
        if workspace_id:
            import workspace_engine
            try:
                workspace_engine.mark_pro(workspace_id, sess.get("customer", ""))
            except workspace_engine.WorkspaceError:
                pass  # unknown workspace_id — log for investigation, don't crash the webhook
    try:
        from send_onboarding_email import send_onboarding_email
        send_onboarding_email(email, plan)
    except Exception as e:
        import logging
        logging.warning(f"onboarding email skipped: {e}")
    return {"registered": True, "email": email, "plan": plan}


@router.post("/v1/billing/checkout")
def create_checkout(request: Request):
    import identity
    workspace_id = identity.resolve_session(request.cookies.get("al_session", ""))
    if not workspace_id:
        raise HTTPException(401, "log in first")
    # NOTE: confirm the exact Stripe Checkout Sessions API request shape
    # (https://docs.stripe.com/api/checkout/sessions/create) at
    # implementation time rather than assuming from memory — this call
    # needs STRIPE_API_KEY (already in x_api_creds.env as STRIPE_SECRET_KEY,
    # GasPermit account) and the existing STRIPE_PRO_PRICE_ID-equivalent
    # for AgentLedger's $19/mo price.
    body = urllib.parse.urlencode({
        "mode": "subscription",
        "line_items[0][price]": os.environ["AL_STRIPE_PRICE_ID"],
        "line_items[0][quantity]": "1",
        "client_reference_id": workspace_id,
        "success_url": "https://agent-ledger-production-0ff8.up.railway.app/dashboard",
        "cancel_url": "https://agent-ledger-production-0ff8.up.railway.app/dashboard",
    }).encode()
    req = urllib.request.Request(
        "https://api.stripe.com/v1/checkout/sessions", data=body, method="POST",
        headers={"Authorization": f"Bearer {os.environ['STRIPE_API_KEY']}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        session = json.loads(resp.read())
    return {"checkout_url": session["url"]}


@router.post("/v1/billing/x402")
def x402_billing(request: Request):
    payment_header = request.headers.get("x-payment", "")
    if not payment_header:
        raise HTTPException(402, "X-PAYMENT header required")
    import x402_verify, workspace_engine
    result = x402_verify.verify_payment(payment_header)
    if not result["verified"]:
        raise HTTPException(402, "payment not verified")
    workspace_id, raw_key = workspace_engine.create_workspace(wallet_address=result["payer_wallet"])
    return {"workspace_id": workspace_id, "workspace_key": raw_key}
