#!/usr/bin/env python3
"""x402 payment verification + settlement, via the real x402 Python SDK.

Replaces the earlier hand-rolled `urllib` POST to an unconfirmed facilitator
schema. This module wraps `x402.http.x402HTTPResourceServerSync` — the same
pattern Agent Watch uses on `/v1/probe/x402`, which has completed a real live
settlement — and adapts it to AgentLedger's need: verify + settle an incoming
payment and hand the route back `(verified, payer_wallet, tx_hash)` plus the
settlement detail the route already validates (recipient/amount/asset).

Configuration (all env, all optional — an unconfigured service degrades to
"x402 unavailable" rather than failing to start):

    X402_PAY_TO           receiving wallet address. NO DEFAULT — AgentLedger
                          must be given its own address; defaulting this would
                          route real funds to another product's treasury.
    X402_FACILITATOR_URL  default https://x402.org/facilitator (free, public,
                          no account; supports Base Sepolia for the `exact`
                          scheme).
    X402_NETWORK          default eip155:84532 (Base Sepolia testnet).
    X402_MINT_PRICE       default $0.01 — price to mint a workspace.

Import never raises: any failure (SDK missing, bad config, unreachable
facilitator at init) sets X402_ENABLED = False and `verify_payment` raises
X402Unavailable, which the route turns into a 503.
"""
import logging
import os

ROUTE_KEY = "POST /v1/billing/x402"

X402_PAY_TO = os.environ.get("X402_PAY_TO", "")
X402_FACILITATOR_URL = os.environ.get("X402_FACILITATOR_URL",
                                      "https://x402.org/facilitator")
X402_NETWORK = os.environ.get("X402_NETWORK", "eip155:84532")
X402_MINT_PRICE = os.environ.get("X402_MINT_PRICE", "$0.01")

X402_ENABLED = False
X402_DISABLED_REASON = "not initialized"
resource_server = None
HTTPRequestContext = None


class X402Unavailable(RuntimeError):
    """x402 is not configured/initialized on this service."""


def _init():
    """Build the resource server once at import. Never raises."""
    global X402_ENABLED, X402_DISABLED_REASON, resource_server, HTTPRequestContext
    try:
        if not X402_PAY_TO:
            raise RuntimeError("X402_PAY_TO not configured (no receiving address)")
        from x402.http import (x402HTTPResourceServerSync, RouteConfig,
                               PaymentOption, HTTPFacilitatorClientSync,
                               FacilitatorConfig,
                               HTTPRequestContext as _HTTPRequestContext)
        from x402.server import x402ResourceServerSync
        from x402.mechanisms.evm.exact import ExactEvmServerScheme

        facilitator = HTTPFacilitatorClientSync(
            FacilitatorConfig(url=X402_FACILITATOR_URL))
        core = x402ResourceServerSync(facilitator_clients=[facilitator])
        core.register(X402_NETWORK, ExactEvmServerScheme())
        routes = {
            ROUTE_KEY: RouteConfig(
                accepts=PaymentOption(
                    scheme="exact",
                    pay_to=X402_PAY_TO,
                    price=X402_MINT_PRICE,
                    network=X402_NETWORK,
                    max_timeout_seconds=60,
                ),
                description="Claim an AgentLedger workspace. Pays once; returns "
                            "a workspace_id and a workspace_key (shown once) "
                            "bound to the paying wallet.",
                service_name="AgentLedger",
                tags=["agent-economy", "ledger", "billing", "workspace"],
            ),
        }
        server = x402HTTPResourceServerSync(core, routes)
        init_res = core.initialize()
        if hasattr(init_res, "result") and init_res.result is False:
            raise RuntimeError("x402 core initialization failed")
        resource_server = server
        HTTPRequestContext = _HTTPRequestContext
        X402_ENABLED = True
        X402_DISABLED_REASON = ""
    except Exception as e:  # noqa: BLE001 — import must never break the app
        X402_ENABLED = False
        X402_DISABLED_REASON = str(e)[:200]
        logging.warning(f"x402 billing route disabled: {X402_DISABLED_REASON}")


_init()


class _FastAPIAdapter:
    """Framework adapter for the x402 SDK's HTTP protocol interface."""

    def __init__(self, request):
        self._r = request

    def get_header(self, name):
        return self._r.headers.get(name)

    def get_method(self):
        return self._r.method

    def get_path(self):
        return self._r.url.path

    def get_url(self):
        return str(self._r.url)

    def get_accept_header(self):
        return self._r.headers.get("accept", "application/json")

    def get_user_agent(self):
        return self._r.headers.get("user-agent", "")

    def get_query_params(self):
        return dict(self._r.query_params)

    def get_query_param(self, name):
        return self._r.query_params.get(name)

    def get_body(self):
        # The mint route takes no body — the payment IS the request.
        return ""


def _instructions_to_dict(resp):
    """HTTPResponseInstructions -> plain dict the route can return verbatim."""
    if resp is None:
        return None
    return {
        "status": resp.status,
        "headers": dict(resp.headers or {}),
        "body": resp.body if resp.body is not None else {},
    }


def verify_payment(request) -> dict:
    """Verify + settle the x402 payment carried on `request`.

    Takes the full FastAPI/Starlette Request (not just the X-PAYMENT header
    string) because the SDK needs method/path/URL/headers to build the
    payment requirements it advertises in a 402.

    Returns a dict with the contract the route already consumes:

        verified      bool
        payer_wallet  payer address, or None
        tx_hash       settlement transaction hash, or None
        recipient     the pay_to the payment was actually made against
        amount/asset/network  settlement detail
        settlement_headers    PAYMENT-RESPONSE headers to echo to the client
        unpaid_response       set when the SDK produced its own response
                              (the 402 with paymentRequirements, or a
                              settlement-failure response). The route must
                              forward it verbatim, headers included — that
                              402 envelope is how discovery clients learn
                              the price and the payment requirements.

    Raises X402Unavailable when x402 is not configured on this service.
    """
    if not X402_ENABLED or resource_server is None:
        raise X402Unavailable(X402_DISABLED_REASON or "x402 not configured")

    ctx = HTTPRequestContext(
        adapter=_FastAPIAdapter(request),
        path=request.url.path,
        method=request.method,
        payment_header=request.headers.get("x-payment"),
        route_pattern=ROUTE_KEY,
    )
    outcome = resource_server.process_http_request(ctx)
    if outcome.response is not None:
        # No payment, invalid payment, or a verification error: the SDK's own
        # response (402 + PAYMENT-REQUIRED header) is the correct answer.
        return {"verified": False, "payer_wallet": None, "tx_hash": None,
                "unpaid_response": _instructions_to_dict(outcome.response)}

    if outcome.payment_payload is None or outcome.payment_requirements is None:
        # "no-payment-required" (route somehow unpriced) — there is no
        # settlement, so no payer and no tx_hash to bind a workspace to.
        # Refuse rather than call process_settlement with None.
        return {"verified": False, "payer_wallet": None, "tx_hash": None,
                "error": "no payment was required for this route — refusing "
                         "to mint without a settlement"}

    settle = resource_server.process_settlement(
        outcome.payment_payload, outcome.payment_requirements, ctx)
    if not settle.success:
        return {"verified": False, "payer_wallet": None, "tx_hash": None,
                "error": settle.error_reason,
                "unpaid_response": _instructions_to_dict(settle.response)}

    reqs = outcome.payment_requirements
    return {
        "verified": True,
        "payer_wallet": settle.payer,
        "tx_hash": settle.transaction,
        # pay_to on the requirements the payment was verified against is the
        # unambiguous "who was actually paid" field. The route still checks it
        # against X402_RECEIVING_ADDRESS when the operator has declared one.
        "recipient": getattr(reqs, "pay_to", None),
        "amount": getattr(reqs, "amount", None),
        "asset": getattr(reqs, "asset", None),
        "network": settle.network or getattr(reqs, "network", None),
        "settlement_headers": dict(settle.headers or {}),
    }
