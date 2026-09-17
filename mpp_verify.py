#!/usr/bin/env python3
"""Custom MPP method wrapping the existing x402 settlement rail (Path A).

MPP's own guide maps x402's `exact` charge flow directly onto MPP's `charge`
intent. This module registers a custom method named `x402-base` whose
challenge advertises the same USDC/EVM terms the x402 SDK already charges,
and whose credential verification delegates settlement to the proven
x402_verify.verify_payment path.

Import never raises: missing/bad config sets MPP_ENABLED = False and
reports why, so the route can fail closed (no WWW-Authenticate challenge
when the challenge cannot be verified) without breaking app startup.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

MPP_SECRET_KEY = os.environ.get("MPP_SECRET_KEY", "")

# Test-only hook: a comma-separated list of <path>=<amount> overrides that
# lets the guard inject a deliberately wrong MPP offer amount into a single
# endpoint without touching production pricing. The harness parses this;
# production code never reads it for real pricing.
_MPP_TEST_OFFER_OVERRIDES: dict[str, str] = {}
_TEST_OVERRIDE_RAW = os.environ.get("_MPP_TEST_OFFER_OVERRIDES", "")
if _TEST_OVERRIDE_RAW:
    for part in _TEST_OVERRIDE_RAW.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            _MPP_TEST_OFFER_OVERRIDES[k.strip()] = v.strip()

MPP_METHOD_NAME = "x402-base"
MPP_ENABLED = False
MPP_DISABLED_REASON = "not initialized"
mpp_server: Any = None


def _load_x402_config():
    """Read live x402 config. Imported lazily so a dev box without x402 SDK
    does not break import of this module."""
    import x402_verify
    asset = x402_verify.X402_USDC_BY_NETWORK.get(x402_verify.X402_NETWORK)
    if asset is None and hasattr(x402_verify, "X402_USDC_BY_NETWORK"):
        asset = x402_verify.X402_USDC_BY_NETWORK.get("eip155:84532")
    return {
        "pay_to": x402_verify.X402_PAY_TO,
        "network": x402_verify.X402_NETWORK,
        "asset": asset,
        "price": x402_verify.X402_MINT_PRICE,
        "enabled": getattr(x402_verify, "X402_ENABLED", False),
    }


class X402BaseChargeIntent:
    """MPP charge intent that delegates to the existing x402 settlement path."""

    name = "charge"

    async def verify(self, credential, request) -> Any:
        """Verify via the existing x402 rail and return an MPP Receipt.

        The credential's payload is expected to carry the x402 v2 signed
        payment envelope that x402_verify can settle. We reconstruct the
        original x402 HTTP request context from the credential so the same
        `verify_payment` path can be reused.
        """
        import x402_verify

        payload = credential.payload or {}
        x402_header = payload.get("x402_header")
        if not x402_header:
            from mpp.errors import VerificationFailedError
            raise VerificationFailedError(
                "x402-base credential missing x402_header payload")

        # Build a minimal Starlette-like request adapter that the x402 SDK
        # needs for verification. The route path and method are the only
        # things that matter for the exact scheme; headers carry the payment.
        class _Adapter:
            def __init__(self, raw_headers):
                self._raw = raw_headers

            def get_header(self, name):
                return self._raw.get(name.lower())

            def get_method(self):
                return "POST"

            def get_path(self):
                return "/v1/billing/x402"

            def get_url(self):
                return "https://aiagentscity.com/v1/billing/x402"

            def get_accept_header(self):
                return "application/json"

            def get_user_agent(self):
                return "mpp-agent"

            def get_query_params(self):
                return {}

            def get_query_param(self, name):
                return None

            def get_body(self):
                return ""

        # verify_payment() takes a Starlette-Request-like object and reads
        # `.url.path`, `.method`, `.headers` and `.query_params` off it. Passing
        # the x402 SDK's own X402HTTPRequestContext here (which carries only
        # payment_header + route_pattern) raised AttributeError before settlement
        # was ever attempted — so no MPP credential could ever be paid, and the
        # route's fall-through turned that crash into a retryable 402. Reproduced:
        # "'HTTPRequestContext' object has no attribute 'url'".
        class _Url:
            def __init__(self, raw):
                self._raw = raw

            @property
            def path(self):
                return "/v1/billing/x402"

            def __str__(self):
                return self._raw

        class _SettledRequest:
            """Minimal Request stand-in carrying the credential's x402 envelope."""

            def __init__(self, x402_header: str):
                self._headers = {"payment-signature": x402_header,
                                 "x-payment": x402_header}
                self.method = "POST"
                self.url = _Url("https://aiagentscity.com/v1/billing/x402")
                self.query_params: dict[str, str] = {}

            @property
            def headers(self):
                return self._headers

        result = x402_verify.verify_payment(_SettledRequest(x402_header))
        if not result.get("verified"):
            from mpp.errors import VerificationFailedError
            raise VerificationFailedError(
                result.get("error") or "x402 settlement refused the payment")

        # Stash the full settlement result so verify_credential() can hand it
        # back to the route alongside the MPP receipt.
        tx_hash = result.get("tx_hash") or result.get("payer_wallet") or "x402-settled"
        _last_settlement_result[tx_hash] = result

        # MPP receipt references the settlement tx hash.
        from mpp import Receipt
        return Receipt.success(
            reference=tx_hash,
            method=MPP_METHOD_NAME,
            external_id=result.get("payer_wallet"),
        )


class X402BaseMethod:
    """Custom MPP method binding the charge intent to the x402 rail."""

    name = MPP_METHOD_NAME

    def __init__(self, intents, recipient, currency, decimals=6):
        self.intents = intents
        self.recipient = recipient
        self.currency = currency
        self.decimals = decimals

    def create_credential(self, challenge):
        raise NotImplementedError("server only")


def _init():
    """Build the Mpp server once at import. Never raises."""
    global MPP_ENABLED, MPP_DISABLED_REASON, mpp_server
    try:
        from mpp.server import Mpp

        if not MPP_SECRET_KEY:
            raise RuntimeError("MPP_SECRET_KEY not configured")
        cfg = _load_x402_config()
        if not cfg["pay_to"]:
            raise RuntimeError("X402_PAY_TO not configured")
        if not cfg["asset"]:
            raise RuntimeError("USDC asset unknown for network " + cfg["network"])

        method = X402BaseMethod(
            intents={"charge": X402BaseChargeIntent()},
            recipient=cfg["pay_to"],
            currency=cfg["asset"],
            decimals=6,
        )
        # Realm defaults to the request host at challenge time; a static env
        # override remains available for deployments that need a canonical realm.
        realm = os.environ.get("MPP_REALM", "")
        mpp_server = Mpp.create(method=method, realm=realm or "aiagentscity.com", secret_key=MPP_SECRET_KEY)
        MPP_ENABLED = True
        MPP_DISABLED_REASON = ""
    except Exception as e:  # noqa: BLE001
        MPP_ENABLED = False
        MPP_DISABLED_REASON = str(e)[:200]
        logging.warning(f"MPP billing route disabled: {MPP_DISABLED_REASON}")


_init()


def requires_mpp_secret_key() -> bool:
    """True when a secret key is configured."""
    return bool(MPP_SECRET_KEY)


def mpp_challenge_amount_atomic() -> str:
    """The MPP offer amount in USDC atomic units, derived from x402 price."""
    import x402_verify
    return x402_verify.x402_mint_price_atomic()


def mpp_offer_dict(path: str = "") -> dict[str, Any]:
    """The MPP discovery offer for /openapi.json."""
    import x402_verify
    cfg = _load_x402_config()
    amount = str(x402_verify.x402_mint_price_atomic())
    # Test-only override: lets the guard inject a wrong amount into one MPP
    # surface in NEW wording, prove RED, then remove it and prove GREEN.
    if path and path in _MPP_TEST_OFFER_OVERRIDES:
        amount = str(_MPP_TEST_OFFER_OVERRIDES[path])
    return {
        "amount": amount,
        "currency": cfg["asset"],
        "description": (
            f"{x402_verify.X402_MINT_PRICE} USDC via x402-base buys a "
            f"{round(x402_verify.X402_PRO_PASS_SECONDS / 3600)}h Pro pass "
            "on a workspace bound to the paying wallet"),
        "intent": "charge",
        "method": MPP_METHOD_NAME,
    }


def build_challenge(request: Any, realm: str | None = None) -> Any:
    """Return an MPP Challenge for the unpaid x402 billing route.

    Returns None when MPP is not enabled (fail closed). This is a sync wrapper
    because the billing route is currently a sync FastAPI handler; the Mpp SDK's
    charge() is async and must be awaited inside an event loop.
    """
    if not MPP_ENABLED or mpp_server is None:
        return None
    import asyncio
    import x402_verify
    # Mpp.charge() expects a human-readable decimal amount and converts to
    # base units using the method's decimals (6 for USDC). Pass the dollar
    # price (e.g. '0.01') so it emits 10000 atomic units.
    amount = str(float(str(x402_verify.X402_MINT_PRICE).lstrip("$")))
    # ── Advertise the credential contract ────────────────────────────────────
    # The intent requires the signed x402 payment under payload["x402_header"].
    # That key is OUR invention — it is not part of the MPP standard and it was
    # NOT carried anywhere a payer could read, so no off-the-shelf mppx/pympp
    # client could construct a credential this server would accept. The tests
    # never caught it because every paying-path test hand-builds that field.
    # Publishing it in the challenge's `extra` (the SDK's own field for string
    # metadata on the charge request) is what makes the rail payable by a client
    # that did not read our source. Without this the only client that can pay is us.
    _credential_contract = {
        "credential_payload.x402_header":
            "required - the base64 x402 v2 signed payment envelope (the same "
            "value an X-PAYMENT / PAYMENT-SIGNATURE header would carry)",
    }
    # Prefer the request's Host header, then the configured override, then the
    # deployment default. mppx validates realm against the origin it is probing.
    effective_realm = realm
    if not effective_realm:
        effective_realm = os.environ.get("MPP_REALM", "")
    if not effective_realm and request is not None:
        effective_realm = request.headers.get("host", "")
    if not effective_realm:
        effective_realm = "aiagentscity.com"
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(mpp_server.charge(
            authorization=None, amount=amount, extra=_credential_contract))
    # Already inside an event loop (e.g. FastAPI sync route in async worker):
    # schedule the coroutine on that loop and block the worker thread until it
    # completes. run_sync + asyncio.run would raise here.
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(
            asyncio.run,
            mpp_server.charge(authorization=None, amount=amount,
                              extra=_credential_contract)
        )
        challenge = future.result()
        # The challenge object carries a realm set at create time; update it
        # to the request host so mppx's origin check passes when probing locally.
        if hasattr(challenge, "realm"):
            challenge.realm = effective_realm
        return challenge


def verify_credential(authorization: str | None) -> dict[str, Any]:
    """Verify an MPP credential and return the x402 settlement result.

    Returns {"receipt": Receipt, "result": x402_verify result} on success,
    or raises mpp.errors.PaymentError / VerificationFailedError on failure.

    NOTE: `broadcast_credential` is a COROUTINE. It must be awaited — calling it
    bare returns a coroutine object, and the very next attribute access raises
    "coroutine has no attribute 'reference'". Because the route wraps this call
    in a fall-through handler, the original bug degraded a real payment attempt
    into a silent 402 instead of minting a workspace, and the only trace was a
    RuntimeWarning. `build_challenge` handles the same async problem the same
    way; keep the two in step.
    """
    if not MPP_ENABLED or mpp_server is None:
        from mpp.errors import PaymentMethodUnsupportedError
        raise PaymentMethodUnsupportedError("x402-base MPP is not enabled")
    import asyncio
    import x402_verify
    amount = str(float(str(x402_verify.X402_MINT_PRICE).lstrip("$")))
    coro = mpp_server.broadcast_credential(
        authorization or "",
        intent="charge",
        request={"amount": amount},
    )
    # Await it. Outside a loop, asyncio.run owns the coroutine directly. Inside
    # one (FastAPI may run this sync handler in a worker thread that still has a
    # loop), hand asyncio.run to a worker thread so we can block for the result.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        receipt = asyncio.run(coro)
    else:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            receipt = pool.submit(asyncio.run, coro).result()
    # NOTE: x402_verify.SettlementOutcomeUnknown is deliberately NOT caught here.
    # It means the payment reached the settlement rail and then failed, so the
    # route must not answer with a retryable 402. The SDK re-raises it unchanged
    # (mpp/server/verify.py: `except Exception: emit_failure; raise`), letting it
    # reach the route's dedicated handler. Re-exported below so the route can
    # reference it without importing x402_verify itself.
    # A Receipt is the only acceptable success value. If the SDK ever returns a
    # Challenge here, the credential was NOT paid — refuse rather than pretend.
    if receipt is None or not hasattr(receipt, "reference"):
        from mpp.errors import PaymentError
        raise PaymentError(
            f"MPP broadcast returned {type(receipt).__name__}, not a Receipt — "
            "the credential was not settled")
    # The receipt.reference is the tx hash; the intent stored the full x402
    # result in a module-level stash so the route can access it.
    return {"receipt": receipt, "result": _last_settlement_result.get(receipt.reference) or {}}


# Intent-to-route bridge: X402BaseChargeIntent stores the x402 settlement
# result keyed by tx hash so verify_credential can hand it back.
_last_settlement_result: dict[str, dict[str, Any]] = {}

# Re-exported so callers can classify an ambiguous settlement without importing
# x402_verify directly (the route already imports both, but keeping the name
# here makes the money-safety contract discoverable next to the MPP method).
import x402_verify as _x402  # noqa: E402
SettlementOutcomeUnknown = _x402.SettlementOutcomeUnknown
