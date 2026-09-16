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

Fulfillment covers BOTH immediate and delayed payment methods:

  - Immediate (card, Klarna, Cash App Pay, Link): the session arrives with
    payment_status="paid" and `checkout.session.completed` fulfills it.
  - Delayed (US bank account / ACH Direct Debit, Boleto, SEPA, vouchers):
    the session arrives with payment_status="unpaid" and is fulfilled days
    later by `checkout.session.async_payment_succeeded`. That event MUST be
    handled — Stripe: "Automatic fulfillment with webhooks is required if you
    sell subscriptions or accept payment methods with delayed success
    notification." Without it, a bank-debit buyer's money settles and they
    are never upgraded.

The live payment link enables US bank, so the delayed path is reachable today,
not hypothetical.
"""
import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ledger_engine import DATA_DIR, error_envelope
import metrics

router = APIRouter()

# ── Stripe billing (LIVE, GASPERMIT acct) — mirrors Agent Watch's pattern ────
CUSTOMERS_FILE = DATA_DIR / "customers.jsonl"
GRANT_FAILURES_FILE = "grant_failures.jsonl"

# Stripe's own limit for client_reference_id (docs.stripe.com/payment-links/
# url-parameters). Values above it are silently dropped by Stripe, but a
# correctly-signed body can carry anything, and the value becomes a filesystem
# path component — so it must be bounded before it reaches mark_pro().
MAX_CLIENT_REFERENCE_ID = 200

# A workspace_id is only ever "ws_" + token_urlsafe(16). Validating the shape
# before the value is used as a path component is what stops an absolute or
# traversing client_reference_id from escaping the workspaces directory, since
# pathlib.__truediv__ discards the left operand on an absolute right operand.
_WORKSPACE_ID_RE = re.compile(r"ws_[A-Za-z0-9_-]{1,200}")


def _append_customer(rec: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CUSTOMERS_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _record_grant_failure(reason: str, session_id: str, email: str,
                          detail: str = "", workspace_id: str = "") -> None:
    """Record a paid-but-not-granted event so it can be found and repaired.

    The failure mode this exists for: a customer pays $19, and for any reason
    the workspace is never upgraded. Before this, the code did `pass` — the
    payment was simply lost with no trace. Money in, plan out, nothing in the
    logs.

    Written to its own file rather than customers.jsonl on purpose: a grant
    failure is NOT a customer. Mixing it in would let a failed upgrade inflate
    the customer count, which is the same class of error as the synthetic
    checkout events scrubbed on 2026-09-14.

    Must never raise. This runs inside a webhook that Stripe is waiting on; an
    exception here would turn a repairable bookkeeping miss into a 500 and
    trigger endless Stripe retries.
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": time.time(),
            "reason": reason,
            "stripe_session": session_id,
            "email": email,
            "workspace_id": workspace_id,
            "detail": detail,
            "revenue_impact_cents": 1900,
            "repaired": False,
        }
        with open(DATA_DIR / GRANT_FAILURES_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass
    # Also surface in the onboarding funnel under a reserved id, so it is
    # visible in /v1/metrics without anyone having to know this file exists.
    try:
        metrics.record_onboarding("grant_failed", "__grant_failure__",
                                  reason=reason, stripe_session=session_id)
    except Exception:
        pass


def resolve_paying_workspace(sess: dict, email: str) -> str | None:
    """Return the workspace_id a settled session should upgrade, or None.

    None means the payment cannot be attributed, and a recorded
    `missing_client_reference_id` failure has already been written. Bounds and
    shape are enforced here rather than at the call sites so the immediate and
    delayed paths cannot diverge.
    """
    workspace_id = sess.get("client_reference_id")
    if workspace_id is None or str(workspace_id).strip() == "":
        _record_grant_failure("missing_client_reference_id",
                              sess.get("id", ""), email)
        return None
    workspace_id = str(workspace_id).strip()
    if len(workspace_id) > MAX_CLIENT_REFERENCE_ID:
        # Reaches the filesystem as a path component; an over-long value raises
        # OSError ENAMETOOLONG, which is not a WorkspaceError and used to escape
        # as a 500 with endless Stripe retries.
        _record_grant_failure("client_reference_id_too_long",
                              sess.get("id", ""), email,
                              f"len={len(workspace_id)}")
        return None
    # Shape check, defence in depth behind workspace_engine._ws_path(), which
    # also validates. An absolute or traversing value here would otherwise be
    # used as a path component by mark_pro(), escaping the workspaces dir —
    # Path.__truediv__ discards the left operand on an absolute right operand.
    # A workspace_id is only ever ws_<token>, so anything else is refused.
    if not _WORKSPACE_ID_RE.fullmatch(workspace_id):
        _record_grant_failure("client_reference_id_not_a_workspace_id",
                              sess.get("id", ""), email,
                              f"shape rejected (len={len(workspace_id)})")
        return None
    return workspace_id


def fulfill_paid_session(sess: dict, *, source: str) -> dict:
    """Grant entitlement for a session whose money has actually settled.

    Called from `checkout.session.completed` when payment_status is settled, and
    from `checkout.session.async_payment_succeeded` when a delayed method
    settles later. One implementation on purpose: the delayed path is exactly
    where a second copy would drift and silently stop granting.

    Never raises. Stripe is waiting on this response, and a deterministic
    failure must not become an endless retry loop.
    """
    email = (sess.get("customer_details") or {}).get("email") or sess.get("customer_email")
    if not email:
        # No email means no way to contact the buyer or reconcile them. Record
        # it rather than returning silently — this is still money we took.
        _record_grant_failure("no_email_on_settled_session",
                              sess.get("id", ""), "",
                              f"settled via {source}",
                              sess.get("client_reference_id") or "")
        return {"received": True, "granted": False, "reason": "no email on session"}

    amount = sess.get("amount_total") or 0
    plan = "pro" if amount == 1900 else "unknown"

    _append_customer({"ts": time.time(), "email": email, "plan": plan,
                      "amount_total": amount,
                      "stripe_session": sess.get("id", ""),
                      "payment_status": str(sess.get("payment_status") or "unknown"),
                      "status": "active",
                      "authority": "confirmed-at-checkout",
                      "fulfilled_via": source})
    try:
        metrics.record_event("checkout_completed", amount_cents=amount)
        metrics.record_onboarding("checkout_completed",
                                  sess.get("client_reference_id") or "",
                                  amount_cents=amount)
    except Exception:
        pass

    granted = False
    if plan == "pro":
        workspace_id = resolve_paying_workspace(sess, email)
        if workspace_id:
            import workspace_engine
            # D-1269: write a clock. `pro_until=None` means "never expires", so
            # granting Pro with no deadline is what made a $19/month subscription
            # permanent. The checkout session does not carry the subscription's
            # period end, so a conservative backstop is used until a lifecycle
            # event supplies the exact value — invoice.paid for
            # billing_reason=subscription_create arrives within seconds and
            # corrects it. The backstop is deliberately slightly GENEROUS
            # (BILLING_PERIOD_BACKSTOP_SECONDS > one month) because expiring a
            # paying customer early is far worse than one extra day of access.
            subscription_id = sess.get("subscription") or ""
            try:
                # Computed inside the try: an unexpected payload shape here must
                # not escape as a 500, because Stripe retries a 5xx forever.
                period_end = _period_end_from(sess) or (
                    time.time() + BILLING_PERIOD_BACKSTOP_SECONDS)
                workspace_engine.mark_pro(
                    workspace_id, sess.get("customer", ""),
                    stripe_subscription_id=subscription_id,
                    pro_until=float(period_end),
                    period_source="checkout.session.completed")
                granted = True
            except Exception as exc:
                # An unresolvable workspace_id used to be swallowed with `pass`,
                # silently converting a real payment into a non-account — the
                # worst possible failure for a $19 subscription.
                #
                # Catches Exception, not just WorkspaceError: an adversarial
                # pass found a bad path component raises OSError
                # (ENAMETOOLONG), which the narrow catch could never contain.
                _record_grant_failure("mark_pro_failed",
                                      sess.get("id", ""), email, str(exc),
                                      workspace_id)
    else:
        # Money settled but not at the Pro price. Do not grant, and do not be
        # silent about it.
        _record_grant_failure("unexpected_amount",
                              sess.get("id", ""), email,
                              f"amount_total={amount}",
                              sess.get("client_reference_id") or "")

    try:
        from send_onboarding_email import send_onboarding_email
        send_onboarding_email(email, plan)
    except Exception as e:
        import logging
        logging.warning(f"onboarding email skipped: {e}")

    return {"received": True, "granted": granted, "plan": plan,
            "fulfilled_via": source}


# ── D-1269: Stripe subscription lifecycle ───────────────────────────────────
# Principal decision (2026-09-15): cancellation is PERIOD-END — a customer who
# cancels keeps Pro until the end of the period they already paid for. A failed
# renewal gets a GRACE window before downgrade, so a transient card problem
# never removes access from someone who is trying to pay.

#: How long a failed renewal keeps Pro before the workspace is downgraded.
#: Chosen to cover Stripe's own smart retries (default 4 attempts over ~3 weeks
#: is longer; 7 days covers the common transient-decline case) while bounding
#: how long a genuinely lapsed customer keeps being served for free.
RENEWAL_GRACE_SECONDS = 7 * 24 * 60 * 60

#: Backstop period written at checkout when the session carries no period end.
#: Deliberately a month plus slack: a billing period is one month, and the exact
#: value arrives on the following invoice.paid (billing_reason=subscription_create
#: or subscription_cycle) within seconds. Too short and a paying customer loses
#: access before their renewal is recorded; slightly generous costs at most a day.
BILLING_PERIOD_BACKSTOP_SECONDS = 32 * 24 * 60 * 60


def _period_end_from(obj: dict) -> int:
    """The paid-period end (unix seconds) from a Stripe subscription/invoice.

    Stripe moved this field: modern API versions put it on the subscription ITEM
    (`items.data[].current_period_end`), older ones on the subscription itself
    (`current_period_end`). Both are checked — reading only one silently yields
    0 on the other, which would write a period end of 1970 and instantly
    downgrade a paying customer. Returns 0 when genuinely absent.
    """
    for key in ("current_period_end", "period_end"):
        v = obj.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    items = ((obj.get("items") or {}).get("data")) or []
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("current_period_end", "period_end"):
            v = item.get(key)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
    return 0


def _resolve_lifecycle_workspace(obj: dict, customer_id: str) -> Optional[str]:
    """Find the workspace a lifecycle event refers to.

    Subscription and invoice payloads carry a CUSTOMER id and usually no
    workspace id — the `client_reference_id` that made checkout attribution work
    is not echoed on them. So the lookup order is:
      1. an explicit client_reference_id, when the payload has one;
      2. the by_stripe_customer index, written by mark_pro();
      3. a workspace whose stored owner_email matches the customer's email
         (covers a period when the index was not yet written).
    Returns None when it cannot be resolved; callers decide the consequence.
    """
    wid = resolve_paying_workspace(
        {"client_reference_id": obj.get("client_reference_id") or ""}, "")
    if wid:
        return wid
    if customer_id:
        import workspace_engine
        rec = workspace_engine.get_workspace_by_stripe_customer(customer_id)
        if rec:
            return rec["workspace_id"]
    email = (obj.get("customer_email")
             or (obj.get("customer_details") or {}).get("email") or "")
    if email:
        import workspace_engine
        for rec in workspace_engine.iter_workspaces():
            if (rec.get("owner_email") or "").lower() == email.lower():
                return rec["workspace_id"]
    return None


def _handle_subscription_lifecycle(event_type: str, event: dict) -> Optional[dict]:
    """Dispatch the post-purchase subscription events. None = not ours.

    Never raises: Stripe retries a 5xx forever, and an unhandled exception here
    would turn one malformed event into an endless delivery loop.
    """
    obj = (event.get("data") or {}).get("object") or {}
    customer_id = obj.get("customer") or ""

    if event_type == "customer.subscription.deleted":
        # PERIOD-END CANCELLATION. Stripe sends this when a subscription ends —
        # both when a customer cancels and when it lapses. The customer already
        # paid for the current period, so they keep Pro until it actually ends:
        # honour the period, do not revoke on the event.
        wid = _resolve_lifecycle_workspace(obj, customer_id)
        if not wid:
            _record_grant_failure("cancel_unresolved", obj.get("id", ""), "",
                                  "subscription.deleted: no workspace for customer",
                                  customer_id)
            return {"received": True, "revoked": False,
                    "reason": "workspace unresolved"}
        period_end = _period_end_from(obj)
        import workspace_engine
        try:
            if period_end:
                workspace_engine.set_pro_period(
                    wid, float(period_end), source=event_type,
                    reason="cancelled — access runs to the paid period end")
            else:
                # No period end in the payload: keep Pro rather than guess a
                # deadline and cut off a paying customer early (fail open).
                pass
            metrics.record_event("subscription_cancelled")
            metrics.record_onboarding("subscription_cancelled", wid)
        except Exception as exc:
            _record_grant_failure("cancel_failed", obj.get("id", ""), "",
                                  str(exc), wid)
            return {"received": True, "revoked": False, "reason": str(exc)[:80]}
        return {"received": True, "revoked": False,
                "pro_until": period_end or None, "workspace_id": wid}

    if event_type == "customer.subscription.updated":
        # A plan change, a cancellation scheduled at period end, or a status
        # change. Keep pro_until in step with what Stripe says the period is, so
        # a subscription that is extended or shortened in the dashboard is
        # reflected here instead of drifting from the record.
        status = (obj.get("status") or "").lower()
        wid = _resolve_lifecycle_workspace(obj, customer_id)
        if not wid:
            return {"received": True, "updated": False,
                    "reason": "workspace unresolved"}
        period_end = _period_end_from(obj)
        import workspace_engine
        try:
            if status in ("active", "trialing"):
                workspace_engine.set_pro_period(
                    wid, float(period_end) if period_end else None,
                    source=event_type, reason=f"status={status}")
            elif status in ("canceled", "unpaid", "incomplete_expired"):
                # Already ended (not merely scheduled to). Honour the period
                # end if Stripe gives one, else revoke now.
                if period_end and period_end > time.time():
                    workspace_engine.set_pro_period(
                        wid, float(period_end), source=event_type,
                        reason=f"status={status}, runs to period end")
                else:
                    workspace_engine.revoke_pro(wid, reason=f"status={status}",
                                                source=event_type)
        except Exception as exc:
            _record_grant_failure("subscription_updated_failed",
                                  obj.get("id", ""), "", str(exc), wid)
            return {"received": True, "updated": False, "reason": str(exc)[:80]}
        return {"received": True, "updated": True, "status": status,
                "workspace_id": wid}

    if event_type == "invoice.paid":
        # RENEWAL vs INITIAL. The first invoice is recorded by
        # checkout.session.completed; counting it again here would double the
        # revenue figure. Only a subscription_cycle invoice is new money.
        reason = obj.get("billing_reason") or ""
        wid = _resolve_lifecycle_workspace(obj, customer_id)
        amount = obj.get("amount_paid") or 0
        if wid:
            import workspace_engine
            period_end = _period_end_from(obj)
            try:
                # A successful payment clears any grace deadline and moves the
                # clock to the new period.
                rec = workspace_engine.get_workspace(wid)
                if rec and rec.get("plan") == "pro":
                    workspace_engine.set_pro_period(
                        wid, float(period_end) if period_end else None,
                        source=event_type, reason=f"billing_reason={reason}")
                    # Clear a grace marker so a recovered customer reads as
                    # healthy rather than as still-failing.
                    if str(rec.get("pro_period_reason") or "").startswith("payment_failed"):
                        workspace_engine.clear_grace(wid)
            except Exception as exc:
                _record_grant_failure("invoice_paid_failed", obj.get("id", ""),
                                      "", str(exc), wid)
        if reason == "subscription_cycle" and amount:
            # New recurring money. This is the line that makes months 2..N
            # visible; before D-1269 they were recorded nowhere.
            try:
                metrics.record_event("subscription_renewed",
                                     amount_cents=int(amount))
                if wid:
                    metrics.record_onboarding("subscription_renewed", wid,
                                              amount_cents=int(amount))
            except Exception:
                pass
        return {"received": True, "renewal": reason == "subscription_cycle",
                "amount_paid": amount, "workspace_id": wid or None}

    if event_type == "invoice.payment_failed":
        # GRACE, not an immediate cut-off. A declined card is usually transient
        # and Stripe retries; revoking on the first failure would remove access
        # from a customer who is actively trying to pay.
        wid = _resolve_lifecycle_workspace(obj, customer_id)
        if not wid:
            _record_grant_failure("payment_failed_unresolved",
                                  obj.get("id", ""), "",
                                  "invoice.payment_failed: no workspace for customer",
                                  customer_id)
            return {"received": True, "grace": False,
                    "reason": "workspace unresolved"}
        import workspace_engine
        # Grace must never SHORTEN a period the customer already paid for. If
        # their paid period runs beyond the grace window, the later of the two
        # wins — otherwise a payment failure would remove access the customer
        # had already bought, which is worse than the leak it is closing.
        grace_until = time.time() + RENEWAL_GRACE_SECONDS
        try:
            rec = workspace_engine.get_workspace(wid)
            existing = (rec or {}).get("pro_until")
            if isinstance(existing, (int, float)) and existing > grace_until:
                grace_until = float(existing)
                grace_reason = ("payment_failed — grace shorter than the paid "
                                "period, keeping the paid period")
            else:
                grace_reason = "payment_failed — grace window, Pro retained"
            workspace_engine.set_pro_period(
                wid, grace_until, source=event_type, reason=grace_reason)
            metrics.record_event("subscription_payment_failed")
            metrics.record_onboarding("subscription_payment_failed", wid)
        except Exception as exc:
            _record_grant_failure("payment_failed_grace_failed",
                                  obj.get("id", ""), "", str(exc), wid)
            return {"received": True, "grace": False, "reason": str(exc)[:80]}
        return {"received": True, "grace": True, "workspace_id": wid,
                "grace_until": int(grace_until),
                "grace_days": RENEWAL_GRACE_SECONDS // 86400}

    return None


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """Stripe fulfillment endpoint. HMAC-verified and fail-closed.

    Handles three event families:
      - checkout.session.<opened>  -> revenue-funnel leading indicator
      - checkout.session.expired   -> abandonment backstop
      - checkout.session.completed -> fulfill IF the money has settled
      - checkout.session.async_payment_succeeded -> fulfill the delayed case
    """
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

    # A correctly-signed but unparseable body must not become an unhandled 500.
    # Stripe would retry a 5xx forever, and a 500 masks the real cause. The
    # signature already proved the sender holds the secret, so a malformed body
    # is a bug or a truncated delivery, not an attack: answer 400 and stop.
    try:
        event = json.loads(payload)
    except Exception:
        raise HTTPException(400, "malformed JSON payload")
    if not isinstance(event, dict):
        raise HTTPException(400, "payload is not a JSON object")
    event_type = event.get("type", "")

    if event_type.startswith("checkout.session.") and event_type not in (
            "checkout.session.completed", "checkout.session.expired",
            "checkout.session.async_payment_succeeded",
            "checkout.session.async_payment_failed"):
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

    if event_type == "checkout.session.async_payment_succeeded":
        # The delayed-method settlement. Stripe: "Automatic fulfillment with
        # webhooks is required if you sell subscriptions or accept payment
        # methods with delayed success notification." A US-bank (ACH) buyer's
        # money lands here days after checkout; without this branch they pay and
        # are never upgraded.
        sess = (event.get("data") or {}).get("object") or {}
        return fulfill_paid_session(sess, source=event_type)

    if event_type == "checkout.session.async_payment_failed":
        # The delayed payment bounced. No entitlement was ever granted, so
        # there is nothing to revoke — record it so the churn is visible.
        sess = (event.get("data") or {}).get("object") or {}
        _record_grant_failure("async_payment_failed",
                              sess.get("id", ""),
                              (sess.get("customer_details") or {}).get("email", ""),
                              "delayed payment method failed to settle",
                              sess.get("client_reference_id") or "")
        return {"received": True, "granted": False, "reason": "payment failed"}

    if event_type != "checkout.session.completed":
        # ── D-1269 subscription lifecycle ──────────────────────────────────
        # Every event below was previously dropped by this one line, which is
        # why a cancellable, renewable $19/month subscription behaved as a
        # one-time charge: cancellation, failed renewal and renewal were all
        # silently ignored. Dispatched before the fall-through.
        lifecycle = _handle_subscription_lifecycle(event_type, event)
        if lifecycle is not None:
            return lifecycle
        return {"received": True, "ignored": event.get("type")}

    sess = event["data"]["object"]
    email = (sess.get("customer_details") or {}).get("email") or sess.get("customer_email")
    if not email:
        return {"registered": False, "reason": "no email on session"}
    amount = sess.get("amount_total") or 0
    plan = "pro" if amount == 1900 else "unknown"

    # `completed` is NOT the same as `paid`. Stripe: "A completed checkout
    # session does not mean that there was a successful payment" - the event
    # carries the money's real state in payment_status (one of `paid`,
    # `unpaid`, `no_payment_required`). With an async method (bank debit,
    # vouchers, Boleto, SEPA) the session completes with `unpaid` and settles
    # days later via checkout.session.async_payment_succeeded.
    #
    # Only an EXPLICIT `unpaid` blocks fulfillment. A missing/unknown value is
    # treated as settled, which is deliberate: payment_status is always present
    # on a real session, so the only way to reach that branch is a malformed or
    # unexpected payload - and failing closed there would mean taking a paying
    # customer's money and handing them nothing. Blocking on the positive
    # evidence of non-payment closes the async hole without risking a false
    # negative on the normal card path.
    payment_status = str(sess.get("payment_status") or "").lower()
    if payment_status == "unpaid":
        # Awaiting settlement. Do NOT grant and do NOT write a customer row —
        # the async_payment_succeeded handler above will do both when the money
        # actually lands. Recorded so the open sale is visible in the meantime.
        _record_grant_failure("payment_not_settled",
                              sess.get("id", ""), email,
                              f"payment_status={payment_status}",
                              sess.get("client_reference_id") or "")
        return {"received": True, "granted": False,
                "reason": "payment not settled", "payment_status": payment_status}

    return fulfill_paid_session(sess, source="checkout.session.completed")


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
                 "at POST /v1/billing/x402)",
            code="workspace_key_required"))
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
            body = unpaid.get("body")
            if not body:
                # The SDK's payment-required envelope is carried in the
                # PAYMENT-REQUIRED *header*, so an unauthenticated caller with
                # an empty or malformed request body got back literally `{}` —
                # a 402 that explains nothing. Keep the header (it is what a
                # discovery client parses) and add the same typed envelope every
                # other endpoint returns, so a human reading the response knows
                # what to do and a generic error handler recognises the shape.
                note = (result.get("error")
                        or "payment required — retry with a signed x402 payment "
                           "header (PAYMENT-SIGNATURE or X-PAYMENT); the "
                           "PAYMENT-REQUIRED response header carries the price, "
                           "network and pay_to")
                body = error_envelope(unpaid["status"], note,
                                      error_type="payment_required_error",
                                      code="payment_required")
            return JSONResponse(status_code=unpaid["status"],
                                content=body,
                                headers=unpaid.get("headers") or {})
        raise HTTPException(402, detail=error_envelope(
            402, result.get("error") or "payment not verified",
            code="payment_not_verified"))
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

    # Grant the Pro pass this settlement paid for. Every verified, deduplicated
    # settlement reaches this line exactly once (a replayed tx_hash returned
    # the cached response above and never gets here), so each real payment
    # buys its own 24h window — paying again resets the clock forward rather
    # than stacking, which matches "buy a day pass" rather than "bank time."
    workspace_engine.mark_pro(workspace_id, "",
                              pro_until=time.time() + x402_verify.X402_PRO_PASS_SECONDS,
                              period_source="x402_pass")

    # amount is USDC atomic units (6 decimals) on the settlement the SDK
    # verified against — convert to cents for the same amount_cents field
    # checkout_completed already reports, so both payment rails roll up
    # together. Never let a malformed/missing amount break a real settlement.
    try:
        amount_cents = round(int(result.get("amount")) / 10_000)
    except (TypeError, ValueError):
        amount_cents = None
    event_kwargs = {"amount_cents": amount_cents} if amount_cents is not None else {}
    metrics.record_event("x402_settled", **event_kwargs)
    if raw_key is not None:
        # A genuinely new workspace was minted by this payment.
        metrics.record_onboarding("workspace_minted", workspace_id, source="x402")
        metrics.record_onboarding("x402_paid", workspace_id, **event_kwargs)
    else:
        # Same wallet, a later real payment — resolved to its existing
        # workspace and just extended/renewed the Pro window.
        metrics.record_onboarding("x402_repeat_paid", workspace_id, **event_kwargs)

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
