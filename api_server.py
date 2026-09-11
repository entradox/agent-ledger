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
import html, json, os, sys, time, hmac, hashlib
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
from fastapi.responses import PlainTextResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
import uvicorn

APP_VERSION = "0.3.0"  # single source for /health + FastAPI metadata
app = FastAPI(title="AgentLedger API", version=APP_VERSION)

from routes_agents import router as agents_router
app.include_router(agents_router)

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
                          "/.well-known/glama.json", "/stats", "/mcp/"})


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
        if path.startswith("/v1/billing/"):
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

    funnel = {k: {"total": totals.get(k, 0), "last_24h": last_24h.get(k, 0)}
              for k in FUNNEL_KINDS}

    checkout_funnel = {
        "checkout_started": {"total": totals.get("checkout_started", 0),
                              "last_24h": last_24h.get("checkout_started", 0)},
        "checkout_completed": {"total": totals.get("checkout_completed", 0),
                                "last_24h": last_24h.get("checkout_completed", 0)},
        "revenue_events_completed": totals.get("checkout_completed", 0),
        "sum_amount_cents_completed": amount_sums.get("checkout_completed", 0),
    }

    reach = {path: unique_ips.get(f"path:{path}", 0) for path in sorted(REACH_PATHS)}

    agents = list_agents()
    with_data = sum(1 for a in agents if a.get("has_data"))
    squatted = sum(1 for a in agents if not a.get("has_data"))

    return {
        "service": "agent-ledger",
        "version": app.version,
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
        payload = {"tracked_agents": claimed_agent_count(), "events": dict(c),
                   "scarcity_claims_left": scarcity_claims_left()}
        _stats_cache["payload"] = payload
        _stats_cache["ts"] = _time.time()
        return payload

LLMS_TXT = """# AgentLedger

Per-agent spend management — the Datadog for agent spending. Track spend
across x402/MPP/API-key rails, budget caps, anomaly alerts, audit trails.

Machine-readable schema: GET /openapi.json (OpenAPI 3) · MCP manifest: GET /server.json
Human/agent status page: GET /status

## Ownership (a workspace_key claims; an agent_secret writes)

Claiming a NEW agent_id requires a workspace_key in the body of the first
write (POST /v1/track or /v1/budget). Get one self-serve with no human at
all by paying via POST /v1/billing/x402 (the paying wallet becomes the
workspace identity), or by opening GET /start — no signup, no login, no
card. Missing or invalid key on a new claim gets 401
workspace_key_required.
That first write mints an `agent_secret` and returns it once, e.g.
{"agent_secret": "...", "_note": "..."}. Save it — every later write to that
same agent_id must include it in the body as "agent_secret" (the
workspace_key is never needed again for that agent), or the request is
rejected with 401. Reads /v1/report, /v1/tokens, and /v1/alerts all require
an X-Agent-Secret or X-Workspace-Key header (either credential proving
access to that agent_id) — missing/wrong gets 401. There is no
unauthenticated read path, on REST or MCP.
Launch window: the first 50 WORKSPACES ever created get Pro free for 1 year
(no action needed — signing up inside the window mints the grant
automatically; it expires one year later).
Outside that window a free workspace is capped at 3 agents; a 4th new
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
     body: {"agent_id": str, "rail": str, "amount_cents": int (0-10000000), "service": str,
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
GET  /v1/tokens/{agent_id}         — token burn report: in/out totals + by model (query: days=30) — requires X-Agent-Secret or X-Workspace-Key
GET  /v1/alerts/{agent_id}         — alerts for agent — requires X-Agent-Secret or X-Workspace-Key
GET  /v1/agents                    — owner-only: full cross-tenant listing (requires X-Al-Admin header)
GET  /stats                        — usage counters
POST /v1/billing/x402              — self-serve workspace minting for an agent with a wallet
                                      (X-PAYMENT header; the paying wallet IS the identity)
GET  /start                        — get a workspace (no signup, no login);
                                      POST /start mints one and shows the key once

## MCP

Registry: io.github.entradox/agent-ledger
Remote:   https://agent-ledger-production-0ff8.up.railway.app/mcp/

Tools exposed at POST /mcp/:
  ledger_track          — record a spend entry (workspace_key to claim, agent_secret after)
  ledger_set_budget     — set a budget cap (workspace_key to claim, agent_secret after)
  ledger_report         — get a spend report (agent_secret or workspace_key param)
  ledger_alerts         — get alerts for an agent (agent_secret or workspace_key param)
  ledger_list_agents    — owner-only (admin_secret param)
  ledger_api_docs       — self-serve docs by topic: quickstart|mcp|rest|budget|errors|idempotency|all (open read)
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
$19/mo, unlimited agents; first 50 workspaces get Pro free for 1 year).
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

AGENT_JSON = {
    "schema_version": "1.0",
    "name": "AgentLedger",
    "description": "Per-agent spend management: track spend across x402/MPP/API-key "
                    "rails, set budget caps, get anomaly alerts, keep an audit trail.",
    "url": "https://agent-ledger-production-0ff8.up.railway.app",
    "api_base": "https://agent-ledger-production-0ff8.up.railway.app/v1",
    "openapi": "https://agent-ledger-production-0ff8.up.railway.app/openapi.json",
    "auth": {
        "type": "workspace_key",
        "field": "workspace_key",
        "description": "Get a workspace_key with no human at all by paying via "
                        "POST /v1/billing/x402 (the paying wallet becomes the workspace "
                        "identity), or at GET /start — no signup, no login, no card. "
                        "Claiming a NEW agent_id "
                        "requires that workspace_key in the first POST /v1/track or "
                        "/v1/budget body; that call mints an agent_secret in the "
                        "response — save it, every later write to that agent_id must "
                        "include it and needs no workspace_key. Reads "
                        "(report/alerts/tokens) require agent_secret or workspace_key, "
                        "sent as X-Agent-Secret or X-Workspace-Key.",
    },
    "pricing": {
        "model": "freemium",
        "amount_usd": 19.00,
        "description": "Free tier is 3 agents per workspace. Pro is $19/mo for "
                        "unlimited agents. Launch window: the first 50 workspaces "
                        "created get Pro free for 1 year.",
    },
    "capabilities": [
        {"id": "mint_workspace_x402",
         "description": "Self-serve workspace + workspace_key for an agent with a "
                        "wallet — no human, no login. Present an x402 payment in the "
                        "X-PAYMENT header; the paying wallet becomes the workspace "
                        "identity. Do this first: a workspace_key is required to "
                        "claim a new agent_id.",
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
    ],
    "contact": "entradox@icloud.com",
    "legal": "Parmanand LLC. Beta software, provided as-is.",
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
    """The front door. Until D-1162 this path 404'd — the only human page was
    /status, so a live product with a working payment link could not be
    reached from its own root."""
    return _status_html()


@app.get("/status", response_class=HTMLResponse)
def status_page():
    return _status_html()


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
<pre>curl -X POST https://agent-ledger-production-0ff8.up.railway.app/v1/billing/x402 \
  -H "X-PAYMENT: &lt;your x402 payment header&gt;"</pre>
<p class="mut">The paying wallet becomes the workspace identity. Agent-facing docs:
<a href="/llms.txt" style="color:#8b949e">/llms.txt</a></p>
</div>
<div class="mut"><a href="/" style="color:#8b949e">← AgentLedger</a></div>
""")


def _start_key_html(workspace_id: str, raw_key: str, checkout: str) -> str:
    key_block = (f'<div class="key">{html.escape(raw_key)}</div>' if raw_key else
                 '<div class="key">a key was already issued for this workspace and is '
                 'shown only once, at mint time</div>')
    return _page("AgentLedger — your workspace", f"""
<h1>Your workspace is <span>live</span></h1>
<div class="sub">workspace_id: {html.escape(workspace_id)}</div>
<div class="card">
<p><b>Your workspace_key — shown once:</b></p>
{key_block}
<div class="warn">Save it now. It is not emailed, and it cannot be displayed again.</div>
<p class="mut">Claim your first agent by sending it as <code>workspace_key</code> on the first
<code>POST /v1/track</code>. That call returns the agent's own <code>agent_secret</code>,
which authenticates every write after it. Working examples:
<a href="/llms.txt" style="color:#8b949e">/llms.txt</a></p>
</div>
<div class="card">
<p><b>Upgrade this workspace to Pro — $19/mo</b> (unlimited tracked agents).</p>
<a class="btn" href="{html.escape(checkout, quote=True)}" rel="noopener">Continue to payment →</a>
<p class="mut">The link carries this workspace's id, so the upgrade lands on <i>this</i>
workspace. The free tier needs no card and does not expire.</p>
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


@app.post("/start", response_class=HTMLResponse)
def start_mint():
    """Mint a workspace for a human with no signup, no login and no card, then
    show its workspace_key exactly once — the same one-time reveal the
    retiring Google dashboard used. The payment link carries the workspace id
    as client_reference_id: that reference is what lets the Stripe webhook
    mark THIS workspace Pro. Without it a real payment would take the card
    and upgrade nothing (D-1162)."""
    import workspace_engine
    workspace_id, raw_key = workspace_engine.create_workspace()
    checkout = f"{PAYMENT_LINK}?client_reference_id={workspace_id}"
    return _start_key_html(workspace_id, raw_key, checkout)

@app.get("/v1/_beacon")
def connect_beacon(event: str):
    """Fire-and-forget telemetry beacon for static-page interactions that have
    no natural server round-trip (launch-kit v0.3 item 4). Only the
    status.html Connect section uses this today, hence the closed allowlist —
    an open `event` value would let a caller write arbitrary metric kinds."""
    if event == "connect_page_view":
        try:
            metrics.record_event("connect_page_view")
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
