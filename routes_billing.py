#!/usr/bin/env python3
"""Per-workspace Stripe billing — checkout.session.completed webhook
(HMAC-verified, fail-closed) and checkout-session creation. Moved out of
api_server.py (Task 6) so billing logic and its two endpoints
(/stripe/webhook, /v1/billing/checkout) live in one file, shared with
Task 7 (x402 also lands here). Workspace identity resolution goes
through identity.resolve_workspace_key() — this file holds routing/plumbing
only. The Google-session arm was removed in D-1162: checkout is authenticated
by the workspace's own key, and the payment URL carries client_reference_id so
the webhook upgrades the right workspace.
"""
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ledger_engine import DATA_DIR, error_envelope
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
    if event_type == "checkout.session.expired":
        # Backstop, not a real-time signal: Stripe expires an unpaid session
        # ~24h after it is created, so an abandonment recorded here is up to a
        # day old. The client beacon (start_checkout_click) is what catches the
        # click itself; this catches the case where the buyer never came back.
        sess = (event.get("data") or {}).get("object") or {}
        try:
            metrics.record_onboarding("checkout_abandoned",
                                      sess.get("client_reference_id") or "")
        except Exception:
            pass
        return {"received": True, "ignored": event_type}

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
        metrics.record_onboarding("checkout_completed",
                                  sess.get("client_reference_id") or "",
                                  amount_cents=amount)
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


@router.get("/v1/billing/{email}")
def billing_status(email: str, token: str = ""):
    """Customer plan lookup — OWNER-ONLY (Opus audit round 3: was an open
    email-enumeration oracle). Token = HMAC-SHA256("billing:<email>",
    AL_ADMIN_SECRET), truncated to 32 hex chars; the operator computes it,
    customers never see billing state of other emails."""
    import hashlib as _h, secrets as _secrets, hmac as _hmac
    admin_secret = os.environ.get("AL_ADMIN_SECRET", "")
    # keyed digest: sha256(secret || email) truncated to 128 bits — constant-time compare
    expected_token = _h.sha256(admin_secret.encode() + b"billing:" + email.lower().encode()).hexdigest()[:32] if admin_secret else ""
    if not (admin_secret and token and expected_token) or not _secrets.compare_digest(token, expected_token):
        raise HTTPException(401, "owner only (billing status is not public)")
    for line in (CUSTOMERS_FILE.read_text().splitlines() if CUSTOMERS_FILE.exists() else []):
        try:
            r = json.loads(line)
            if r.get("email", "").lower() == email.lower() and r.get("status") == "active":
                return {"email": email, "plan": r.get("plan"), "status": "active"}
        except Exception:
            continue
    return {"email": email, "plan": "beta", "status": "free_during_beta"}


@router.post("/v1/billing/checkout")
def create_checkout(request: Request):
    """Checkout URL for an existing workspace, authenticated by its own
    workspace_key.

    Re-keyed off the retired Google session (D-1162). The old version read an
    `al_session` cookie, which meant the product's only human purchase path
    required a login that the deployment could not serve — and it needed
    STRIPE_API_KEY / AL_STRIPE_PRICE_ID, neither of which was set, so the
    route could never have worked here.

    No Stripe secret is needed: the URL is the existing live payment link
    with client_reference_id appended. Stripe echoes that value back on
    checkout.session.completed (docs.stripe.com/payment-links/url-parameters),
    which is exactly the field this module's webhook reads to call
    workspace_engine.mark_pro(). No API key, no session, no Google.
    """
    import identity
    workspace_id = (request.headers.get("x-workspace-id") or "").strip()
    raw_key = (request.headers.get("x-workspace-key") or "").strip()
    if not workspace_id or not raw_key:
        raise HTTPException(401, detail=error_envelope(
            401, "send the workspace's own credentials as X-Workspace-Id and "
                 "X-Workspace-Key (mint a workspace at POST /start, or by paying "
                 "at POST /v1/billing/x402)", code="workspace_key_required"))
    if identity.resolve_workspace_key(raw_key) != workspace_id:
        raise HTTPException(401, detail=error_envelope(
            401, "workspace_key does not match that workspace_id",
            code="agent_secret_mismatch"))
    link = os.environ.get("AL_STRIPE_PAYMENT_LINK",
                          "https://buy.stripe.com/14AbJ0clUeoE9QN3Nl2400e")
    return {"checkout_url": f"{link}?client_reference_id={workspace_id}",
            "workspace_id": workspace_id}


@router.post("/v1/billing/x402")
def x402_billing(request: Request):
    """Mint (or resolve) a workspace from a settled x402 payment.

    The settlement transaction hash is the idempotency key, per spec 3b —
    implemented on the SAME idempotency store /v1/track uses
    (idempotency_begin/store/release in ledger_engine), not a second
    bespoke mechanism. Consequences:

    - Replaying the SAME tx_hash returns the same workspace_id, but the
      raw workspace_key is REDACTED from the cached replay. The key is
      shown exactly once, in the original response; a replay must not
      re-expose it to anyone who can name the tx_hash. Same precedent as
      /v1/track and /v1/budget, which strip the minted agent_secret from
      their cached payloads for the same reason.
    - A NEW tx_hash from an ALREADY-KNOWN wallet (a second real payment)
      resolves to that wallet's existing workspace and returns
      workspace_key: null — a key was already issued for this wallet and
      only its hash is stored, so it cannot be re-shown. Nothing is
      invalidated (the previous behavior silently reissued, breaking the
      key the agent was already using).
    """
    from ledger_engine import (idempotency_begin, idempotency_store,
                               idempotency_release, error_envelope,
                               IdempotencyKeyTooLongError, IdempotencyConflictError)
    import x402_verify, workspace_engine
    try:
        result = x402_verify.verify_payment(request)
    except x402_verify.X402Unavailable as e:
        raise HTTPException(503, f"x402 not available on this service: {e}")
    except Exception as e:
        raise HTTPException(502, f"x402 payment processing failed: {str(e)[:120]}")
    if not result["verified"]:
        # The SDK's own response (402 + PAYMENT-REQUIRED header listing price,
        # network and pay_to) is what discovery clients need; forward it
        # verbatim rather than flattening it to a bare 402 string.
        unpaid = result.get("unpaid_response")
        if unpaid:
            return JSONResponse(status_code=unpaid["status"],
                                content=unpaid.get("body") or {},
                                headers=unpaid.get("headers") or {})
        raise HTTPException(402, "payment not verified")
    wallet = result.get("payer_wallet")
    if not wallet:
        # Same defensive shape as the tx_hash guard below. A settlement with
        # no payer is an identity we cannot key a workspace to; attempting it
        # anyway surfaced as an unhandled sqlite3.IntegrityError -> 500
        # instead of a typed, actionable error.
        raise HTTPException(402, detail=error_envelope(
            402, "payer wallet missing from settlement — no identity to bind "
                 "a workspace to, refusing to mint", code="x402_no_payer_wallet"))
    # Settlement sanity: a verified payment of ANY size to ANY recipient must
    # not mint a workspace. `recipient` is the pay_to on the PaymentRequirements
    # the SDK verified the payment against — the one field whose meaning is
    # unambiguous. Defence in depth: under the real SDK that pay_to comes from
    # this service's own X402_PAY_TO config, so a mismatch means the route and
    # the treasury config have drifted apart. X402_RECEIVING_ADDRESS defaults
    # to X402_PAY_TO so the two can never silently diverge when an operator
    # only sets one (the common case) — set X402_RECEIVING_ADDRESS explicitly
    # only if it must differ from X402_PAY_TO for a real reason.
    expected_recipient = os.environ.get("X402_RECEIVING_ADDRESS") or os.environ.get("X402_PAY_TO", "")
    if expected_recipient:
        recipient = result.get("recipient")
        if not recipient or str(recipient).lower() != expected_recipient.lower():
            raise HTTPException(402, detail=error_envelope(
                402, "settlement recipient does not match this service's "
                     "receiving address — refusing to mint",
                code="x402_recipient_mismatch"))
    tx_hash = result.get("tx_hash")
    if not tx_hash:
        # Without a settlement tx_hash there is no replay key, so a resubmit
        # could not be told apart from a fresh payment. Fail closed rather
        # than mint on an undedupable payment.
        raise HTTPException(402, detail=error_envelope(
            402, "settlement transaction hash missing — payment cannot be "
                 "deduplicated, refusing to mint", code="x402_no_tx_hash"))

    try:
        status, cached = idempotency_begin(tx_hash, wallet, "x402_mint")
    except IdempotencyKeyTooLongError as e:
        raise HTTPException(400, detail=error_envelope(
            400, str(e), code="idempotency_key_too_long"))
    except IdempotencyConflictError as e:
        raise HTTPException(409, detail=error_envelope(
            409, str(e), code="idempotency_conflict"))
    if status == "cached":
        return JSONResponse(status_code=cached["status_code"],
                            content=cached["response"])

    try:
        workspace_id, raw_key = workspace_engine.create_workspace(wallet_address=wallet)
    except Exception:
        idempotency_release(tx_hash, wallet, "x402_mint")
        raise

    payload = {"workspace_id": workspace_id, "workspace_key": raw_key}
    if raw_key is None:
        payload["message"] = (
            "A workspace_key was already issued for this wallet and is shown "
            "only once at mint time — it cannot be re-issued in this version. "
            "This payment resolved to your existing workspace.")
    # SECURITY: the raw workspace_key must never be persisted in the
    # idempotency cache — a replayable cached response would re-expose it to
    # anyone who can name the settlement tx_hash. Same rule (and same shape)
    # as the minted agent_secret on /v1/track and /v1/budget.
    cached_payload = dict(payload)
    cached_payload.pop("workspace_key", None)
    cached_payload["_note"] = ("workspace_key is shown once at claim time and is "
                               "not included in cached (idempotent) replays.")
    idempotency_store(tx_hash, wallet, "x402_mint", cached_payload, 200)
    # Echo the SDK's PAYMENT-RESPONSE settlement headers so the paying client
    # can see its receipt (tx hash, network) alongside the minted workspace.
    return JSONResponse(status_code=200, content=payload,
                        headers=result.get("settlement_headers") or {})
