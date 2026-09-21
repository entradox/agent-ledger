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
    X402_FACILITATOR_URL  explicit facilitator URL. When set it WINS over the
                          CDP credentials below — an operator overriding the
                          URL should not be silently ignored.
    CDP_API_KEY_ID        Coinbase CDP key id. When both this and the secret are
    CDP_API_KEY_SECRET    set, the CDP Facilitator is used instead of the free
                          public one. Required for mainnet: the public
                          facilitator at https://x402.org/facilitator serves
                          TESTNETS ONLY (verified 2026-09-15 — it advertises
                          base-sepolia/eip155:84532, solana-devnet, aptos:2,
                          hedera:testnet, stellar:testnet, xrpl:1, algorand:
                          testnet, and no Base mainnet). So the CDP Facilitator
                          is not an optimisation for mainnet, it is the only
                          supported path.
    X402_NETWORK          default eip155:84532 (Base Sepolia testnet).
                          eip155:8453 = Base mainnet (needs CDP creds).
    X402_MINT_PRICE       default $0.01 — price to mint a workspace.

Import never raises: any failure (SDK missing, bad config, unreachable
facilitator at init) sets X402_ENABLED = False and `verify_payment` raises
X402Unavailable, which the route turns into a 503.
"""
import logging
import os

ROUTE_KEY = "POST /v1/billing/x402"

X402_PAY_TO = os.environ.get("X402_PAY_TO", "")
X402_FACILITATOR_URL = os.environ.get("X402_FACILITATOR_URL", "")
CDP_API_KEY_ID = os.environ.get("CDP_API_KEY_ID", "")
CDP_API_KEY_SECRET = os.environ.get("CDP_API_KEY_SECRET", "")
X402_NETWORK = os.environ.get("X402_NETWORK", "eip155:84532")
X402_MINT_PRICE = os.environ.get("X402_MINT_PRICE", "$0.01")


def x402_mint_price_atomic(decimals: int = 6) -> int:
    """The configured mint price in atomic units.

    Single source of truth: every discovery document, the x402 till, and
    the MPP offer all derive from X402_MINT_PRICE rather than carrying their
    own literal. The price is a string like "$0.01"; this strips the "$"
    and converts to base units (1e6 for USDC 6 decimals by default).
    """
    raw = str(X402_MINT_PRICE).lstrip("$")
    try:
        return int(round(float(raw) * (10 ** decimals)))
    except (TypeError, ValueError):
        raise ValueError(f"X402_MINT_PRICE={X402_MINT_PRICE!r} is not a valid price")

# What a settled x402 payment actually buys (D-1270): a time-boxed Pro pass —
# unlimited agents on the resolved workspace — rather than the free-tier-
# equivalent workspace it used to mint. Lives here, next to X402_MINT_PRICE,
# so every module that talks about the x402 offer (the billing route, the
# cap-exceeded quote) reads one number instead of each hardcoding its own.
X402_PRO_PASS_SECONDS = int(os.environ.get("X402_PRO_PASS_SECONDS", 24 * 3600))

# What the ONE-TIME $0.01 trial GRANTS (principal-owned pricing knob). Values:
#   "unlimited"  (default, current behaviour) — unlimited agents for the pass
#   "starter"    — the Starter cap (workspace_engine.STARTER_AGENT_CAP, 10 agents)
# The trial is limited to one purchase per wallet either way (see
# TrialAlreadyUsed); this only decides how generous that one purchase is. Any
# other value logs a warning and falls back to "unlimited" so a typo can never
# silently change what a paying customer receives.
X402_TRIAL_GRANT = os.environ.get("X402_TRIAL_GRANT", "unlimited").strip().lower()
if X402_TRIAL_GRANT not in ("unlimited", "starter"):
    logging.warning(f"X402_TRIAL_GRANT={X402_TRIAL_GRANT!r} is not 'unlimited' or "
                    "'starter'; using 'unlimited'")
    X402_TRIAL_GRANT = "unlimited"

# The paid path a repeat trial buyer is pointed at.
X402_TRIAL_PAID_PATH = "/start?plan=starter"


def x402_trial_tier() -> str:
    """The workspace tier the trial pass is granted as ("pro" = unlimited)."""
    return "starter" if X402_TRIAL_GRANT == "starter" else "pro"


# Resolved facilitator: explicit URL wins, else CDP, else the free public one.
FACILITATOR_URL_RESOLVED = (X402_FACILITATOR_URL
                            or ("https://api.cdp.coinbase.com/platform/v2/x402"
                                if (CDP_API_KEY_ID and CDP_API_KEY_SECRET)
                                else "https://x402.org/facilitator"))
FACILITATOR_MODE = ("explicit-url" if X402_FACILITATOR_URL
                    else ("cdp" if (CDP_API_KEY_ID and CDP_API_KEY_SECRET)
                          else "public-testnet"))

# Mainnet identifiers, so a misconfiguration is caught at import rather than at
# the first real payment. The public facilitator cannot settle these.
_MAINNET_NETWORKS = {"eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"}

# Canonical USDC contracts per network, so every module that needs the asset
# (discovery docs, MPP offer) reads one table instead of hardcoding its own.
X402_USDC_BY_NETWORK = {
    "eip155:84532": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",  # Base Sepolia
    "eip155:8453": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",   # Base mainnet
}

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
        # Refuse a mainnet/mode mismatch instead of advertising a network no
        # configured facilitator can settle. Without this, X402_NETWORK=8453
        # plus no CDP creds would publish a mainnet 402 that can never complete —
        # a config error that only shows up as a failed real payment.
        if X402_NETWORK in _MAINNET_NETWORKS and FACILITATOR_MODE == "public-testnet":
            raise RuntimeError(
                f"X402_NETWORK={X402_NETWORK} is mainnet but no CDP credentials "
                "are set (CDP_API_KEY_ID/CDP_API_KEY_SECRET); the public "
                "facilitator serves testnets only, so this payment could never "
                "settle")
        from x402.http import (x402HTTPResourceServerSync, RouteConfig,
                               PaymentOption, HTTPFacilitatorClientSync,
                               FacilitatorConfig,
                               HTTPRequestContext as _HTTPRequestContext)
        from x402.server import x402ResourceServerSync
        from x402.mechanisms.evm.exact import ExactEvmServerScheme

        # CDP mode: build the authenticated client. create_facilitator_config()
        # returns a FacilitatorConfig, and it authenticates verify/settle
        # against the CDP Facilitator — it does not create a receiving wallet,
        # so X402_PAY_TO is still required and still ours.
        if FACILITATOR_MODE == "cdp":
            try:
                from cdp.x402 import create_facilitator_config
            except ImportError as exc:
                raise RuntimeError(
                    "CDP credentials are set but the cdp-sdk package is not "
                    "installed (pip install cdp-sdk)") from exc
            facilitator_cfg = create_facilitator_config(
                api_key_id=CDP_API_KEY_ID, api_key_secret=CDP_API_KEY_SECRET)
        else:
            facilitator_cfg = FacilitatorConfig(url=FACILITATOR_URL_RESOLVED)

        facilitator = HTTPFacilitatorClientSync(facilitator_cfg)
        core = x402ResourceServerSync(facilitator_clients=[facilitator])
        core.register(X402_NETWORK, ExactEvmServerScheme())
        routes = {
            ROUTE_KEY: RouteConfig(
                accepts=PaymentOption(
                    scheme="exact",
                    pay_to=X402_PAY_TO,
                    price=X402_MINT_PRICE,
                    # The resource server uses the literal string for display.
                    # We register it exactly as configured so the 402 envelope
                    # stays human-readable; amount enforcement is done by the
                    # atomic price helper below.
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


class SettlementOutcomeUnknown(Exception):
    """The settlement call was made, then failed in an unclassifiable way.

    Raised ONLY from around `process_settlement` — i.e. after the payment has
    been handed to the facilitator, where USDC may already have moved on-chain.
    Callers must NOT answer a retryable "pay me again" (that invites a double
    charge) and must NOT mint without a confirmed settlement. They return a
    non-retryable typed error instead.

    Why this lives here: only this module knows which line separates
    "verification refused" (definitively unpaid, safe to retry) from
    "settlement attempted" (ambiguous, never safe to invite a retry). Deriving
    that from a flag elsewhere would be a guess; deriving it here is exact.
    """

    def __init__(self, cause: BaseException, reference: str | None = None):
        self.cause = cause
        # The settlement reference (tx hash) when one is known. It normally is
        # not: this is raised when settlement failed mid-flight, before a hash
        # was returned. Kept as an explicit attribute so callers never have to
        # guess whether a reference exists.
        self.reference = reference
        super().__init__(
            f"settlement outcome unknown (reference={reference or 'unknown'}): "
            f"{cause}")


class TrialAlreadyUsed(Exception):
    """This wallet already bought its one-time $0.01 trial.

    Raised from verify_payment BEFORE settlement, so no money has moved and the
    caller can refuse cleanly. Both payment rails (x402 header and MPP
    credential) reach settlement through verify_payment, so this one check
    covers both.
    """

    def __init__(self, wallet: str):
        self.wallet = wallet
        super().__init__(f"x402 trial already used by wallet {wallet}")


def _payer_from_payload(payment_payload) -> str | None:
    """The paying wallet, read from the signed payload — known BEFORE settlement.

    The settlement result only reveals the payer after the money has moved, which
    is too late to refuse. The EIP-3009 authorization (and Permit2's) carries the
    payer as `from`, covered by the signature the facilitator just verified.
    None when the shape is not one we recognise; the caller then lets settlement
    proceed (the route still honours and logs the purchase) rather than refusing
    a payment we cannot attribute.
    """
    inner = getattr(payment_payload, "payload", None)
    if not isinstance(inner, dict):
        return None
    for key in ("authorization", "permit2Authorization"):
        auth = inner.get(key)
        if isinstance(auth, dict):
            payer = auth.get("from") or auth.get("from_address")
            if isinstance(payer, str) and payer:
                return payer
    return None


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

    Raises X402Unavailable when x402 is not configured on this service, and
    TrialAlreadyUsed when the paying wallet has already bought its one-time
    trial (raised before settlement — no money moves).
    """
    if not X402_ENABLED or resource_server is None:
        raise X402Unavailable(X402_DISABLED_REASON or "x402 not configured")

    ctx = HTTPRequestContext(
        adapter=_FastAPIAdapter(request),
        path=request.url.path,
        method=request.method,
        # x402 has two live header conventions in the wild: v2 clients send
        # PAYMENT-SIGNATURE, v1 clients send X-PAYMENT. The SDK's own reference
        # FastAPI middleware checks both (payment-signature first) — mirrored
        # here since AgentLedger builds its own HTTPRequestContext by hand
        # instead of using that middleware. Missing this meant every v2 client
        # (the current, higher-adoption generation) signed and sent a valid
        # payment that this route silently never looked for.
        payment_header=(request.headers.get("payment-signature")
                        or request.headers.get("x-payment")),
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

    # ── ONE TRIAL PER WALLET — refuse BEFORE the money moves ─────────────────
    # The payment above is verified but not settled. This is the last point at
    # which refusing costs the payer nothing: after process_settlement the USDC
    # is gone, and taking $0.01 while granting nothing is the worst outcome.
    # The route only learns the wallet AFTER settlement, so the check cannot
    # live there. A repeat purchase that races past this check (two concurrent
    # submits before either workspace exists) has already settled, and the
    # route honours it.
    payer = _payer_from_payload(outcome.payment_payload)
    if payer:
        import workspace_engine
        if workspace_engine.wallet_has_used_trial(payer):
            raise TrialAlreadyUsed(payer)

    # ── SETTLEMENT BOUNDARY ──────────────────────────────────────────────────
    # Everything above is verification: a refusal there is definitively
    # "unpaid" and safe to answer with a retryable 402. Past this call the
    # facilitator may already have broadcast a USDC transfer, so a failure is
    # AMBIGUOUS and must never be reported as a plain "not paid".
    # A returned settle.success == False is a definitive refusal (the SDK
    # obtained an answer and it was no); only a RAISE is ambiguous.
    try:
        settle = resource_server.process_settlement(
            outcome.payment_payload, outcome.payment_requirements, ctx)
    except Exception as exc:  # noqa: BLE001 — classify, never mask
        raise SettlementOutcomeUnknown(exc) from exc
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
