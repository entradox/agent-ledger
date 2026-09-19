#!/usr/bin/env python3
"""AgentLedger — REST API.

Endpoints:
  GET  /health                       — liveness
  POST /v1/track                     — record a spend entry
  POST /v1/budget                    — set budget caps
  GET  /v1/report/{agent_id}         — spend report
  GET  /v1/alerts/{agent_id}         — alerts for agent
  GET  /v1/agents                    — list all tracked agents
  GET  /stats                        — usage counters
  GET  /.well-known/agent.json       — AEO capability manifest
"""
import html, json, os, re, sys, time, hmac, hashlib, uuid
from pathlib import Path
from typing import Optional
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ledger_engine import (
    track, set_budget, get_budget, report, list_agents, _ledger_path,
    ensure_agent_secret, claimed_agent_count, MAX_AMOUNT_CENTS,
    AuthError, ValidationError, BudgetExceededError, BetaCapExceededError,
    WorkspaceKeyRequiredError,
    validate_agent_id as le_validate_agent_id, pro_active, BETA_AGENT_CAP,
    AL_API_VERSION, error_envelope, IdempotencyKeyTooLongError,
    IdempotencyConflictError, idempotency_begin, idempotency_store,
    idempotency_release, scarcity_claims_left,
)
import metrics

from fastapi import FastAPI, HTTPException, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import (PlainTextResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from pydantic import BaseModel, Field
import uvicorn

APP_VERSION = "0.4.1"  # single source for /health + FastAPI metadata
app = FastAPI(title="AgentLedger API", version=APP_VERSION)

# ── OpenAPI augmentation for agent payment-directory discovery (D-1293) ──────
# x402scan (the main x402 directory) FOUND this service via /openapi.json but
# REFUSED to register the payable endpoint:
#
#   registerFromOrigin -> {"success": false, "error": {"type": "noValidResources",
#     "message": "No valid x402 or free (SIWX) resources were found for this
#      origin"}}, failed: 1, skipped: 75, failedDetails: [{"url":
#      "/v1/billing/x402", "error": "validation: Missing input schema — add a
#      requestBody or parameter schema to your OpenAPI spec so agents know what
#      to send"}]
#
# Verified live 2026-09-16. The route takes a raw Request and reads no body, so
# FastAPI emitted no requestBody, and the endpoint was classified
# non-invocable and dropped — the one route that can actually take money was
# the one route the catalogue could not list.
#
# This is applied as an override rather than via openapi_extra= on the route so
# EVERY payment value is read from the same live config the endpoint charges
# from. That is the project's standing drift rule (the `eip155:845` bug, the
# hardcoded testnet asset, the stale mainnet copy): a second literal copy of
# price/network/asset is a second thing that can go wrong silently.
_ORIGINAL_OPENAPI = app.openapi


def _augmented_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = _ORIGINAL_OPENAPI()
    try:
        import x402_verify
        import mpp_verify
        net = _x402_network()
        asset = _x402_asset()
        pay_to = _x402_pay_to()
        amount = str(x402_verify.x402_mint_price_atomic())
        op = schema["paths"].get("/v1/billing/x402", {}).get("post")
        if op is not None:
            op["requestBody"] = {
                "required": False,
                "description": (
                    "No body is required — the payment rides in the "
                    "PAYMENT-SIGNATURE (or X-PAYMENT) header. Send an empty "
                    "JSON object to ask for the 402 challenge."),
                "content": {"application/json": {"schema": {
                    "type": "object", "properties": {},
                    "example": {},
                }}},
            }
            resp = op.setdefault("responses", {})
            resp["402"] = {
                "description": (
                    "Payment required. The PAYMENT-REQUIRED response header "
                    "carries the base64url x402 challenge (price, network, "
                    "asset, payTo, maxTimeoutSeconds)."),
                "headers": {"PAYMENT-REQUIRED": {
                    "description": "base64url x402 payment challenge",
                    "schema": {"type": "string"}}},
                "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"error": {"type": "object"}}}}},
            }
            offer = mpp_verify.mpp_offer_dict(path="/v1/billing/x402")
            op["x-payment-info"] = {
                # HARD CONSTRAINT, proven by Stripe/Tempo's own validator
                # (`npx mppx@latest validate`): you CANNOT mix offers[] with flat
                # payment-info fields at the same level. Doing so fails the
                # document with "Cannot mix offers with flat payment info
                # fields" and the endpoint stops being discoverable as MPP.
                # So `offers` OWNS the top level, and the D-1293 x402scan keys
                # (which must all survive) live under the nested `x402` key.
                # Do not "simplify" this by flattening — it was tried and the
                # validator rejected it.
                "offers": [offer],
                "x402": {
                    "protocols": [{"protocol": "x402", "version": 2,
                                   "scheme": "exact", "network": net}],
                    "pricingMode": "fixed",
                    "currency": "USDC",
                    "amountUnit": "atomic",
                    "amount": offer["amount"],
                    "asset": asset,
                    "network": net,
                    "payTo": pay_to,
                    "priceUsd": float(str(x402_verify.X402_MINT_PRICE).lstrip("$")),
                    "priceDescription": (
                        f"{x402_verify.X402_MINT_PRICE} USDC buys a "
                        f"{round(x402_verify.X402_PRO_PASS_SECONDS / 3600)}h Pro "
                        "pass on a workspace bound to the paying wallet"),
                },
            }
        # MPP discovery spec: x-service-info is a ROOT-LEVEL OpenAPI document
        # extension (sibling of openapi, info, paths), NOT an info member.
        # Verified against https://mpp.dev/advanced/discovery ("x-service-info …
        # add service-level metadata to the document root").
        schema["x-service-info"] = {"docs": {"llms": "/llms.txt"}}

        info = schema.setdefault("info", {})
        info["x-guidance"] = (
            "AgentLedger is per-agent AI spend tracking with ENFORCED budget "
            "caps. Two ways in, both machine-only. (1) FREE, no wallet: "
            "POST /start mints a workspace_key (shown once) with no signup and no "
            "card — GET /start only renders the form and does NOT mint. Claim an agent "
            "with the key in the body of POST /v1/track, set a cap with "
            "POST /v1/budget, then route traffic through /proxy/{provider}/... "
            "so an over-budget call is refused with 402 BEFORE the provider is "
            "contacted. (2) PAID, no human: POST /v1/billing/x402 with a "
            "wallet-signed x402 payment mints a workspace bound to the paying "
            "wallet. NOTE: a workspace_key claims NEW agents; an agent_secret "
            "writes to an existing one. Plain POST /v1/track can reject the "
            "ledger write that would cross a cap but cannot un-spend a charge "
            "that already happened — only the proxy/wrapper path stops money.")
    except Exception:
        # Discovery must never 500 because x402 is unconfigured on a dev box.
        pass
    app.openapi_schema = schema
    return schema


app.openapi = _augmented_openapi

from routes_agents import router as agents_router
app.include_router(agents_router)

# The proxy (D-1222): the only surface where a cap stops money rather than a
# ledger write. Pass-through — the caller's provider credential is forwarded,
# never stored.
from routes_workspace import router as workspace_router
app.include_router(workspace_router)

from routes_proxy import router as proxy_router
app.include_router(proxy_router)

@app.exception_handler(HTTPException)
async def _typed_error_handler(request: Request, exc: HTTPException):
    """Single source for the typed error envelope on every REST error path
    (launch-kit v0.3). Raise sites that already pass an envelope dict as
    `detail` (via error_envelope()) pass through unchanged; anything still
    raising a plain string gets wrapped here so no path is ever untyped —
    without changing any status code."""
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code,
                         content=error_envelope(exc.status_code, str(exc.detail)))
DATA_DIR = Path(os.environ.get("AGENT_LEDGER_DATA", os.path.expanduser("~/.agent-ledger")))
COUNTS_FILE = DATA_DIR / "counts.jsonl"

# reach paths tracked for unique-ip-hash "reach" telemetry
# Paths whose distinct-visitor count is tracked (ip_bucket). D-1321: the old 9-path list
# omitted the product landing pages, so AgentLedger's REAL landing page (/agent-ledger,
# 21 KB, carries the pitch and the pricing, links to /start) was invisible to metrics, while
# the numerator (/start) was counted against a denominator (/ = the AI Agent City umbrella
# hub, which cannot convert — it lists five products and has no price and no /start link).
# The "92% never reach the mint" figure was arithmetic over two different populations, not
# user behaviour. Guarded by test_reach_instrumentation.py, which fails if any registered
# HTML GET route is neither listed here nor explicitly exempted with a reason.
REACH_PATHS = frozenset({"/", "/start", "/status", "/llms.txt", "/server.json",
                          "/.well-known/glama.json", "/.well-known/mcp/server-card.json",
                          "/stats", "/mcp/",
                          # Product landing pages (D-1321)
                          "/agent-ledger", "/quickstart"})

# HTML routes deliberately NOT in REACH_PATHS, each with a reason. A route may only be
# absent from REACH_PATHS if it appears here; otherwise the guard test goes RED. This exists
# because the previous list was hand-maintained with nothing noticing what was missing.
REACH_EXEMPT = {
    "/privacy": "legal boilerplate, not a product surface with a CTA",
    "/terms": "legal boilerplate, not a product surface with a CTA",
    "/security": "informational, reached from footers, not a funnel entry",
    "/reliability": "informational, reached from footers, not a funnel entry",
    "/compare": "comparison page reached mid-funnel, not a funnel entry",
    "/v1/dashboard": "authenticated workspace UI, not a public funnel entry",
    "/perimeter-watch": "different product's landing page — instrument when it gets a funnel",
    "/cited": "different product's landing page — instrument when it gets a funnel",
    "/agent-watch": "different product's landing page — instrument when it gets a funnel",
    "/trust-scan": "different product's landing page — instrument when it gets a funnel",
}


def _ip_hash(request: "Request") -> Optional[str]:
    """sha256(client_ip + AL_METRICS_SALT), truncated — never store raw IPs.
    Refuses to hash at all when the salt is unset: an unsalted sha256 over the
    IPv4 space is brute-forceable in seconds, which would silently degrade the
    privacy guarantee into a reversible pseudonym (Morgan review, 2026-09-09)."""
    salt = os.environ.get("AL_METRICS_SALT", "")
    if not salt:
        return None
    client = request.client.host if request.client else None
    if not client:
        return None
    return hashlib.sha256((client + salt).encode()).hexdigest()[:12]


@app.middleware("http")
async def _metrics_middleware(request: Request, call_next):
    response = await call_next(request)
    try:
        path = request.url.path
        # PII redaction: /v1/billing/{email} has the customer's raw email in the
        # path — never let it reach the metrics event log (Morgan review, 2026-09-09).
        if path.startswith("/v1/billing/") and path not in (
                "/v1/billing/checkout", "/v1/billing/x402"):
            # Only the email-bearing path is PII; the two real routes keep
            # their own bucket so the buy funnel stays countable.
            path = "/v1/billing/<redacted>"
        ip_bucket = f"path:{path}" if path in REACH_PATHS or path.startswith("/mcp") else None
        metrics.record_event("http", path=path, method=request.method,
                              status=response.status_code, ip_hash=_ip_hash(request),
                              ip_bucket=ip_bucket)
    except Exception:
        pass
    return response

@app.get("/health")
def health():
    return {"ok": True, "service": "agent-ledger", "version": APP_VERSION}

import threading as _threading
import time as _time

@app.get("/v1/agents")
def get_agents(request: Request):
    """Portfolio-wide listing across every agent_id ever claimed — owner-only.
    (Per-agent data at GET /v1/report/{agent_id}, /v1/tokens/{agent_id}, and
    /v1/alerts/{agent_id} requires X-Agent-Secret or X-Workspace-Key; this
    endpoint is the separate full cross-tenant dump.)"""
    admin_secret = os.environ.get("AL_ADMIN_SECRET", "")
    if not admin_secret or not hmac.compare_digest(request.headers.get("x-al-admin", ""), admin_secret):
        raise HTTPException(401, "owner only")
    return {"agents": list_agents()}

FUNNEL_KINDS = ("track_ok", "auth_fail", "cap_blocked", "validation_fail",
                "budget_set", "mcp_call")

@app.get("/v1/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    """Owner-only dashboard: every claimed agent, its spend, and its budget
    status in one page. Same X-Al-Admin guard as /v1/agents and /v1/metrics.
    Server-rendered, no JS — reuses list_agents() + report(), both already
    cheap at current scale (a handful of claimed agents)."""
    admin_secret = os.environ.get("AL_ADMIN_SECRET", "")
    if not admin_secret or not hmac.compare_digest(request.headers.get("x-al-admin", ""), admin_secret):
        raise HTTPException(401, "owner only")

    rows = []
    for a in list_agents():
        r = report(a["agent_id"], days=30)
        budget = r.budget_status or {}
        cap = budget.get("monthly_cap_cents")
        cap_str = f"${cap/100:.2f}/mo cap, {budget.get('pct_used', 0):.0f}% used" if cap else "no cap set"
        alerts_path = DATA_DIR / "agents" / a["agent_id"] / "alerts.jsonl"
        alert_count = sum(1 for _ in open(alerts_path)) if alerts_path.exists() else 0
        rows.append({
            "agent_id": a["agent_id"],
            "has_data": a["has_data"],
            "plan": a["plan"],
            "spend_30d": r.total_spend_cents / 100,
            "cap_str": cap_str,
            "exceeded": bool(budget.get("exceeded")),
            "anomalies": len(r.anomalies),
            "alerts": alert_count,
        })

    def row_html(row):
        # Escape every interpolated field even though agent_id/plan are
        # currently regex/enum-constrained — safe by construction, not by
        # accident, so this doesn't become the unsafe precedent a future
        # free-text column (rail/service) copies (Turing review 2026-09-10).
        flag = " ⚠️" if row["exceeded"] or row["anomalies"] or row["alerts"] else ""
        squat = " (squatted — claimed, no writes)" if not row["has_data"] else ""
        agent_id = html.escape(str(row["agent_id"]))
        plan = html.escape(str(row["plan"]))
        cap_str = html.escape(str(row["cap_str"]))
        return (f"<tr><td>{agent_id}{squat}</td><td>{plan}</td>"
                f"<td>${row['spend_30d']:.2f}</td><td>{cap_str}{flag}</td>"
                f"<td>{row['anomalies']}</td><td>{row['alerts']}</td></tr>")

    body_rows = "\n".join(row_html(r) for r in rows) or "<tr><td colspan=6>No agents claimed yet.</td></tr>"
    page_html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>AgentLedger — Dashboard</title>
<style>
body{{background:#0d1117;color:#e6edf3;font-family:-apple-system,sans-serif;padding:24px}}
table{{border-collapse:collapse;width:100%;max-width:900px}}
th,td{{text-align:left;padding:8px 12px;border-bottom:1px solid #30363d;font-size:13px}}
th{{color:#8b949e;font-weight:600}}
h1{{font-size:20px}} .sub{{color:#8b949e;font-size:12px;margin-bottom:16px}}
</style></head><body>
<h1>AgentLedger — Dashboard</h1>
<div class="sub">{len(rows)} claimed agents · 30-day window · owner-only</div>
<table><tr><th>Agent</th><th>Plan</th><th>30d spend</th><th>Budget</th><th>Anomalies</th><th>Alerts</th></tr>
{body_rows}
</table>
</body></html>"""
    return HTMLResponse(content=page_html)

@app.get("/v1/metrics")
def get_metrics(request: Request):
    """Owner-only telemetry: funnel counters, revenue events, and reach —
    same X-Al-Admin guard as /v1/agents."""
    admin_secret = os.environ.get("AL_ADMIN_SECRET", "")
    if not admin_secret or not hmac.compare_digest(request.headers.get("x-al-admin", ""), admin_secret):
        raise HTTPException(401, "owner only")
    snap = metrics.snapshot()
    totals = snap["totals"]
    last_24h = snap["last_24h"]
    unique_ips = snap["unique_ip_hashes"]
    amount_sums = snap["amount_cents_sum"]

    # Durable counts, so a fresh deploy does not read as "distribution stopped"
    # (in-memory totals reset to zero on boot — see metrics.funnel_from_file).
    try:
        durable = metrics.funnel_from_file()
    except Exception:
        durable = {}
    try:
        durable_amounts = metrics.amount_sums_from_file()
    except Exception:
        durable_amounts = {}

    def _durable(name: str) -> int:
        # `name` is always a key of FUNNEL_KINDS / a fixed module constant, never
        # request-derived — this reads counters, it persists nothing.
        # History can exceed the in-memory counter (which only counts since
        # boot), never the other way round on a single-instance service.
        return max(totals.get(name, 0), durable.get(name, 0))

    funnel = {k: {"total": _durable(k), "last_24h": last_24h.get(k, 0)}
              for k in FUNNEL_KINDS}

    checkout_funnel = {
        "checkout_started": {"total": _durable("checkout_started"),
                              "last_24h": last_24h.get("checkout_started", 0)},
        "checkout_completed": {"total": _durable("checkout_completed"),
                                "last_24h": last_24h.get("checkout_completed", 0)},
        "revenue_events_completed": _durable("checkout_completed"),
        "sum_amount_cents_completed": max(amount_sums.get("checkout_completed", 0),
                                          durable_amounts.get("checkout_completed", 0)),
    }

    # File-backed, so the reach number survives a deploy instead of resetting
    # to zero (the in-memory set is process-lifetime). Falls back to memory if
    # the file can't be read.
    try:
        durable_reach = metrics.reach_from_file()
    except Exception:
        durable_reach = {}
    reach = {path: max(durable_reach.get(f"path:{path}", 0), unique_ips.get(f"path:{path}", 0))
             for path in sorted(REACH_PATHS)}

    # The step funnel (D-1163): where do signups actually stop?
    onboarding = {}
    try:
        onboarding = metrics.onboarding_funnel()
    except Exception:
        pass

    agents = list_agents()
    with_data = sum(1 for a in agents if a.get("has_data"))
    squatted = sum(1 for a in agents if not a.get("has_data"))

    return {
        "service": "agent-ledger",
        "version": app.version,
        "onboarding": onboarding,
        "funnel": funnel,
        "checkout_funnel": checkout_funnel,
        "reach": reach,
        "agents": {
            "claimed": claimed_agent_count(),
            "with_data": with_data,
            "squatted": squatted,
            "cap": None if pro_active() else BETA_AGENT_CAP,
        },
    }

# /stats is fetched by the status page on EVERY page view — cache it so viral
# reader traffic doesn't burn CPU (Railway usage pricing = CPU × traffic).
_stats_cache = {"ts": 0.0, "payload": None}
_stats_lock = _threading.Lock()
STATS_CACHE_TTL = 60.0  # seconds; fine for a funnel counter

@app.get("/stats")
def stats():
    now = _time.time()
    if _stats_cache["payload"] is not None and now - _stats_cache["ts"] < STATS_CACHE_TTL:
        return _stats_cache["payload"]
    with _stats_lock:
        now = _time.time()
        if _stats_cache["payload"] is not None and now - _stats_cache["ts"] < STATS_CACHE_TTL:
            return _stats_cache["payload"]
        from collections import Counter
        c = Counter()
        if COUNTS_FILE.exists():
            # counts.jsonl grows one line per track call forever — read only the
            # tail (last 50K lines) for the counter; older events have negligible
            # effect on a funnel display and unbounded reads cost CPU on every miss
            lines = COUNTS_FILE.read_text().splitlines()
            for line in lines[-50000:]:
                try:
                    c[json.loads(line).get("kind", "?")] += 1
                except (json.JSONDecodeError, KeyError):
                    continue
        # scarcity_claims_left is deliberately NOT served any more: it counted
        # the launch window, and the human self-serve path (/start) does not
        # grant it. A public count for an offer that signup cannot receive is a
        # promise the product does not keep. (Adversarial/editorial review
        # 2026-09-13; the grant was retired in code and left advertised in five
        # places.)
        payload = {"tracked_agents": claimed_agent_count(), "events": dict(c)}
        _stats_cache["payload"] = payload
        _stats_cache["ts"] = _time.time()
        return payload

LLMS_TXT = """# AgentLedger

Per-agent spend management — the Datadog for agent spending. Track spend
across x402/MPP/API-key rails, budget caps, anomaly alerts, audit trails.

Payment acceptance (2026-09-17): POST /v1/billing/x402 accepts x402 payment
(PAYMENT-SIGNATURE / X-PAYMENT header). {MPP_ACCEPTANCE}
The custom `x402-base` method is not Stripe/Tempo — this service does not
hold Stripe/Tempo settlement credentials.

Machine-readable schema: GET /openapi.json (OpenAPI 3) · MCP manifest: GET /server.json
Human/agent status page: GET /status (live health, version, uptime, counters)
Buyer skill (how an agent buys this, as markdown): GET /skill.md
Privacy: GET /privacy · Terms: GET /terms

Data handling: cost metadata only — agent id, rail, service label, amount, token
counts, model, timestamp. There is no field for prompt or response content; none
is collected and none is stored. Prompts and responses are never used to train
models, and your data is never sold.

## Ownership (a workspace_key claims; an agent_secret writes)

Claiming a NEW agent_id requires a workspace_key in the body of the first
write (POST /v1/track or /v1/budget). Get one self-serve with no human at
all by paying via POST /v1/billing/x402 (the paying wallet becomes the
workspace identity — {X402_PASS_OFFER}), or by
POST /start — no signup, no login, no card, but capped at 3 agents. (GET /start
renders the form only; it does not mint.)
{X402_SETTLEMENT} {MPP_ACCEPTANCE} Missing or invalid key on a new claim gets 401
workspace_key_required.
That first write mints an `agent_secret` and returns it once, e.g.
{"agent_secret": "...", "_note": "..."}. Save it — every later write to that
same agent_id must include it in the body as "agent_secret" (the
workspace_key is never needed again for that agent), or the request is
rejected with 401. Reads /v1/report, /v1/tokens, and /v1/alerts all require
an X-Agent-Secret or X-Workspace-Key header (either credential proving
access to that agent_id) — missing/wrong gets 401. There is no
unauthenticated read path, on REST or MCP.
A free workspace is capped at 3 agents; a 4th new
agent_id gets 402 with two upgrade paths in the error body: pay via x402{MPP_OR}
yourself for a time-boxed Pro pass ({X402_PASS_OFFER}),
or a human upgrades the workspace to Pro ($19/mo, unlimited, no expiry) via
the Stripe link the same error returns. The cap is
per workspace, not site-wide. Amounts per entry are capped at $100,000 and
must be >= 0.
Setting a budget makes it enforced going forward: a track() entry that would
cross the monthly/daily cap is rejected with 402, not just logged.
Dollar caps (monthly_cents/daily_cents) and token caps (monthly_tokens/
daily_tokens) are independent: dollar caps only cover non-"tokens" rails;
token caps only cover rail="tokens" bookkeeping rows. A token-metered agent
(flat-rate billing, no dollar amount per call) needs a token cap set —
a dollar cap alone does not protect it. See ledger_api_docs("budget").

## Validation rules (enforced on every write)

- agent_id: 1-64 chars, letters/digits/./_/-, must start alphanumeric.
  Path traversal ("../", "/", leading dot) is rejected with 422.
- rail: exactly one of "mpp", "x402", "api_key", "manual" (other values → 422).
  The special rail value "tokens" is reserved for internal token-burn
  bookkeeping rows: amount_cents must be 0.
- amount_cents: integer 0-10000000 ($0-$100,000 per entry).
- monthly_cents / daily_cents (budget): integers 0-10000000.

## Endpoints

GET  /health                       — liveness
POST /v1/track                     — record a spend entry (mints/verifies agent_secret)
     body: {"agent_id": str, "rail": str,
            "amount_cents": int (0-10000000) — OPTIONAL: omit it and send
                            tokens_in/tokens_out + model, and the amount is computed from the
                            price table (GET /v1/pricing). An unpriced model is refused with
                            422 model_not_priced rather than recorded as zero.
            "service": str — OPTIONAL when auto-pricing; defaults to the model's provider
            "tokens_in": int (optional), "tokens_out": int (optional), "model": str (optional),
            "workspace_key": str (required to CLAIM a new agent_id),
            "agent_secret": str (required after the first call for this agent_id)}
POST /v1/budget                    — set budget caps (mints/verifies agent_secret); once set,
                                      track() blocks entries that would cross the cap
     body: {"agent_id": str, "monthly_cents": int (0-10000000), "daily_cents": int (optional, 0-10000000),
            "monthly_tokens": int (optional, token-burn cap), "daily_tokens": int (optional, token-burn cap),
            "workspace_key": str (required to CLAIM a new agent_id),
            "agent_secret": str (required after the first call for this agent_id)}
GET  /v1/report/{agent_id}         — spend report (query: days=30) — requires X-Agent-Secret or X-Workspace-Key
POST /v1/report/{agent_id}/share   — mint a read-only, EXPIRING link to the human report page
                                      (agent_secret OR workspace_key; default 7 days, max 90)
POST /v1/report/{agent_id}/share/revoke — kill every outstanding share link for this agent
GET  /v1/report/{agent_id}/html?t=<token> — the shared page itself. A BROWSER CANNOT SEND A
                                      HEADER, which is why this exists: open this URL directly,
                                      no credential, no curl. Bad/expired/revoked token returns a
                                      styled HTML error page, never raw JSON. The token reads that
                                      ONE agent_id only — it cannot write, rotate, or read others.
GET  /v1/tokens/{agent_id}         — token burn report: in/out totals + by model (query: days=30) — requires X-Agent-Secret or X-Workspace-Key
GET  /v1/alerts/{agent_id}         — alerts for agent — requires X-Agent-Secret or X-Workspace-Key
POST /v1/agents/{agent_id}/rotate-secret — RECOVER a lost agent_secret: mints a new one, kills the old
                                      (workspace_key ONLY — never the agent_secret itself; 404 if unclaimed,
                                      403 if the agent belongs to another workspace)
POST /v1/agents/{agent_id}/revoke-secret — invalidate an agent's secret, keep its ledger
                                      (workspace_key ONLY; the agent_id stays claimed, so no other
                                      workspace can take it over; rotate back in when you want it writing)
POST /v1/webhooks                  — register an alert destination for this workspace
                                      (X-Workspace-Key; http(s) URL only — no email rail)
GET  /v1/webhooks                  — list this workspace's destinations
GET  /v1/webhooks/deliveries       — delivery receipts, successes AND failures
DELETE /v1/webhooks/{id}           — remove one destination
                                      Events: alert.raised, budget.warning (80%), budget.exceeded,
                                      anomaly.detected. Omit events to get all. Payloads carry cost
                                      metadata only — never a secret, prompt, or response.
GET  /v1/agents                    — owner-only: full cross-tenant listing (requires X-Al-Admin header)
GET  /stats                        — usage counters
POST /v1/billing/x402              — self-serve workspace minting for an agent with a wallet
                                      (X-PAYMENT header; the paying wallet IS the identity)
                                      {X402_SETTLEMENT}
                                      Use POST /start instead if you have no wallet
                                      (GET /start only renders the form).
GET  /start                        — renders the start form (no signup, no login);
                                      does NOT mint. POST /start mints one and
                                      shows the workspace_key once.

## Getting started (the shortest path)

    pip install "aiagentscity-ledger[wrapper]"   # or: uvx --from git+https://github.com/entradox/agent-ledger agent-ledger init
    agent-ledger init                   # mints a workspace, claims an agent, writes .env

then, in your code:

    from openai import OpenAI
    import agentledger
    client = agentledger.wrap(OpenAI(api_key=OPENAI_KEY),
                              agent_id="<AGENT_LEDGER_AGENT_ID from .env>",
                              agent_secret="<from .env>")

Every call now goes through the proxy: metered, and refused before the provider
is contacted if it would cross a cap. Streaming, tool calls and retries are
unchanged — wrap() only repoints the base URL and adds two headers; it does not
patch or subclass the SDK. Anthropic's client works the same way.

`agent-ledger share --agent-id <AGENT_LEDGER_AGENT_ID from .env> --agent-secret <secret>` prints a
read-only link anyone can open in a browser.

## Proxy (enforcement — a cap that stops money, not just a record)

POST /proxy/{provider}/{path}     — provider = openai, anthropic, or ANY provider listed in
                                    providers.json (deepseek and moonshot ship as examples).
                                    Adding a vendor is a config edit, not a code change — spend must
                                    be meterable regardless of which vendor an agent calls.
                                    {path} is the provider's own path, e.g. /proxy/openai/v1/chat/completions
     headers: X-AL-Agent: <agent_id>       (who gets billed)
              X-AL-Secret: <agent_secret>  (proves you may write for it)
              Authorization / x-api-key:   YOUR provider credential — forwarded, NEVER stored
     flow: identify -> ESTIMATE the call's max cost -> if it would cross a cap, 402 to YOU and the
           provider is never contacted -> otherwise forward -> meter from the provider's real
           `usage` tokens
     PASS-THROUGH: AgentLedger holds no provider key. A caller can bypass the proxy entirely, and
     traffic that does is not enforced. Nothing here claims otherwise.
     An UNPRICED model is never blocked: the call passes through and an alert fires, because a
     silent zero would read as "this agent spends nothing".
     PRICING IS KEYED BY MODEL, not by provider — the same model costs the same through any
     carrier, so a caller cannot route around its own price by switching endpoints. Where a
     provider reports cached input (DeepSeek: prompt_cache_hit_tokens), the cache-hit rate is
     used; ignoring it would overstate a cache-heavy workload by roughly 50x.
GET  /v1/pricing                  — the price table in use + provenance (open read). Unverified
                                    entries are placeholders: check them against your provider.

## MCP

Registry: io.github.entradox/agent-ledger
Remote:   https://aiagentscity.com/mcp/

Tools exposed at POST /mcp/:
  ledger_track          — record a spend entry (workspace_key to claim, agent_secret after)
  ledger_set_budget     — set a budget cap (workspace_key to claim, agent_secret after)
  ledger_report         — get a spend report (agent_secret or workspace_key param)
  ledger_alerts         — get alerts for an agent (agent_secret or workspace_key param)
  ledger_rotate_secret  — recover a lost agent_secret (workspace_key param; old secret dies at once)
  ledger_revoke_secret  — invalidate an agent's secret without deleting its history (workspace_key param)
  ledger_list_agents    — owner-only (admin_secret param)
  ledger_api_docs       — self-serve docs by topic: quickstart|mcp|rest|budget|errors|idempotency|metering|all (open read)
  ledger_examples       — runnable recipe by pattern: python_tracking|budget_enforcement|weekly_report|retry_safe_writes (open read)

Note: MCP and REST are credential-equivalent. ledger_report/ledger_alerts
take agent_secret/workspace_key parameters and enforce the same access rule
as GET /v1/report and GET /v1/alerts — there is no unauthenticated read path
on either surface. Only the meta-doc tools (ledger_api_docs,
ledger_examples) are open, and they expose no agent data.

Every /v1/* REST write (POST /v1/track, POST /v1/budget) must send
AL-API-Version: {AL_API_VERSION} — missing/invalid values are rejected with 400.
The /mcp/ endpoint itself does not require this header (MCP tool calls are
not version-gated).
POST /v1/track and POST /v1/budget accept an optional Idempotency-Key header
(<=255 chars) for at-most-once retries.

Pricing is as described above (free tier = 3 agents per workspace; Pro =
$19/mo, unlimited agents).
Contact: entradox@icloud.com
"""
LLMS_TXT = LLMS_TXT.replace("{AL_API_VERSION}", AL_API_VERSION)

@app.get("/.well-known/glama.json")
def glama_claim():
    """Glama HTTP-challenge ownership verification file."""
    return JSONResponse(content={
        "$schema": "https://glama.ai/mcp/schemas/connector.json",
        "claim": "glama_claim_waz6O2PC6GoDz7HpLpGshyVk0UGWUo9i",
    }, media_type="application/json")

@app.get("/llms.txt", response_class=PlainTextResponse)
def llms_txt():
    """Served with the MPP claims derived per request, so the document cannot
    advertise a rail that this deployment has not actually enabled."""
    return (LLMS_TXT
            .replace("{MPP_ACCEPTANCE}", _mpp_acceptance_words())
            .replace("{MPP_OR}", _mpp_or_words()))

ROBOTS_TXT = """User-agent: *
Allow: /
Sitemap: https://aiagentscity.com/sitemap.xml
"""

@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt():
    return ROBOTS_TXT

@app.get("/server.json")
def server_json():
    """MCP server discovery manifest at the canonical root path."""
    p = Path(__file__).parent / "server.json"
    if not p.exists():
        raise HTTPException(404, "server.json not deployed")
    return JSONResponse(content=json.loads(p.read_text()))

@app.get("/.well-known/mcp/server-card.json")
def mcp_server_card():
    """Static MCP server card (SEP-1649 shape).

    Generated from the live tools/list rather than hand-written, so directory
    scanners that cannot complete an automated scan (auth wall, WAF, bot rules)
    still get accurate tools + schemas. Smithery and similar registries read
    this path when scanning is blocked.
    """
    p = Path(__file__).parent / "server-card.json"
    if not p.exists():
        raise HTTPException(404, "server-card.json not deployed")
    return JSONResponse(content=json.loads(p.read_text()))

# ── Discovery constants ──────────────────────────────────────────────────
# Values are read from the same module the x402 verifier uses, so the published
# discovery document cannot drift from what the endpoint actually charges.
# Wrapped in a helper because x402_verify may fail to import on a dev box
# (no SDK / no wallet configured) — discovery must still answer.
#
# USDC differs per network, so this MUST be network-derived. Sending a payer the
# testnet token address on a mainnet network (or the reverse) yields a payment
# that cannot settle: the wrong asset. Both addresses are the canonical USDC
# contracts (mainnet verified against the x402 supported-assets endpoint).
X402_USDC_BY_NETWORK = {
    "eip155:84532": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",  # Base Sepolia
    "eip155:8453": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",   # Base mainnet
}
X402_MAINNET_NETWORKS = {"eip155:8453"}

_X402_NETWORK_FALLBACK = "eip155:84532"      # Base Sepolia, the configured default


def _x402_is_mainnet() -> bool:
    """True when the payment endpoint settles on a mainnet network.

    The previous check was `== "eip155:845"`, which is WRONG: Base mainnet is
    eip155:8453 (845 was the old chain id, still used by some RPC endpoints).
    So `mainnet` reported false even when running on mainnet — the published
    discovery document contradicted the live network. Set-membership on the
    CAIP-2 id is the correct test.
    """
    return _x402_network() in X402_MAINNET_NETWORKS


def _x402_settlement_words() -> str:
    """Honest one-line description of where x402 settles, for agent-facing text.

    Discovery prose used to hardcode "TESTNET ONLY ... a mainnet wallet cannot
    complete it". That is false the moment X402_NETWORK flips, and it would tell
    agents to stay away from a working mainnet payment path. Derived, not
    literal, so it can never contradict the configured network.
    """
    net = _x402_network()
    if net in X402_MAINNET_NETWORKS:
        return (f"settles on Base MAINNET ({net}) in real USDC. The paying "
                "wallet pays real money; the workspace is live immediately.")
    return (f"settles on Base Sepolia TESTNET ({net}) with testnet USDC, so a "
            "mainnet wallet cannot complete it — use POST /start if you hold no "
            "testnet wallet (GET /start only renders the form).")


def _x402_settlement_class() -> str:
    """Wrapper class for the settlement sentence on the landing page.

    On mainnet the sentence describes a working payment path, so it is
    informational; on testnet it is a genuine warning that a mainnet wallet
    cannot pay. Derived alongside the words: a literal class would be one more
    thing that can disagree with the configured network — the defect D-1312
    removed from the sentence itself.
    """
    return "note" if _x402_is_mainnet() else "warnnote"


def _x402_settlement_span() -> str:
    """The settlement sentence as a styled <span>, class derived per network.

    Distinct from `_x402_settlement_words()` (the bare sentence, which
    `{X402_SETTLEMENT_HTML}` has always meant on /start and the key page).
    This placeholder carries its own wrapper because the wrapper depends on the
    network too, and a literal class would be the same defect in CSS.
    """
    return (f'<span class="{_x402_settlement_class()}">'
            f'{_x402_settlement_words()}</span>')


def _x402_pass_offer_words() -> str:
    """One-line description of what an x402 payment actually buys (D-1270).

    Price and duration are read from x402_verify, the one place that
    configures them, so this can never drift from the real offer the way the
    hardcoded "$19/mo, unlimited agents" cap-upgrade line did before a
    second, cheaper, no-human path existed.
    """
    import x402_verify
    hours = round(x402_verify.X402_PRO_PASS_SECONDS / 3600)
    return (f"{x402_verify.X402_MINT_PRICE} via x402 buys {hours}h of Pro "
            "(unlimited agents) on the workspace your wallet resolves to — "
            "no card, no human, pay again any time to extend it")


def _x402_asset() -> str:
    """The USDC contract for the configured network."""
    return X402_USDC_BY_NETWORK.get(_x402_network(),
                                    X402_USDC_BY_NETWORK["eip155:84532"])


# The settlement sentence is network-dependent, so it is substituted into the
# agent-facing documents rather than hardcoded. The previous literal
# "TESTNET ONLY ... a mainnet wallet cannot complete it" became false the moment
# X402_NETWORK changed, and would have told agents to avoid a working mainnet
# payment path. Derived here, after the helpers, but still at import time.


def _x402_network() -> str:
    """The network the payment endpoint actually settles on."""
    try:
        import x402_verify
        return x402_verify.X402_NETWORK or _X402_NETWORK_FALLBACK
    except Exception:
        return _X402_NETWORK_FALLBACK


def _x402_pay_to():
    """The receiving wallet, or None when x402 is unconfigured."""
    try:
        import x402_verify
        return x402_verify.X402_PAY_TO or None
    except Exception:
        return None


# Computed here, after every helper it calls is defined (_x402_settlement_words
# -> _x402_network), and still at import time so the replacement below sees it.
X402_SETTLEMENT = _x402_settlement_words()
LLMS_TXT = LLMS_TXT.replace("{X402_SETTLEMENT}", X402_SETTLEMENT)
X402_PASS_OFFER = _x402_pass_offer_words()
LLMS_TXT = LLMS_TXT.replace("{X402_PASS_OFFER}", X402_PASS_OFFER)


def _mpp_acceptance_words() -> str:
    """The MPP half of the payment-acceptance sentence, derived PER REQUEST.

    Derived, not frozen at import. A static "accepts MPP" string is served even
    when MPP_SECRET_KEY is unset (or no receiving address is configured), so the
    document would advertise a payment method the endpoint cannot actually
    accept — the stale-claim defect class this project has shipped five times.
    Every other payment claim on these surfaces (price, network, asset) is
    already derived per request; this one was the exception.

    It also states the credential contract, because the `x402_header` field is
    THIS SERVICE'S OWN invention — not part of the MPP standard and not carried
    in the challenge — so a generic MPP client cannot guess it.
    """
    try:
        import mpp_verify
        enabled = bool(mpp_verify.MPP_ENABLED)
    except Exception:  # noqa: BLE001 — never let prose break a served doc
        enabled = False
    if not enabled:
        return ("MPP is NOT enabled on this deployment, so only x402 payment is "
                "accepted here.")
    return ("It also accepts an MPP credential (Authorization: Payment "
            "<base64 credential>) via the custom `x402-base` method, settling "
            "on the same rail. The credential payload must carry the signed "
            "x402 payment under the key \"x402_header\" — that field is this "
            "service's own contract, not part of the MPP standard. MPP is NOT "
            "live-verified yet: the challenge passes Stripe/Tempo's validator, "
            "but no MPP payment has settled funds end-to-end.")


def _agents_mpp_status() -> str:
    """The /agents.txt MPP status line, derived PER REQUEST.

    Same defect as the llms.txt claim, in a second surface: this line was a
    frozen import-time string reading "MPP is ACCEPTED", so it was served even
    when MPP_SECRET_KEY is unset and no WWW-Authenticate challenge is emitted.
    A served surface must never advertise a payment method the running
    deployment cannot accept — verified by rendering /agents.txt in a
    production-shaped env (mainnet + CDP creds + no MPP secret): llms.txt
    correctly said "MPP is NOT enabled" while /agents.txt still said "ACCEPTED".
    """
    try:
        import mpp_verify
        if not mpp_verify.MPP_ENABLED:
            return ("MPP is NOT enabled on this deployment (only x402 payment is "
                    "accepted here).")
        return ("MPP is offered via the custom `x402-base` method (same Base USDC "
                "rail); the challenge is validated but the rail has not yet "
                "settled a payment end-to-end. Not Stripe/Tempo directly (no "
                "settlement credentials for those rails).")
    except Exception:  # noqa: BLE001 — never let prose break a served doc
        return "MPP status unavailable."


def _mpp_or_words() -> str:
    """' or MPP' only where MPP is really enabled (else the sentence over-promises)."""
    try:
        import mpp_verify
        return " or MPP" if mpp_verify.MPP_ENABLED else ""
    except Exception:  # noqa: BLE001
        return ""


# ── The buyer skill, published where a machine surface can find it (D-1313) ──
# The validated x402 buyer skill had no home: /skill, /skills, /buyer-skill and
# /SKILL.md were all 404, and /docs is FastAPI's Swagger UI, not a docs site.
# Principal decision (2026-09-16): serve it at GET /skill.md as markdown and link
# it from llms.txt.
#
# The file itself is VERSIONED IN THE REPO, not inlined here, so the published
# text is reviewable in a diff. It is served with every live value SUBSTITUTED
# rather than pasted, for the same reason D-1312 derived the settlement prose:
# this project has shipped a stale x402 literal three times (`eip155:845`, the
# testnet asset, the stale mainnet copy), and a price/network/asset baked into
# prose is simply a fourth chance to do it again.
#
# NOTE: skill/ is NOT exempt in .railwayignore — that filter's `*.md` rule
# excludes every markdown file from the build context (only README.md is
# negated). A skill file that ships locally but not in the image would 404 in
# production while every local test stayed green, so the file is explicitly
# un-ignored there. Keep the two in sync.
SKILL_FILE = Path(__file__).parent / "skill" / "agent-ledger-buyer.md"


def _x402_network_label() -> str:
    """Human-readable name for the configured network.

    Derived, not literal: "Base mainnet" hardcoded would be false the moment
    X402_NETWORK is flipped back to a testnet for a rehearsal.
    """
    return "Base mainnet" if _x402_is_mainnet() else "Base Sepolia testnet"


def _x402_amount_atomic() -> str:
    """The x402 price in USDC atomic units (6 decimals) — what the 402 carries."""
    import x402_verify
    return str(x402_verify.x402_mint_price_atomic())


def _skill_markdown() -> str:
    """The buyer skill with every live value substituted, read per request.

    Per request rather than frozen at import, matching the /agent-ledger
    pattern, so the served document cannot contradict the running config.
    """
    import x402_verify
    pay_to = _x402_pay_to()
    text = SKILL_FILE.read_text(encoding="utf-8")
    for token, value in (
        ("{X402_NETWORK}", _x402_network()),
        ("{X402_NETWORK_LABEL}", _x402_network_label()),
        ("{X402_ASSET}", _x402_asset()),
        ("{X402_AMOUNT_ATOMIC}", _x402_amount_atomic()),
        ("{X402_PRICE_USD}", f"{float(str(x402_verify.X402_MINT_PRICE).lstrip('$')):.2f}"),
        # Omitted entirely when x402 is unconfigured, so the sentence stays
        # grammatical instead of rendering an empty pair of backticks.
        ("{X402_PAY_TO_NOTE}", f" (`{pay_to}`)" if pay_to else ""),
        ("{X402_PASS_HOURS}", str(round(x402_verify.X402_PRO_PASS_SECONDS / 3600))),
        ("{FREE_AGENT_CAP}", str(BETA_AGENT_CAP)),
        ("{AL_API_VERSION}", AL_API_VERSION),
    ):
        text = text.replace(token, value)
    return text


@app.get("/skill.md")
@app.get("/skill")
def skill_md():
    """The validated x402 buyer skill as markdown, for an agent to actually use.

    Served at /skill too: /.well-known/x402 and /x402.json set the precedent of
    answering the extensionless spelling a prober tries first, and one function
    serves both, so the two spellings cannot drift apart.

    text/markdown (not text/plain) because the body IS markdown — declaring it
    plain invites a client to show raw source. A missing file is a 404, never a
    500: a deploy that omitted it must not read as a server fault.
    """
    if not SKILL_FILE.is_file():
        raise HTTPException(404, "buyer skill not deployed")
    return Response(_skill_markdown(),
                    media_type="text/markdown; charset=utf-8")


AGENT_JSON = {
    "schema_version": "1.0",
    "name": "AgentLedger",
    "description": "Per-agent spend management: track spend across x402/MPP/API-key "
                    "rails, set budget caps, get anomaly alerts, keep an audit trail.",
    "url": "https://aiagentscity.com",
    "api_base": "https://aiagentscity.com/v1",
    "openapi": "https://aiagentscity.com/openapi.json",
    "auth": {
        "type": "workspace_key",
        "field": "workspace_key",
        "description": "Get a workspace_key with no human at all by paying via "
                        "POST /v1/billing/x402 (the paying wallet becomes the workspace "
                        "identity), or by POST /start — no signup, no login, no card "
                        "(GET /start renders the form only; it does not mint). "
                        f"NOTE: /v1/billing/x402 {X402_SETTLEMENT} "
                        "Claiming a NEW agent_id "
                        "requires that workspace_key in the first POST /v1/track or "
                        "/v1/budget body; that call mints an agent_secret in the "
                        "response — save it, every later write to that agent_id must "
                        "include it and needs no workspace_key. Reads "
                        "(report/alerts/tokens) require agent_secret or workspace_key, "
                        "sent as X-Agent-Secret or X-Workspace-Key. A LOST agent_secret "
                        "is recoverable — the workspace_key can always mint a new one at "
                        "POST /v1/agents/{agent_id}/rotate-secret, so losing a credential never "
                        "bricks an agent_id.",
    },
    "pricing": {
        "model": "freemium",
        "amount_usd": 19.00,
        "description": "Free tier is 3 agents per workspace, no expiry. Two paid paths "
                        "to unlimited agents beyond that: an agent can pay via x402 "
                        "itself for a time-boxed Pro pass, or a human subscribes at "
                        "$19/mo for Pro with no expiry. See capabilities below for the "
                        "x402 price and pass duration.",
    },
    "capabilities": [
        {"id": "mint_workspace_x402",
         "description": "Self-serve workspace + workspace_key for an agent with a "
                        "wallet — no human, no login. Present an x402 payment in the "
                        "X-PAYMENT header; the paying wallet becomes the workspace "
                        f"identity. {X402_PASS_OFFER} Do this first: a workspace_key "
                        f"is required to claim a new agent_id. {X402_SETTLEMENT} "
                        "If you hold no wallet, mint at POST /start instead (no "
                        "signup, no card, but capped at 3 agents; GET /start only "
                        "renders the form and does not mint).",
         "endpoint": "/v1/billing/x402", "method": "POST", "free": False},
        {"id": "track_spend", "description": "Record a spend entry for an agent",
         "endpoint": "/v1/track", "method": "POST", "free": True},
        {"id": "set_budget", "description": "Set monthly/daily budget caps for an agent",
         "endpoint": "/v1/budget", "method": "POST", "free": True},
        {"id": "get_report", "description": "Spend report — totals, by rail, by service, anomalies",
         "endpoint": "/v1/report/{agent_id}", "method": "GET", "free": True},
        {"id": "get_alerts", "description": "Anomaly alerts for an agent",
         "endpoint": "/v1/alerts/{agent_id}", "method": "GET", "free": True},
        {"id": "get_tokens", "description": "Token burn report — in/out totals by model",
         "endpoint": "/v1/tokens/{agent_id}", "method": "GET", "free": True},
        {"id": "rotate_agent_secret",
         "description": "Recover a LOST agent_secret: mint a new one for an agent_id you own, "
                        "invalidating the previous credential immediately. Workspace_key only — "
                        "an agent_secret cannot rotate itself. 404 if the agent_id is not claimed.",
         "endpoint": "/v1/agents/{agent_id}/rotate-secret", "method": "POST", "free": True},
        {"id": "route_through_proxy",
         "description": "Point your provider base_url at /proxy/{provider} — openai, anthropic, or "
                        "any vendor listed in providers.json (deepseek, moonshot ship as examples; "
                        "adding one is a config edit). Every call is metered "
                        "and capped BEFORE it reaches the provider: a call that would cross the "
                        "budget gets 402 to the caller and the provider is never contacted. "
                        "Pass-through — send your provider credential in Authorization / x-api-key "
                        "and AgentLedger forwards it without storing it. Traffic that bypasses the "
                        "proxy is not enforced.",
         "endpoint": "/proxy/{provider}/{path}", "method": "POST", "free": True},
        {"id": "register_alert_webhook",
         "description": "Register an http(s) webhook for this workspace's alerts — "
                        "budget.warning at 80%, budget.exceeded when a write is actually blocked, and "
                        "anomaly.detected. Delivered on the event with retries; every attempt is "
                        "recorded, including failures. Cost metadata only, never prompt content.",
         "endpoint": "/v1/webhooks", "method": "POST", "free": True},
        {"id": "share_report_link",
         "description": "Mint a read-only, expiring URL for an agent's human-readable report page — "
                        "a browser cannot send X-Agent-Secret as a header, so this is how a report "
                        "reaches a person. Scoped to ONE agent_id, read-only, expiring (7 days "
                        "default, 90 max), revocable in bulk.",
         "endpoint": "/v1/report/{agent_id}/share", "method": "POST", "free": True},
        {"id": "revoke_agent_secret",
         "description": "Invalidate an agent's secret while keeping its spend history. The "
                        "agent_id stays claimed, so no other workspace can take it over.",
         "endpoint": "/v1/agents/{agent_id}/revoke-secret", "method": "POST", "free": True},
    ],
    "contact": "entradox@icloud.com",
    "legal": ("Parmanand LLC. Beta software, provided as-is. "
              "Privacy: /privacy · Terms: /terms. Cost metadata only — prompts "
              "and responses are never stored."),
}

@app.get("/.well-known/agent.json")
def agent_json():
    """AEO capability manifest: agents discover what this product does,
    pricing, auth, and how to call it (rules/agent-native-standard.md)."""
    return JSONResponse(content=AGENT_JSON)


@app.get("/.well-known/agent-card.json")
def agent_card_json():
    """Alias of /.well-known/agent.json.

    Agents request this spelling 136 times in the live log (2026-09-14) and got
    a 404 every time. `agent-card.json` is the name used by several crawler
    families. Serving the same manifest under every spelling an agent actually
    asks for beats making them learn ours. Same content, no second source of
    truth: this returns AGENT_JSON, it does not copy it.
    """
    return JSONResponse(content=AGENT_JSON)


@app.get("/.well-known/mcp.json")
def mcp_wellknown_json():
    """MCP server descriptor at the spelling agents probe (23 live 404s).

    Distinct from /.well-known/mcp/server-card.json (which is the richer
    registry card). This is the shorthand form crawlers look for.
    """
    return JSONResponse(content={
        "name": "io.aiagentscity/agent-ledger",
        "title": "AgentLedger",
        "description": "Per-agent spend management: track spend across "
                       "x402/MPP/API-key rails, set budget caps, get anomaly "
                       "alerts, keep an audit trail.",
        "version": APP_VERSION,
        "remotes": [{"type": "streamable-http",
                     "url": "https://aiagentscity.com/mcp/"}],
        "documentation": "https://aiagentscity.com/llms.txt",
        "payment": {"protocol": "x402",
                    "discovery": "https://aiagentscity.com/.well-known/x402",
                    "endpoint": "https://aiagentscity.com/v1/billing/x402",
                    "network": _x402_network(),
                    "mainnet": _x402_is_mainnet()},
    })


@app.get("/.well-known/agents.json")
@app.get("/agents.json")
@app.get("/.well-known/agent-directory.json")
@app.get("/agent-directory.json")
def agents_directory_aliases():
    """Every spelling of the agent manifest that live crawlers actually request.

    Counted in /data/metrics.jsonl (2026-09-15): /.well-known/agents.json 30x,
    /.well-known/agent-directory.json 28x, /agent-directory.json 28x,
    /agents.json 22x — all 404. There is no directory concept here, so the honest
    answer is the capability manifest. Returning AGENT_JSON (the single source of
    truth) beats inventing a second document that could drift from it.
    """
    return JSONResponse(content=AGENT_JSON)


@app.get("/.well-known/mcp")
@app.get("/mcp.json")
def mcp_manifest_aliases():
    """Bare-manifest spellings for the MCP descriptor.

    /.well-known/mcp 29x and /mcp.json 29x in the live log, both 404. Delegates to
    the existing /.well-known/mcp.json handler so there is one implementation.
    """
    return mcp_wellknown_json()


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
@app.get("/.well-known/oauth-authorization-server")
@app.get("/.well-known/oauth-authorization-server/mcp")
@app.get("/mcp/.well-known/oauth-protected-resource")
@app.get("/mcp/.well-known/oauth-authorization-server")
def oauth_discovery_absent():
    """Answer the OAuth discovery probes honestly, instead of 404ing them.

    An MCP client that wants authenticated transport walks RFC 9728 / RFC 8414
    discovery before it will connect. Measured 2026-09-16 in /data/metrics.jsonl:
    150 such probes 404'd, including /mcp/-relative variants. A 404 reads as
    "discovery is broken"; it does not read as "no auth here".

    This deployment has no auth layer at all — MCP here is deliberately open
    (`claude mcp add --transport http agent-ledger https://aiagentscity.com/mcp/`
    needs no login). The correct answer is therefore an explicit empty discovery
    document: it tells the client no authorization server exists, so it should
    proceed unauthenticated, and it stops the 404 loop.

    Returning a fake authorization_server here would be worse than the 404: it
    would send a client to an endpoint that cannot issue tokens.
    """
    return JSONResponse(
        status_code=200,
        content={},
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/.well-known/x402")
@app.get("/.well-known/x402.json")
def x402_wellknown_json():
    """The till, published where agents actually look.

    An agent with 1,343 successful MCP calls (`b628ad13e37e`) probed
    /.well-known/x402, /payments, /pricing, /payment and /monetization on
    2026-09-14 and every one returned 404. It was looking for how to pay us
    and we told it nothing. This is the signpost.

    Values are read from the same env the verifier uses, so this cannot drift
    from what the endpoint actually charges.

    Served at BOTH spellings deliberately. x402scan's own spec says it fetches
    `/.well-known/x402` (no extension) first and falls back to `.json`, and its
    `registerFromOrigin` is documented to fail with `noDiscovery` if only one
    variant is served. They are one function, so they cannot disagree.
    """
    import x402_verify
    enabled = bool(getattr(x402_verify, "X402_ENABLED", False))
    # The x402scan compatibility fan-out. Its discovery spec wants
    # {"version": 1, "resources": [...]} — a list of what is payable, which is
    # a DIFFERENT document from a 402 challenge body. Both are served here so a
    # scanner gets what it expects without losing the rich challenge detail the
    # rest of this document already carried.
    content = {
        "version": 1,
        "resources": ["POST https://aiagentscity.com/v1/billing/x402"],
        "x402Version": 2,
        "enabled": enabled,
        "scheme": "exact",
        "network": _x402_network(),
        "mainnet": _x402_is_mainnet(),
        "asset": _x402_asset(),
        "assetSymbol": "USDC",
        "priceUsd": float(str(x402_verify.X402_MINT_PRICE).lstrip("$")),
        "payTo": _x402_pay_to(),
        # Publish the RESOLVED facilitator, not the raw env var: the raw var is
        # now empty by default (it is an explicit override), so publishing it
        # would advertise an empty string to every agent reading the docs while
        # the service actually talks to x402.org or CDP. `mode` is additive, so
        # nothing that already parses this document breaks.
        "facilitator": x402_verify.FACILITATOR_URL_RESOLVED,
        "facilitatorMode": x402_verify.FACILITATOR_MODE,
        "endpoint": "https://aiagentscity.com/v1/billing/x402",
        "method": "POST",
        "howToPay": "POST /v1/billing/x402 with an X-PAYMENT header carrying a "
                    "signed x402 `exact` payment. On success the response "
                    "returns a workspace_id and a workspace_key (shown once), "
                    "bound to the paying wallet, with no human, no email and no card.",
        "returns": ["workspace_id", "workspace_key"],
        "note": (f"{X402_SETTLEMENT} To get a workspace with no wallet at "
                 "all, use POST /start (GET /start only renders the form and "
                 "does not mint)."),
    }
    if not enabled:
        content["disabledReason"] = getattr(x402_verify, "X402_DISABLED_REASON", "unknown")
    return JSONResponse(content=content)


@app.get("/.well-known/payments")
def payments_wellknown_json():
    """Alias for the payment discovery document (agents probe both spellings)."""
    return x402_wellknown_json()


@app.get("/agents.txt", response_class=PlainTextResponse)
def agents_txt():
    """Plain-text agent orientation: the .txt spelling crawlers request.

    Content is derived from AGENT_JSON and the live route set, so it cannot
    assert a capability the API does not have.
    """
    # The price must be DERIVED, never a literal: this function's own docstring
    # promises it "cannot assert a capability the API does not have", and a
    # hardcoded "$0.01" broke exactly that promise the moment X402_MINT_PRICE was
    # configured to anything else (reproduced: served "$0.01" while the same
    # document, three lines later, correctly rendered the derived price).
    import x402_verify
    caps = "\n".join(
        f"  {c['method']:5s} {c['endpoint']:38s} {c['description'].splitlines()[0]}"
        for c in AGENT_JSON.get("capabilities", [])
    )
    return (
        "# AgentLedger agent orientation\n"
        "# https://aiagentscity.com\n"
        "\n"
        "WHAT IT DOES\n"
        "  Per-agent spend management: track spend across x402/MPP/API-key\n"
        "  rails, set budget caps, get anomaly alerts, keep an audit trail.\n"
        "\n"
        "CONNECT (MCP, no install)\n"
        "  claude mcp add --transport http agent-ledger https://aiagentscity.com/mcp/\n"
        "  Both /mcp and /mcp/ work. No login required to connect.\n"
        "\n"
        "GET A WORKSPACE (no human needed)\n"
        "  POST /start                 free workspace, no signup, no card\n"
        "                              (GET /start only renders the form; it does\n"
        "                               not mint)\n"
        f"  POST /v1/billing/x402       pay {x402_verify.X402_MINT_PRICE} in USDC; "
        "wallet IS the identity\n"
        f"                              ({X402_SETTLEMENT})\n"
        "  Discovery: https://aiagentscity.com/.well-known/x402\n"
        "\n"
        "CAPABILITIES\n"
        f"{caps}\n"
        "\n"
        "DISCOVERY\n"
        "  /.well-known/agent.json           full capability manifest\n"
        "  /.well-known/agent-card.json      same manifest (alias)\n"
        "  /.well-known/x402                 payment discovery\n"
        "  /.well-known/mcp/server-card.json MCP registry card\n"
        "  /openapi.json                     OpenAPI 3 spec\n"
        "  /llms.txt                         dense API reference\n"
        "  /skill.md                         buyer skill (x402, markdown)\n"
        "\n"
        "PAYMENT STATUS\n"
        f"  x402 is LIVE and {X402_SETTLEMENT}\n"
        # HONESTY (card criterion 8): do NOT claim MPP is LIVE. The challenge is
        # validated by Stripe/Tempo's own validator, but no MPP payment has ever
        # SETTLED funds end-to-end (needs a funded payer wallet), and the rail is
        # not deployed. "MPP is LIVE" was an over-promise on a served discovery
        # surface — the exact defect class this project has shipped repeatedly.
        f"  {_agents_mpp_status()}\n"
        "\n"
        "CONTACT\n"
        "  entradox@icloud.com\n"
    )


def _status_html() -> str:
    """The /agent-ledger landing page.

    D-1312: this page used to be read straight off disk with no substitution, so
    it kept serving a literal "Testnet only right now ... a mainnet wallet cannot
    complete it" span after mainnet landed — on the same paywall that settles real
    USDC. The sentence is network-derived now, like every other surface: the
    template is static but the substitution runs per request, so the words always
    follow the live configuration.
    """
    return (_status_html_template()
            .replace("{X402_SETTLEMENT_SPAN}", _x402_settlement_span()))


def _status_html_template() -> str:
    return (Path(__file__).parent / "status.html").read_text()


@app.get("/sitemap.xml")
def sitemap_xml():
    """Sitemap for crawlers (69 live 404s on 2026-09-14).

    Lists the public documents a crawler should index: the product and marketing
    pages, the two counter pages, and the prose documents an agent can read.

    D-1314 — the exclusion rule, stated accurately. The previous docstring said
    "API and discovery paths are intentionally excluded because they are not
    indexable content", which was both over-broad and not what the list below
    does (it already lists /docs and /stats). The rule actually applied is about
    CONTENT, not about path shape:

      excluded — transport and descriptor surfaces a crawler gains nothing from:
                 /llms.txt, /agents.txt, /openapi.json, /robots.txt and the
                 /.well-known/* documents.
      included — anything a reader (human or agent) can read as a document.

    /skill.md is included on that rule. It is the x402 buyer skill served as
    text/markdown: prose, not a transport descriptor — the same class of
    indexable content as /docs. Leaving it out would have made the product's
    single most useful document invisible to the crawlers this route exists for.

    The list is hand-maintained. The old docstring claimed it was "built from the
    real route set, not a hand-written list" — that was never true, and it is the
    kind of claim that stops a reader from checking. Adding a path here is a
    deliberate act; the entry must correspond to a route that actually exists.

    Served with the XML media type: a sitemap declared text/plain is ignored by
    some crawlers, which would make the whole route pointless.
    """
    from datetime import date
    pages = [
        ("/",             "1.0", "daily"),
        ("/agent-ledger", "0.9", "weekly"),
        ("/start",        "0.8", "weekly"),
        ("/skill.md",     "0.7", "weekly"),
        ("/status",       "0.6", "daily"),
        ("/stats",        "0.5", "daily"),
        ("/docs",         "0.5", "weekly"),
    ]
    today = date.today().isoformat()
    body = "\n".join(
        f"  <url>\n"
        f"    <loc>https://aiagentscity.com{p}</loc>\n"
        f"    <lastmod>{today}</lastmod>\n"
        f"    <changefreq>{cf}</changefreq>\n"
        f"    <priority>{pr}</priority>\n"
        f"  </url>"
        for p, pr, cf in pages
    )
    return PlainTextResponse(
        content=(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{body}\n"
            "</urlset>\n"
        ),
        media_type="application/xml",
    )


@app.get("/", response_class=HTMLResponse)
def front_door():
    """The AI Agent City umbrella index (D-1239). Until D-1239 this path
    served AgentLedger's own landing page directly — that content now lives
    at /agent-ledger, and this lists all products so the domain reads as the
    umbrella it actually is."""
    import site_pages
    return HTMLResponse(site_pages.city_page(
        "AI Agent City",
        "AI Agent City is a toolbelt for running AI agents in production: "
        "AgentLedger, Perimeter Watch, Cited, Agent Watch and TrustScan.",
        site_pages.UMBRELLA_INDEX))


@app.get("/agent-ledger", response_class=HTMLResponse)
def agent_ledger_page():
    """AgentLedger's own landing page — this is the pre-D-1239 root content,
    unchanged, moved here so the domain root can become the umbrella index."""
    return _status_html()


@app.get("/perimeter-watch", response_class=HTMLResponse)
def perimeter_watch_page():
    import site_pages
    return HTMLResponse(site_pages.city_page(
        "Perimeter Watch — AI Agent City",
        "Passive external-perimeter monitoring for web agencies: cert expiry, "
        "dangling DNS, lookalike domains.",
        site_pages.PERIMETER_WATCH_PAGE))


@app.get("/cited", response_class=HTMLResponse)
def cited_page():
    import site_pages
    return HTMLResponse(site_pages.city_page(
        "Cited — AI Agent City",
        "Does AI recommend your practice? Instant free scan, verbatim evidence.",
        site_pages.CITED_PAGE))


@app.get("/agent-watch", response_class=HTMLResponse)
def agent_watch_page():
    import site_pages
    return HTMLResponse(site_pages.city_page(
        "Agent Watch — AI Agent City",
        "Monitoring for the agent economy.",
        site_pages.AGENT_WATCH_PAGE))


@app.get("/trust-scan", response_class=HTMLResponse)
def trust_scan_page():
    import site_pages
    return HTMLResponse(site_pages.city_page(
        "TrustScan — AI Agent City",
        "Scan before you trust.",
        site_pages.TRUST_SCAN_PAGE))


_BOOT_TS = _time.time()

_LEGAL_SHELL = """<!doctype html><html><head><meta charset="utf-8">
<title>{title} — AgentLedger</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{{background:#0d1117;color:#e6edf3;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;
padding:40px 18px;line-height:1.7}}
.w{{max-width:680px;margin:0 auto}}
.brand{{font-size:12px;color:#8b949e;letter-spacing:.06em;text-transform:uppercase}}
h1{{font-size:24px;margin:8px 0 4px}} h2{{font-size:15px;margin:26px 0 6px}}
p,li{{color:#c9d1d9;font-size:14px}} .mut{{color:#8b949e;font-size:12px}}
a{{color:#79c0ff}}
</style></head><body><div class="w">
<div class="brand">AgentLedger</div>
<h1>{title}</h1>
<div class="mut">Last updated 2026-09-13 · plain-language summary, not legal advice.
Questions: <a href="mailto:entradox@icloud.com">entradox@icloud.com</a></div>
{body}
<p style="margin-top:28px"><a href="/">← AgentLedger</a></p>
</div></body></html>"""

PRIVACY_BODY = """
<h2>What is stored</h2>
<p>Per spend entry: the agent id, the payment rail, the service label, the amount, token counts,
the model name and a timestamp. Per workspace: the workspace id, a hash of the workspace key, the
plan and billing state, and — if you register one — an alert webhook URL. Keys and secrets are stored as hashes or as files readable only by the service.</p>

<h2>What is never stored</h2>
<p>Prompts and model responses. There is no field for them: the ledger records cost metadata, and
the service has no code path that writes message content to storage. That is true of the hosted
ledger today and is a design constraint on the proxy, which will meter cost without retaining
payloads. The <i>service</i> and <i>model</i> fields are free-text labels you supply — do not put
anything sensitive in a service name.</p>

<h2>Who processes it</h2>
<p>Railway hosts the service and its storage volume. Stripe processes payments and receives the
billing details you give it — we never see your card number. Alert webhooks, if you register one,
carry cost metadata to the URL you chose. The product does not send email to arbitrary addresses on
your behalf. Nothing else receives your data.</p>

<h2>What we do not do</h2>
<p>We do not sell, rent or share your data, and we do not use it to train models.</p>

<h2>Retention and deletion</h2>
<p>Ledger data is kept while your workspace exists. Deleting an agent removes its ledger and
secret; the operator can delete a workspace and its data on request. Alert delivery receipts are
kept to a rolling window.</p>

<h2>Cookies and tracking</h2>
<p>The marketing page and API set no advertising cookies. Aggregate counters (workspaces minted,
requests) are collected without storing IP addresses — the service hashes them with a salt and
keeps only the hash.</p>

<h2>Contact</h2>
<p>AgentLedger is operated by Parmanand LLC. Privacy questions and deletion requests:
<a href="mailto:entradox@icloud.com">entradox@icloud.com</a>.</p>
"""

TERMS_BODY = """
<h2>The service</h2>
<p>AgentLedger is beta software provided as-is. It records agent spend, enforces the budget caps you
configure, and reports on both. It is a bookkeeping and guardrail tool — not a payment processor, a
bank, or a financial adviser, and not a guarantee that a third-party provider will not charge you.</p>

<h2>What a budget cap does</h2>
<p>A cap rejects the ledger write that would cross it, with HTTP 402. It stops the <i>recording</i>
of a spend, not the underlying charge, unless the spend went through the AgentLedger proxy. Do not
rely on a cap as your only protection against a runaway agent.</p>

<h2>Your responsibilities</h2>
<p>You are responsible for the credentials you hold (workspace keys, agent secrets, provider API
keys), for what your agents do, and for the accuracy of what they report. Keep your agent secrets
secret; anyone holding one can write to that agent.</p>

<h2>Acceptable use</h2>
<p>Do not use the service to break the law, to attack other systems, or to resell it as your own
without agreement. Per-entry amounts are capped, and rate and storage limits are enforced to keep
the service available for everyone.</p>

<h2>Plans and payment</h2>
<p>The free tier covers 3 agents per workspace. Pro is $19/month for unlimited agents on the same
workspace, billed by Stripe, cancellable at any time.</p>

<h2>Availability and liability</h2>
<p>The service is offered without warranty of uptime or fitness for a particular purpose. To the
extent the law allows, Parmanand LLC's total liability is limited to the amount you paid in the
preceding three months. Nothing here excludes liability that cannot lawfully be excluded.</p>

<h2>Changes</h2>
<p>These terms may change as the product does; material changes will be noted on this page with a
new date. Continued use after a change is acceptance of it.</p>

<h2>Contact</h2>
<p><a href="mailto:entradox@icloud.com">entradox@icloud.com</a></p>
"""


def _health_page() -> str:
    """A real status page.

    Before D-1219 `/status` served the same marketing HTML as `/`, so the
    product had no status page at all. This reads the same counters the API
    serves, so it cannot quietly become decorative.
    """
    up = int(_time.time() - _BOOT_TS)
    hours, rem = divmod(up, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>AgentLedger — status</title><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<style>
body{{background:#0d1117;color:#e6edf3;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;
padding:40px 18px;line-height:1.7}}
.w{{max-width:640px;margin:0 auto}}
.brand{{font-size:12px;color:#8b949e;letter-spacing:.06em;text-transform:uppercase}}
h1{{font-size:22px;margin:8px 0 16px}}
.row{{display:flex;justify-content:space-between;gap:14px;padding:7px 0;
border-bottom:1px solid #21262d;font-size:14px}}
.row:last-child{{border:0}} .k{{color:#8b949e}} .ok{{color:#3fb950}}
.mut{{color:#8b949e;font-size:12px;margin-top:18px}} a{{color:#79c0ff}}
</style></head><body><div class="w">
<div class="brand">AgentLedger status</div>
<h1><span class="ok">●</span> Operational</h1>
<div class="row"><span class="k">service</span><span>agent-ledger</span></div>
<div class="row"><span class="k">version</span><span>{app.version}</span></div>
<div class="row"><span class="k">uptime (this instance)</span><span>{hours}h {minutes}m {seconds}s</span></div>
<div class="row"><span class="k">claimed agents</span><span>{claimed_agent_count()}</span></div>
<div class="row"><span class="k">liveness</span><span><a href="/health">/health</a></span></div>
<div class="row"><span class="k">counters</span><span><a href="/stats">/stats</a></span></div>
<div class="mut">Refreshes every 60s. These are the live counters the API serves, not a static page.
Uptime is per instance — a deploy restarts the process. ·
<a href="/">AgentLedger</a> · <a href="/privacy">Privacy</a> · <a href="/terms">Terms</a></div>
</div></body></html>"""


@app.get("/status", response_class=HTMLResponse)
def status_page():
    """Live health. See _health_page()."""
    return _health_page()


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    return _LEGAL_SHELL.format(title="Privacy", body=PRIVACY_BODY)


@app.get("/img/dashboard.png")
def dashboard_image():
    """The product screenshot on the landing page. Served from disk and cached
    hard: it is a static asset, not a per-request rendering."""
    from fastapi.responses import FileResponse
    p = Path(__file__).parent / "static" / "dashboard.png"
    if not p.exists():
        return JSONResponse({"error": {"type": "not_found"}}, status_code=404)
    return FileResponse(p, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/about")
def about_page():
    """D-1239: /about folded into the umbrella index at "/" — that IS the
    product list now, so this is a redirect rather than a second copy."""
    return RedirectResponse(url="/", status_code=301)


@app.get("/security", response_class=HTMLResponse)
def security_page():
    """Trust surface: exactly what is stored, what is not, and what a cap does
    and does not guarantee."""
    import site_pages
    return HTMLResponse(site_pages.page(
        "Security &amp; data handling — AgentLedger",
        "What AgentLedger stores, what it never stores, how provider keys are "
        "handled, and how to report a vulnerability.",
        site_pages.SECURITY))


@app.get("/quickstart", response_class=HTMLResponse)
def quickstart_page():
    """Zero to a metered agent in five minutes. Published on PyPI/npm as
    aiagentscity-ledger — the name agent-ledger belongs to a different
    author, hence the umbrella-namespace package name instead."""
    import site_pages
    return HTMLResponse(site_pages.page(
        "Quickstart — AgentLedger",
        "Connect an OpenAI or Anthropic agent to AgentLedger in five minutes: "
        "wrapper, proxy, MCP or CLI.",
        site_pages.QUICKSTART,
        footnav="<a href='/demo'>see the demo</a> · "
                "<a href='/about'>an AI Agent City product</a>"))


@app.get("/compare", response_class=HTMLResponse)
def compare_page():
    """Why this is not a trace viewer — the honest version, with dated prices."""
    import site_pages
    return HTMLResponse(site_pages.page(
        "AgentLedger vs trace viewers — LangSmith, Helicone, Langfuse",
        "How per-agent budget enforcement differs from request-level trace "
        "observability, with dated list prices.",
        site_pages.COMPARE))


@app.get("/reliability", response_class=HTMLResponse)
def reliability_page():
    """Measured proxy overhead, not a claimed number (D-1250)."""
    import metrics
    import site_pages
    pct = metrics.latency_percentiles("pre_call_check")
    return HTMLResponse(site_pages.page(
        "Reliability — AgentLedger",
        "Measured proxy latency (p50/p95/p99) and failure behavior — real "
        "numbers, not a claimed uptime badge.",
        site_pages.reliability_body(pct)))


@app.get("/terms", response_class=HTMLResponse)
def terms_page():
    return _LEGAL_SHELL.format(title="Terms", body=TERMS_BODY)


PAYMENT_LINK = os.environ.get(
    "AL_STRIPE_PAYMENT_LINK", "https://buy.stripe.com/14AbJ0clUeoE9QN3Nl2400e")


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<style>
:root{{--bg:#0d1117;--card:#161b22;--b:#30363d;--g:#D4AF37;--t:#e6edf3;--m:#8b949e}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--t);font-family:-apple-system,'Segoe UI',Roboto,sans-serif;padding:28px 14px;line-height:1.55}}
.w{{max-width:640px;margin:0 auto}}
h1{{font-size:24px}} h1 span{{color:var(--g)}}
.sub{{color:var(--m);font-size:13px;margin:4px 0 20px}}
.card{{background:var(--card);border:1px solid var(--b);border-radius:10px;padding:16px;margin:16px 0;font-size:13px}}
.card p{{margin-bottom:8px}}
.key{{background:#0d1117;border:1px solid var(--g);border-radius:8px;padding:12px;font-family:ui-monospace,Menlo,monospace;font-size:14px;color:var(--g);word-break:break-all;user-select:all;margin:8px 0}}
a.btn,button.btn{{display:inline-block;background:var(--g);color:#0d1117;font-weight:700;border:0;cursor:pointer;text-decoration:none;padding:12px 20px;border-radius:8px;margin-top:12px;font-size:15px;font-family:inherit}}
.warn{{color:#f0b429;font-size:12.5px;margin-top:8px}}
.mut{{color:var(--m);font-size:12px;margin-top:18px}}
pre{{background:#0d1117;border:1px solid var(--b);border-radius:8px;padding:10px;font-size:12px;overflow-x:auto;color:#79c0ff;white-space:pre-wrap;word-break:break-word}}
</style></head><body><div class="w">{body}</div></body></html>"""


def _start_form_html() -> str:
    return _page("AgentLedger — start", """
<h1>Start your <span>ledger</span></h1>
<div class="sub">Get a workspace, claim your first agent, set a cap.</div>
<div class="card">
<p><b>No signup. No login. No card.</b> One press and you get a workspace_key you can
use immediately. Free tier is 3 agents per workspace — every rail, budget caps with real
enforcement, alerts, reports, token burn, and the MCP server are included.</p>
<form method="post" action="/start"><button class="btn" type="submit">Create my workspace</button></form>
<div class="warn">The key is shown once, on the next screen. Save it before you leave — it is not emailed.</div>
</div>
<div class="card">
<p><b>Running this from an agent?</b> Skip the form — pay with your own wallet and get more
than the free tier gives, with no human in the loop:</p>
<pre>curl -X POST https://aiagentscity.com/v1/billing/x402 \
  -H "X-PAYMENT: &lt;your x402 payment header&gt;"</pre>
<p class="mut">{X402_PASS_OFFER_HTML}</p>
<p class="warn">{X402_SETTLEMENT_HTML}</p>
<p class="mut">Agent-facing docs:
<a href="/llms.txt" style="color:#8b949e">/llms.txt</a></p>
</div>
<div class="mut"><a href="/" style="color:#8b949e">← AgentLedger</a></div>
""")


def _fresh_agent_id(workspace_id: str) -> str:
    """A ready-to-paste agent_id that is unique per mint.

    WHY THIS EXISTS (2026-09-18, D-1382)
        The success screen used to hand the customer `"agent_id":"my-agent"` in the copy-paste curl.
        `agent_id` is a GLOBAL namespace, and `my-agent` was claimed once by our own
        `agent-ledger init` probe — so that exact string is now permanently taken. The FIRST
        instruction every new customer follows returned:

            HTTP 401 agent_id 'my-agent' is already claimed — pass its agent_secret

        i.e. a brand-new customer was told someone else already owns their agent. A control call with
        a unique id returned 200, isolating the defect to the string, not the API.

        Fixing it with a documented placeholder (`<your-agent-id>`) would still require the customer
        to invent and type a value. The principal's standing rule is that any flow requiring a human
        to type data fails, so we pre-fill a value that is already valid and unique.

    Shaped to satisfy the API's own `agent_id` validator: lowercase [a-z0-9-], <=64 chars
    (probed live: "a"*80 -> 422 invalid_agent_id; "agent with space" -> 422; "first-agent" -> ok).
    Derived from the workspace id so it is stable-ish, plus a short time suffix for uniqueness.
    """
    tail = re.sub(r"[^a-z0-9]+", "-", workspace_id.lower()).strip("-")[-12:]
    return f"first-agent-{tail}-{int(time.time()) % 100000}"


def _start_key_html(workspace_id: str, raw_key: str, checkout: str) -> str:
    """The post-mint page.

    The layout here is a conversion decision, not decoration (D-1163). This
    page's message is "you are done, and it is free". The previous version made
    the $19 upgrade the ONLY primary (gold) button on the page — a plausible
    way to walk a free-tier visitor into a card form they never wanted, and we
    have a live abandoned checkout that looks exactly like that. So: the free
    state is the visual peak, the next ACTION is claiming an agent, and the
    upgrade is an explicitly optional, visually subordinate link.
    """
    key_block = (f'<div class="key">{html.escape(raw_key)}</div>' if raw_key else
                 '<div class="key">a key was already issued for this workspace and is '
                 'shown only once, at mint time</div>')
    # Fire-and-forget, and deliberately not required for navigation: the link
    # must work with JS disabled and with the beacon failing.
    beacon = ("if(navigator.sendBeacon){navigator.sendBeacon("
              "'/v1/_beacon?event=start_checkout_click&ws="
              + html.escape(workspace_id, quote=True) + "');}")
    return _page("AgentLedger — your workspace", f"""
<h1>You're <span>set up</span></h1>
<div class="sub">workspace_id: {html.escape(workspace_id)}</div>
<div class="card">
<p><b>Your workspace_key — shown once:</b></p>
{key_block}
<div class="warn">Save it now. It is not emailed, and it cannot be displayed again.</div>
<p><b>Nothing to pay.</b> This workspace is on the free tier: up to 3 agents, every rail,
budget caps that block, alerts, reports and the MCP server included. No card, no expiry,
no signup.</p>
</div>
<div class="card">
<p><b>Next: claim your first agent.</b> Send this key as <code>workspace_key</code> on the
first <code>POST /v1/track</code> for a new <code>agent_id</code>. That call returns the
agent's own <code>agent_secret</code>, which authenticates every write after it.</p>
<pre class="cmd">curl -sL --post301 -X POST https://aiagentscity.com/v1/track -H "Content-Type: application/json" -H "AL-API-Version: 2026-09-01" -d '{{"agent_id":"{html.escape(_fresh_agent_id(workspace_id), quote=True)}","rail":"manual","amount_cents":100,"service":"test","workspace_key":"{html.escape(raw_key, quote=True)}"}}'</pre>
<p class="mut">This command is ready to paste as-is: the <code>agent_id</code> is unique to your
workspace and your key is already filled in. (Agent ids are global, so use a name that is yours —
this one is pre-made so the example works on the first try.)</p>
<p class="mut">Full working examples: <a href="/llms.txt" style="color:#8b949e">/llms.txt</a></p>
<p class="mut">Or connect over MCP — <code>claude mcp add --transport http agent-ledger
https://aiagentscity.com/mcp/</code> — and let the agent do it.</p>
</div>
<div class="card">
<p><b>Optional, and not needed today.</b> Only matters past 3 agents — two ways to lift
that, for different situations:</p>
<p class="mut"><b>If an agent hits the wall itself:</b> {_x402_pass_offer_words()}</p>
<p class="mut"><b>If you'd rather not think about it again:</b> Pro at $19/mo, unlimited
agents, no expiry, same workspace, same key, nothing to migrate.</p>
<a class="plain" href="{html.escape(checkout, quote=True)}" rel="noopener"
   onclick="{beacon}">Upgrade to Pro →</a>
</div>
<div class="mut"><a href="/" style="color:#8b949e">← AgentLedger</a></div>
""")


def _start_limited_html() -> str:
    return _page("AgentLedger — slow down", """
<h1>Too many workspaces from this address</h1>
<div class="sub">Three per day.</div>
<div class="card">
<p>The free tier needs no signup, which also means there is nothing stopping an
automated loop — so the mint is capped at 3 workspaces per address per day.</p>
<p>Already have one? Your <code>workspace_key</code> was shown once when you created it.
If it is lost, it cannot be recovered in this version.</p>
<p>Running an agent rather than a browser? The path is
<code>POST /v1/billing/x402</code>, which is not affected by this limit — and it buys more
than the free tier anyway: {X402_PASS_OFFER_HTML} It
{X402_SETTLEMENT_HTML}</p>
<p><a class="plain" href="/llms.txt">/llms.txt</a> has the full API docs.</p>
</div>
<div class="mut"><a href="/" style="color:#8b949e">← AgentLedger</a></div>
""")

@app.get("/start", response_class=HTMLResponse)
def start_page():
    """Step one of the buy path. Deliberately does NOT mint: a mint on GET
    would let any crawler, link-preview bot or accidental reload burn one of
    the 50 launch-window workspaces and orphan a key nobody ever saw. The
    form POSTs to this same path, which does the minting."""
    return (_start_form_html()
            .replace("{X402_SETTLEMENT_HTML}", _x402_settlement_words())
            .replace("{X402_PASS_OFFER_HTML}", _x402_pass_offer_words()))


_START_WINDOW_SECONDS = 86400
_START_MAX_PER_IP = 3
_START_MINTS: dict = {}


def _start_mint_allowed(request: Request) -> bool:
    """Three workspace mints per IP per day.

    POST /start is an unauthenticated write on a public route, and each mint
    costs disk. Without this, one loop creates unlimited workspaces — and
    against a deployment where the mint still consumed a launch-window slot,
    it took the whole promotion in a second. In-memory on purpose: this is a
    single-instance service and the counter is not worth a datastore.
    """
    client = request.client.host if request.client else "unknown"
    now = time.time()
    recent = [t for t in _START_MINTS.get(client, []) if now - t < _START_WINDOW_SECONDS]
    if len(recent) >= _START_MAX_PER_IP:
        _START_MINTS[client] = recent
        return False
    recent.append(now)
    _START_MINTS[client] = recent
    return True


@app.post("/start", response_class=HTMLResponse)
def start_mint(request: Request):
    """Mint a workspace for a human with no signup, no login and no card, then
    show its workspace_key exactly once — the same one-time reveal the
    retiring Google dashboard used. The payment link carries the workspace id
    as client_reference_id: that reference is what lets the Stripe webhook
    mark THIS workspace Pro. Without it a real payment would take the card
    and upgrade nothing (D-1162).

    grant_scarcity=False: this is the free tier, not the launch grant. See
    workspace_engine.create_workspace for why.
    """
    if not _start_mint_allowed(request):
        # Bug fixed alongside D-1270: this page carried {X402_SETTLEMENT_HTML}
        # literally, unsubstituted — no .replace() was ever called on it, so a
        # rate-limited visitor saw the placeholder text instead of the real
        # settlement/offer sentence.
        limited = (_start_limited_html()
                  .replace("{X402_SETTLEMENT_HTML}", _x402_settlement_words())
                  .replace("{X402_PASS_OFFER_HTML}", _x402_pass_offer_words()))
        return HTMLResponse(limited, status_code=429)
    import workspace_engine
    workspace_id, raw_key = workspace_engine.create_workspace(grant_scarcity=False)
    # Onboarding step 1/2 (D-1163). Workspace id only — never the key, never an IP.
    try:
        metrics.record_onboarding("workspace_minted", workspace_id,
                                  plan="free", grant_scarcity=False)
        if raw_key:
            metrics.record_onboarding("key_revealed", workspace_id)
    except Exception:
        pass
    checkout = f"{PAYMENT_LINK}?client_reference_id={workspace_id}"
    # no-store: the key is shown exactly once and can never be re-revealed, so
    # a cache (or a browser's back-forward cache) holding this response would
    # strand a credential we cannot reissue. no-referrer keeps the key out of
    # any Referer header on the outbound click to Stripe.
    return HTMLResponse(
        _start_key_html(workspace_id, raw_key, checkout).replace(
            "{X402_SETTLEMENT_HTML}", _x402_settlement_words()),
                        headers={"Cache-Control": "no-store",
                                 "Referrer-Policy": "no-referrer"})

_WS_ID_RE = re.compile(r"^ws_[A-Za-z0-9_\-]{10,64}$")


@app.get("/v1/_beacon")
def connect_beacon(event: str, ws: str = ""):
    """Fire-and-forget telemetry beacon for static-page interactions that have
    no natural server round-trip (launch-kit v0.3 item 4). The allowlist is
    closed on purpose — an open `event` value would let a caller write
    arbitrary metric kinds, and an open `ws` would let them invent workspaces
    in the onboarding stream. Both are validated here."""
    if event == "connect_page_view":
        try:
            metrics.record_event("connect_page_view")
        except Exception:
            pass
    elif event == "start_checkout_click" and _WS_ID_RE.match(ws or ""):
        # The upgrade button on the post-mint page is a plain link to Stripe:
        # no server round-trip, so without this the click leaves no trace and
        # the funnel has a hole exactly where conversion is decided.
        try:
            metrics.record_onboarding("checkout_clicked", ws)
        except Exception:
            pass
    return JSONResponse(content={"ok": True})

from routes_billing import router as billing_router
app.include_router(billing_router)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8761))
    uvicorn.run(app, host="0.0.0.0", port=port)

# --- hosted MCP (streamable-http) mount for agent-marketplace remotes ---
try:
    from contextlib import asynccontextmanager
    from al_mcp_http import mcp as al_mcp, get_asgi_app  # noqa

    _al_asgi = get_asgi_app()
    _mcp_lifespan = _al_asgi.lifespan  # fastmcp 3.x: ASGI app carries its own session-manager lifespan
    _base_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _combined_lifespan(app):
        async with _base_lifespan(app):
            async with _mcp_lifespan(app):
                yield

    app.router.lifespan_context = _combined_lifespan

    class _McpSlashRewrite:
        """Make `/mcp` answer like `/mcp/`, and tolerate a missing Content-Type.

        Two front-door defects measured live in /data/metrics.jsonl:

        1. Starlette's redirect_slashes answers `POST /mcp` with a 307 to `/mcp/`.
           A client that does not re-send the body on 307 then lands on the mount
           with an empty body and gets `400 Bad Request` — forever, because the
           next retry follows the same path. Measured 2026-09-14: 104 distinct
           agents hit that 307, two of them (`88b16a824d91`, `cda40c3d5993`) with
           336 and 328 attempts and ZERO successes, retrying every ~5 minutes.

        2. The MCP SDK's DNS-rebinding guard (`transport_security.validate_request`)
           rejects a POST with NO Content-Type header outright:
           `Response("Invalid Content-Type header", status_code=400)`. This runs
           BEFORE the JSON-RPC handler, so an otherwise-valid client that simply
           omits the header is turned away at the door and never gets a hint why.
           Measured 2026-09-16: 79 distinct agents had 1,162 such 400s and zero
           successes while hammering `/mcp` and `/mcp/` exclusively —
           `cda40c3d5993` alone had 630 attempts across 3 days. They are not
           scanners; they are asking to talk MCP and we are refusing on a header
           they did not know to send.

           A JSON-RPC POST body is JSON by definition. Two client habits get refused
           before the handler runs, both with the same unhelpful 400: sending no
           Content-Type at all, and sending the HTTP client's DEFAULT form
           encoding (`application/x-www-form-urlencoded` — what `curl -d`, urllib
           and requests all use unless told otherwise). The second is what the live
           agents actually sent; assuming "no header" alone would have missed every
           one of them. Neither habit means the body is malformed, so we relabel to
           JSON and let the JSON parser be the judge — a genuinely non-JSON body
           still fails, with a parse error that names the real problem. A
           deliberate non-JSON type (text/plain, text/xml) is left alone.

        This is a pure ASGI middleware, not BaseHTTPMiddleware: it only mutates
        `scope` and delegates. That matters because MCP streamable-http answers
        with `text/event-stream`, and BaseHTTPMiddleware buffers streaming
        responses.

        Path rewrite, not a redirect: the request body and method are preserved
        and the client sees a single 200 instead of a 307 it may mishandle.
        """

        def __init__(self, asgi_app):
            self.asgi_app = asgi_app

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http":
                path = scope.get("path", "")

                if path == "/mcp":
                    scope = dict(scope)
                    scope["path"] = "/mcp/"
                    scope["raw_path"] = b"/mcp/"
                if scope.get("method") == "POST" and str(path).startswith("/mcp"):
                    headers = list(scope.get("headers") or [])
                    _ct = ""
                    for k, v in headers:
                        if k.lower() == b"content-type":
                            _ct = v.decode("latin-1").split(";")[0].strip().lower()
                            break
                    # A JSON-RPC POST body is JSON. Two client habits get refused
                    # before the handler runs, both with an unhelpful
                    # "Invalid Content-Type header" 400 from the SDK's
                    # DNS-rebinding guard:
                    #   - no Content-Type at all
                    #   - the HTTP client's default form encoding
                    #     (`curl -d`, urllib and requests all send
                    #     application/x-www-form-urlencoded unless told otherwise)
                    # Neither means the body is malformed, so relabel it JSON and
                    # let the JSON parser be the judge. A body that is genuinely
                    # not JSON still fails — with a JSON-RPC parse error that
                    # names the real problem instead of blaming a header.
                    # A deliberate non-JSON type (text/plain, text/xml, ...) is
                    # left alone: that is an explicit client choice, and we should
                    # not silently reinterpret it.
                    if _ct in ("", "application/x-www-form-urlencoded"):
                        headers = [(k, v) for k, v in headers
                                   if k.lower() != b"content-type"]
                        headers.append((b"content-type", b"application/json"))
                        scope = dict(scope)
                        scope["headers"] = headers
            await self.asgi_app(scope, receive, send)

    # Added before the mount so it wraps the whole router, including /mcp/.
    app.add_middleware(_McpSlashRewrite)
    app.mount("/mcp", _al_asgi)
except Exception as _e:  # MCP optional — API keeps working without it
    import logging
    logging.warning(f"MCP mount skipped: {_e}")
