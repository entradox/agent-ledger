"""Per-call payable resources sold over x402 at the city layer (D-1641).

The traffic that actually pays on this host is the x402/agent-commerce fleet —
it finds payable routes via /.well-known/x402 + llms.txt and pays PER CALL for
a resource, not for a workspace mint. These routes sell the satellites'
per-call work. The free bridges in routes_satellite stay unchanged: free is the
funnel, these are the same calls with a price tag.

Money discipline, same as POST /v1/billing/x402:
  - input is validated BEFORE charging — a 422 never costs the payer
  - the settlement recipient must match the configured treasury address
  - the settlement tx_hash is the idempotency key: a replayed payment returns
    the cached resource instead of double-calling the upstream
  - an upstream failure AFTER settlement releases the key, so the same payment
    can retry and be re-served without paying again
  - a settlement whose outcome is ambiguous never gets a retryable 402 —
    that would invite a double charge
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import metrics
import routes_satellite
import x402_verify
from ledger_engine import (IdempotencyConflictError, IdempotencyKeyTooLongError,
                           error_envelope, idempotency_begin,
                           idempotency_release, idempotency_store)

router = APIRouter()

PROBE_ROUTE = "POST /api/x402/agent-watch/probe"
CENSUS_ROUTE = "GET /api/x402/agent-watch/census"
TRUSTSCAN_ROUTE = "POST /api/x402/trustscan/npm"


def _settle(request: Request, route_key: str):
    """Verify + settle the payment on `request` priced by `route_key`.

    Returns (result, None) on a confirmed settlement, or (result, response)
    where response is the payable answer to send verbatim — the SDK's own 402
    envelope carries the price/network/pay_to in PAYMENT-REQUIRED and IS the
    discovery surface.
    """
    try:
        result = x402_verify.verify_payment(request, route_key)
    except x402_verify.SettlementOutcomeUnknown as e:
        # Money may already have moved — never answer "pay again" here.
        raise HTTPException(502, detail=error_envelope(
            502, "settlement outcome unknown — the payment may already have "
                 "settled; do NOT pay again",
            error_type="api_error", code="settlement_outcome_unknown"))
    except x402_verify.X402Unavailable:
        raise HTTPException(503, detail=error_envelope(
            503, f"this deployment's x402 rail is not available on "
                 f"{x402_verify.X402_NETWORK} — see /llms.txt for how to pay "
                 "on an available rail",
            error_type="api_error", code="x402_unavailable"))
    except Exception:
        raise HTTPException(502, detail=error_envelope(
            502, "payment processing failed — nothing was settled, safe to retry",
            error_type="api_error", code="x402_processing_error"))

    if result["verified"]:
        return result, None
    unpaid = result.get("unpaid_response") or {"status": 402, "headers": {},
                                               "body": None}
    body = unpaid.get("body")
    if not body:
        body = error_envelope(
            unpaid["status"],
            "payment required — retry with a signed x402 payment header "
            "(PAYMENT-SIGNATURE or X-PAYMENT); the PAYMENT-REQUIRED response "
            "header carries the price, network and pay_to",
            error_type="payment_required_error", code="payment_required")
    return result, JSONResponse(status_code=unpaid["status"], content=body,
                                headers=dict(unpaid.get("headers") or {}))


def _check_recipient(result: dict) -> None:
    """A verified payment to a FOREIGN recipient must not serve the resource."""
    expected = (os.environ.get("X402_RECEIVING_ADDRESS")
                or os.environ.get("X402_PAY_TO", ""))
    recipient = result.get("recipient")
    if expected and (not recipient or str(recipient).lower() != expected.lower()):
        raise HTTPException(402, detail=error_envelope(
            402, "settlement recipient does not match this service's receiving "
                 "address — refusing to serve", error_type="payment_required_error",
            code="x402_recipient_mismatch"))


def _fulfill(result: dict, op: str, work):
    """Dedupe on the settlement tx_hash, run the paid work, cache the answer.

    `work()` returns the response body dict on success or raises HTTPException
    on upstream failure — a failure releases the idempotency slot so the same
    payment retries the resource instead of caching the error.
    """
    tx_hash = result.get("tx_hash")
    if not tx_hash:
        raise HTTPException(402, detail=error_envelope(
            402, "settlement transaction hash missing — payment cannot be "
                 "deduplicated, refusing to serve",
            error_type="payment_required_error", code="x402_no_tx_hash"))
    wallet = result.get("payer_wallet") or "x402"
    try:
        status, cached = idempotency_begin(tx_hash, wallet, op)
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
        body = work()
    except BaseException:
        idempotency_release(tx_hash, wallet, op)
        raise
    try:
        cents = round(int(result.get("amount")) / 10_000)
    except (TypeError, ValueError):
        cents = None
    # `wallet` is a public Base address, not a secret — it is what makes "did
    # the same buyer pay twice" (the card's actual demand question) countable.
    kwargs = {"resource": op, "wallet": wallet.lower()}
    if cents is not None:
        kwargs["amount_cents"] = cents
    metrics.record_event("x402_settled", **kwargs)
    idempotency_store(tx_hash, wallet, op, body, 200)
    headers = dict(result.get("settlement_headers") or {})
    return JSONResponse(status_code=200, content=body, headers=headers)


# ── Agent Watch: paid probe ──────────────────────────────────────────────────

@router.post("/api/x402/agent-watch/probe")
def paid_probe(req: routes_satellite.ProbeRequest, request: Request):
    """x402-payable endpoint probe — same upstream as the free
    /api/agent-watch/probe, minus the rate limit (the payment IS the limiter).
    Input is validated before charging."""
    err = routes_satellite._url_is_public(req.url)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=422)
    result, unpaid = _settle(request, PROBE_ROUTE)
    if unpaid is not None:
        return unpaid
    _check_recipient(result)

    def work():
        data, uerr = routes_satellite._post_json(
            routes_satellite.AW_PROBE_URL,
            {"url": req.url.strip(), "timeout": 25}, timeout=55)
        if uerr:
            raise HTTPException(502, detail=error_envelope(
                502, "probe backend unreachable right now — payment settled, "
                     "retry with the same payment to be re-served",
                error_type="api_error", code="upstream_unreachable"))
        data = data or {}
        return {"ok": True, "result": {
            "endpoint_url": data.get("endpoint_url") or req.url.strip(),
            "alive": data.get("alive"),
            "latency_p50_ms": data.get("latency_p50_ms"),
            "latency_p95_ms": data.get("latency_p95_ms"),
            "protocol": data.get("protocol"),
            "tool_count": data.get("tool_count"),
            "price_challenge_found": data.get("price_challenge_found"),
            "auth_metadata_present": data.get("auth_metadata_present"),
            "error": data.get("error"),
            "probed_at": data.get("probed_at"),
        }}

    return _fulfill(result, "aw_probe", work)


# ── Agent Watch: paid census ─────────────────────────────────────────────────

@router.get("/api/x402/agent-watch/census")
def paid_census(request: Request):
    """x402-payable ecosystem census snapshot."""
    result, unpaid = _settle(request, CENSUS_ROUTE)
    if unpaid is not None:
        return unpaid
    _check_recipient(result)

    def work():
        data, uerr = routes_satellite._get_json(
            routes_satellite.AW_CENSUS_URL, timeout=20)
        if uerr or not data:
            raise HTTPException(502, detail=error_envelope(
                502, "census backend unreachable right now — payment settled, "
                     "retry with the same payment to be re-served",
                error_type="api_error", code="upstream_unreachable"))
        return {"ok": True, "census": {
            "total": data.get("total"), "alive": data.get("alive"),
            "dead": data.get("dead"), "unknown": data.get("unknown"),
            "pct_alive": data.get("pct_alive"),
        }}

    return _fulfill(result, "aw_census", work)


# ── TrustScan: paid npm check ────────────────────────────────────────────────

@router.post("/api/x402/trustscan/npm")
def paid_trustscan_npm(req: routes_satellite.NpmCheckRequest, request: Request):
    """x402-payable npm trust check — same verdict as the free
    /api/trustscan/npm form, minus the rate limit."""
    name = (req.package or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "enter a package name"},
                            status_code=422)
    if name != name.lower():
        return JSONResponse({"ok": False, "error": "npm names are lowercase",
                             "suggestion": name.lower()}, status_code=422)
    if not routes_satellite.NPM_NAME_RE.match(name):
        return JSONResponse({"ok": False,
                             "error": "that isn't a valid npm package name"},
                            status_code=422)
    result, unpaid = _settle(request, TRUSTSCAN_ROUTE)
    if unpaid is not None:
        return unpaid
    _check_recipient(result)

    def work():
        res = routes_satellite.npm_trust_check(name)
        if isinstance(res, JSONResponse):
            raise HTTPException(502, detail=error_envelope(
                502, "npm registry unreachable right now — payment settled, "
                     "retry with the same payment to be re-served",
                error_type="api_error", code="upstream_unreachable"))
        return res

    return _fulfill(result, "trustscan_npm", work)
