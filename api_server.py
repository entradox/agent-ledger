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
import html, json, os, re, sys, time, hmac, hashlib
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
from fastapi.responses import PlainTextResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
import uvicorn

APP_VERSION = "0.4.1"  # single source for /health + FastAPI metadata
app = FastAPI(title="AgentLedger API", version=APP_VERSION)

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
REACH_PATHS = frozenset({"/", "/start", "/status", "/llms.txt", "/server.json",
                          "/.well-known/glama.json", "/.well-known/mcp/server-card.json",
                          "/stats", "/mcp/"})


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

Machine-readable schema: GET /openapi.json (OpenAPI 3) · MCP manifest: GET /server.json
Human/agent status page: GET /status (live health, version, uptime, counters)
Privacy: GET /privacy · Terms: GET /terms

Data handling: cost metadata only — agent id, rail, service label, amount, token
counts, model, timestamp. There is no field for prompt or response content; none
is collected and none is stored. Prompts and responses are never used to train
models, and your data is never sold.

## Ownership (a workspace_key claims; an agent_secret writes)

Claiming a NEW agent_id requires a workspace_key in the body of the first
write (POST /v1/track or /v1/budget). Get one self-serve with no human at
all by paying via POST /v1/billing/x402 (the paying wallet becomes the
workspace identity), or by opening GET /start — no signup, no login, no
card. TESTNET ONLY: /v1/billing/x402 settles on Base Sepolia (eip155:84532)
with testnet USDC, so a mainnet wallet cannot complete it until mainnet
onboarding lands; use GET /start if you have no testnet wallet. Missing or
invalid key on a new claim gets 401
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
agent_id gets 402 until upgrading ($19/mo, unlimited agents). The cap is
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
                                      CURRENTLY TESTNET ONLY: settles on Base Sepolia
                                      (eip155:84532) with testnet USDC; a mainnet wallet
                                      cannot complete it until Coinbase CDP onboarding lands.
                                      Use GET /start instead if you have no testnet wallet.
GET  /start                        — get a workspace (no signup, no login);
                                      POST /start mints one and shows the key once

## Getting started (the shortest path)

    pip install "aiagentscity-ledger[wrapper]"   # or: uvx --from git+https://github.com/entradox/agent-ledger agent-ledger init
    agent-ledger init --agent my-agent    # mints a workspace, claims an agent, writes .env

then, in your code:

    from openai import OpenAI
    import agentledger
    client = agentledger.wrap(OpenAI(api_key=OPENAI_KEY),
                              agent_id="my-agent", agent_secret="<from .env>")

Every call now goes through the proxy: metered, and refused before the provider
is contacted if it would cross a cap. Streaming, tool calls and retries are
unchanged — wrap() only repoints the base URL and adds two headers; it does not
patch or subclass the SDK. Anthropic's client works the same way.

`agent-ledger share --agent-id my-agent --agent-secret <secret>` prints a
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
    return LLMS_TXT

ROBOTS_TXT = """User-agent: *
Allow: /
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
                        "identity), or at GET /start — no signup, no login, no card. "
                        "NOTE: /v1/billing/x402 is TESTNET ONLY right now (Base Sepolia "
                        "eip155:84532, testnet USDC); a mainnet wallet cannot complete it "
                        "until mainnet onboarding lands, so prefer GET /start unless you "
                        "hold a testnet wallet. "
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
        "description": "Free tier is 3 agents per workspace. Pro is $19/mo for "
                        "unlimited agents.",
    },
    "capabilities": [
        {"id": "mint_workspace_x402",
         "description": "Self-serve workspace + workspace_key for an agent with a "
                        "wallet — no human, no login. Present an x402 payment in the "
                        "X-PAYMENT header; the paying wallet becomes the workspace "
                        "identity. Do this first: a workspace_key is required to "
                        "claim a new agent_id. TESTNET ONLY: settles on Base Sepolia "
                        "(eip155:84532) with testnet USDC, so a mainnet wallet cannot "
                        "complete it until mainnet onboarding lands. If you hold no "
                        "testnet wallet, mint at GET /start instead (no wallet, no card).",
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
    """AEO capability manifest — agents discover what this product does,
    pricing, auth, and how to call it (rules/agent-native-standard.md)."""
    return JSONResponse(content=AGENT_JSON)

def _status_html() -> str:
    return (Path(__file__).parent / "status.html").read_text()


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
    """Zero to a metered agent in five minutes. The install line is the git URL
    because nothing is published on PyPI yet, and the name agent-ledger there
    belongs to a different author."""
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
<p><b>Running this from an agent?</b> An agent with a wallet can mint its own workspace with
no human in the loop at all:</p>
<pre>curl -X POST https://aiagentscity.com/v1/billing/x402 \
  -H "X-PAYMENT: &lt;your x402 payment header&gt;"</pre>
<p class="mut">The paying wallet becomes the workspace identity.</p>
<p class="warn"><b>Currently testnet only.</b> This path settles on Base Sepolia
(<code>eip155:84532</code>) with testnet USDC. A mainnet wallet cannot complete it yet.
Mainnet arrives when Coinbase CDP onboarding completes; until then, mint at
<a href="/start" style="color:#8b949e">/start</a> — it needs no wallet and no card.</p>
<p class="mut">Agent-facing docs:
<a href="/llms.txt" style="color:#8b949e">/llms.txt</a></p>
</div>
<div class="mut"><a href="/" style="color:#8b949e">← AgentLedger</a></div>
""")


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
<pre class="cmd">curl -sL --post301 -X POST https://aiagentscity.com/v1/track -H "Content-Type: application/json" -H "AL-API-Version: 2026-09-01" -d '{{"agent_id":"my-agent","rail":"manual","amount_cents":100,"service":"test","workspace_key":"YOUR_KEY"}}'</pre>
<p class="mut">Full working examples: <a href="/llms.txt" style="color:#8b949e">/llms.txt</a></p>
<p class="mut">Or connect over MCP — <code>claude mcp add --transport http agent-ledger
https://aiagentscity.com/mcp/</code> — and let the agent do it.</p>
</div>
<div class="card">
<p><b>Optional, and not needed today: Pro — $19/mo</b> for unlimited tracked agents.</p>
<p class="mut">Only matters past 3 agents. Same workspace, same key, nothing to migrate.</p>
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
<code>POST /v1/billing/x402</code>, which is not affected by this limit — but it is
<b>testnet only</b> right now (Base Sepolia, <code>eip155:84532</code>, testnet USDC),
so a mainnet wallet cannot complete it until mainnet onboarding lands.</p>
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
    return _start_form_html()


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
        return HTMLResponse(_start_limited_html(), status_code=429)
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
    return HTMLResponse(_start_key_html(workspace_id, raw_key, checkout),
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
    app.mount("/mcp", _al_asgi)
except Exception as _e:  # MCP optional — API keeps working without it
    import logging
    logging.warning(f"MCP mount skipped: {_e}")
