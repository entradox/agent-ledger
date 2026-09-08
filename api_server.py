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
"""
import json, os, sys, time, hmac, hashlib
from pathlib import Path
from typing import Optional
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ledger_engine import (
    track, set_budget, get_budget, report, list_agents, _ledger_path,
    ensure_agent_secret, claimed_agent_count, MAX_AMOUNT_CENTS,
    AuthError, ValidationError, BudgetExceededError, BetaCapExceededError,
    validate_agent_id as le_validate_agent_id,
)

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
import uvicorn

app = FastAPI(title="AgentLedger API", version="0.2.1-hardening")

DATA_DIR = Path(os.environ.get("AGENT_LEDGER_DATA", os.path.expanduser("~/.agent-ledger")))
COUNTS_FILE = DATA_DIR / "counts.jsonl"

def _log_event(kind):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(COUNTS_FILE, "a") as f:
            f.write(json.dumps({"ts": time.time(), "kind": kind}) + "\n")
    except Exception:
        pass

class TrackRequest(BaseModel):
    agent_id: str
    rail: str
    amount_cents: int = Field(ge=0, le=MAX_AMOUNT_CENTS)
    service: str
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    model: str = ""
    agent_secret: Optional[str] = None

class BudgetRequest(BaseModel):
    agent_id: str
    monthly_cents: int = Field(ge=0)
    daily_cents: int = Field(default=0, ge=0)
    agent_secret: Optional[str] = None

@app.get("/health")
def health():
    return {"ok": True, "service": "agent-ledger", "version": "0.2.1-hardening"}

import threading as _threading
import time as _time

def _claim_or_401(agent_id: str, provided_secret: Optional[str]):
    """Shared auth gate for every write endpoint. Mints a secret on first use
    of a new agent_id (no signup), verifies it on every later write, and
    enforces the beta agent-slot cap. Raises HTTPException on failure."""
    try:
        return ensure_agent_secret(agent_id, provided_secret)
    except AuthError as e:
        raise HTTPException(401, str(e))
    except BetaCapExceededError as e:
        # Pro exemption is handled inside ensure_agent_secret(); anything that
        # still reaches here is a genuinely free-tier cap hit.
        raise HTTPException(402, str(e))
    except ValidationError as e:
        # malformed agent_id (traversal, bad charset) — 422, not a 500
        raise HTTPException(422, str(e))

@app.post("/v1/track")
def create_track(req: TrackRequest):
    _log_event("track")
    # Validate BEFORE the claim/mint step: a garbage rail or amount must not
    # burn a free-tier agent slot (the minted secret is only returned on a
    # successful write, so failing after minting would squat the agent_id).
    try:
        le_validate_agent_id(req.agent_id)
    except ValidationError as e:
        raise HTTPException(422, str(e))
    if req.rail != "tokens":
        from ledger_engine import VALID_RAILS
        if req.rail not in VALID_RAILS:
            raise HTTPException(422, f"rail must be one of {sorted(VALID_RAILS)} (got '{req.rail}')")
    secret, created = _claim_or_401(req.agent_id, req.agent_secret)
    try:
        entry = track(req.agent_id, req.rail, req.amount_cents, req.service)
    except ValidationError as e:
        raise HTTPException(422, str(e))
    except BudgetExceededError as e:
        raise HTTPException(402, str(e))
    # token dimension: token counts stored SEPARATELY from the dollar ledger
    # (never mixed — token counts are not cents) via meta on the entry.
    # rail="tokens" rows are 0-cent bookkeeping, exempt from budget/amount checks,
    # and ride on the ownership already verified above for the main entry.
    if req.tokens_in or req.tokens_out:
        tok_meta = {"tokens_in": req.tokens_in, "tokens_out": req.tokens_out}
        if req.model:
            tok_meta["model"] = req.model
        track(req.agent_id, "tokens", 0, req.model or req.service, **tok_meta)
    result = entry.to_dict()
    if created:
        result["agent_secret"] = secret
        result["_note"] = ("Save this agent_secret — required for every future write "
                            "to this agent_id (track/budget). It will not be shown again.")
    return result

@app.post("/v1/budget")
def create_budget(req: BudgetRequest):
    try:
        le_validate_agent_id(req.agent_id)
    except ValidationError as e:
        raise HTTPException(422, str(e))
    secret, created = _claim_or_401(req.agent_id, req.agent_secret)
    try:
        b = set_budget(req.agent_id, req.monthly_cents, req.daily_cents)
    except ValidationError as e:
        raise HTTPException(422, str(e))
    result = b.to_dict()
    if created:
        result["agent_secret"] = secret
        result["_note"] = ("Save this agent_secret — required for every future write "
                            "to this agent_id (track/budget). It will not be shown again.")
    return result

@app.get("/v1/report/{agent_id}")
def get_report(agent_id: str, days: int = 30):
    r = report(agent_id, days)
    return {"agent_id": r.agent_id, "period": r.period,
            "total_spend_cents": r.total_spend_cents, "by_rail": r.by_rail,
            "by_service": r.by_service, "budget_status": r.budget_status,
            "anomalies": r.anomalies, "entry_count": r.entry_count}

@app.get("/v1/alerts/{agent_id}")
def get_alerts(agent_id: str):
    from ledger_engine import validate_agent_id
    try:
        validate_agent_id(agent_id)
    except ValidationError as e:
        raise HTTPException(422, str(e))
    alerts_path = DATA_DIR / "agents" / agent_id / "alerts.jsonl"
    if not alerts_path.exists():
        return {"count": 0, "alerts": []}
    alerts = [json.loads(l) for l in open(alerts_path)]
    return {"count": len(alerts), "alerts": alerts}

@app.get("/v1/agents")
def get_agents(request: Request):
    """Portfolio-wide listing across every agent_id ever claimed — owner-only.
    (Per-agent data stays open-read at GET /v1/report/{agent_id} and
    /v1/tokens/{agent_id}; this endpoint is the full cross-tenant dump.)"""
    admin_secret = os.environ.get("AL_ADMIN_SECRET", "")
    if not admin_secret or request.headers.get("x-al-admin") != admin_secret:
        raise HTTPException(401, "owner only")
    return {"agents": list_agents()}

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
        payload = {"tracked_agents": claimed_agent_count(), "events": dict(c)}
        _stats_cache["payload"] = payload
        _stats_cache["ts"] = _time.time()
        return payload

LLMS_TXT = """# AgentLedger

Per-agent spend management — the Datadog for agent spending. Track spend
across x402/MPP/API-key rails, budget caps, anomaly alerts, audit trails.

Machine-readable schema: GET /openapi.json (OpenAPI 3) · MCP manifest: GET /server.json
Human/agent status page: GET /status

## Ownership (no signup — but not open-write either)

The first write (POST /v1/track or /v1/budget) to a new agent_id mints an
`agent_secret` and returns it once, e.g. {"agent_secret": "...", "_note": "..."}.
Save it — every later write to that same agent_id must include it in the body
as "agent_secret", or the request is rejected with 401. Reads
(/v1/report, /v1/tokens, /v1/alerts) stay open — no secret required.
Beta caps total claimed agents at 3 site-wide; a 4th new agent_id gets 402
until upgrading. Amounts per entry are capped at $100,000 and must be >= 0.
Setting a budget makes it enforced going forward: a track() entry that would
cross the monthly/daily cap is rejected with 402, not just logged.

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
            "agent_secret": str (required after the first call for this agent_id)}
POST /v1/budget                    — set budget caps (mints/verifies agent_secret); once set,
                                      track() blocks entries that would cross the cap
     body: {"agent_id": str, "monthly_cents": int (0-10000000), "daily_cents": int (optional, 0-10000000),
            "agent_secret": str (required after the first call for this agent_id)}
GET  /v1/report/{agent_id}         — spend report (query: days=30) — open read
GET  /v1/tokens/{agent_id}         — token burn report: in/out totals + by model (query: days=30) — open read
GET  /v1/alerts/{agent_id}         — alerts for agent — open read
GET  /v1/agents                    — owner-only: full cross-tenant listing (requires X-Al-Admin header)
GET  /stats                        — usage counters

## MCP

Registry: io.github.entradox/agent-ledger
Remote:   https://agent-ledger-production-0ff8.up.railway.app/mcp/

Tools exposed at POST /mcp/:
  ledger_track          — record a spend entry (agent_secret param, same rules as above)
  ledger_set_budget     — set a budget cap (agent_secret param, same rules as above)
  ledger_report         — get a spend report (open read)
  ledger_alerts         — get alerts for an agent (open read)
  ledger_list_agents    — owner-only (admin_secret param)

Free during beta. Contact: entradox@icloud.com
"""

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

@app.get("/status", response_class=HTMLResponse)
def status_page():
    return (Path(__file__).parent / "status.html").read_text()

# ── Stripe billing (LIVE, GASPERMIT acct) — mirrors Agent Watch's pattern ────
CUSTOMERS_FILE = DATA_DIR / "customers.jsonl"

def _append_customer(rec: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CUSTOMERS_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")

@app.post("/stripe/webhook")
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
    if event.get("type") != "checkout.session.completed":
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
    if plan == "pro":
        from ledger_engine import activate_pro
        activate_pro()
    try:
        from send_onboarding_email import send_onboarding_email
        send_onboarding_email(email, plan)
    except Exception as e:
        import logging
        logging.warning(f"onboarding email skipped: {e}")
    return {"registered": True, "email": email, "plan": plan}

@app.delete("/v1/agents/{agent_id}")
def delete_agent(agent_id: str, request: Request):
    """Remove an agent's ledger entirely. Owner-only (cron secret) — beta slots
    are per-product, so the operator can clear test/demo agents to free slots."""
    from ledger_engine import validate_agent_id
    admin_secret = os.environ.get("AL_ADMIN_SECRET", "")
    if not admin_secret or request.headers.get("x-al-admin") != admin_secret:
        raise HTTPException(401, "owner only")
    try:
        validate_agent_id(agent_id)
    except ValidationError as e:
        raise HTTPException(422, str(e))
    import shutil
    agent_dir = DATA_DIR / "agents" / agent_id
    if not agent_dir.exists():
        raise HTTPException(404, f"agent not found: {agent_id}")
    shutil.rmtree(agent_dir)
    return {"deleted": agent_id}

@app.get("/v1/tokens/{agent_id}")
def token_report(agent_id: str, days: int = 30):
    """Token burn report: totals in/out, by model, per period. Separate from
    dollar spend — answers 'what is this agent burning on?'"""
    from ledger_engine import validate_agent_id
    try:
        validate_agent_id(agent_id)
    except ValidationError as e:
        raise HTTPException(422, str(e))
    from ledger_engine import _ledger_path
    p = _ledger_path(agent_id)
    if not p.exists():
        return {"agent_id": agent_id, "days": days, "tokens_in": 0, "tokens_out": 0,
                "total_tokens": 0, "by_model": {}, "entries": 0}
    import time as _t
    cutoff = _t.time() - days * 86400
    tin = tout = 0
    by_model = {}
    entries = 0
    for line in p.read_text().splitlines():
        try:
            e = json.loads(line)
        except Exception:
            continue
        # token counts live at TOP level of the entry (track() promotes **meta to keys)
        if e.get("rail") != "tokens" or "tokens_in" not in e:
            continue
        ts = e.get("timestamp", "")
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            if dt < cutoff:
                continue
        except Exception:
            pass
        t_in = e.get("tokens_in", 0)
        t_out = e.get("tokens_out", 0)
        tin += t_in
        tout += t_out
        entries += 1
        m = e.get("model") or "unknown"
        by_model[m] = by_model.get(m, 0) + t_in + t_out
    return {"agent_id": agent_id, "days": days, "tokens_in": tin, "tokens_out": tout,
            "total_tokens": tin + tout, "by_model": by_model, "entries": entries}

@app.get("/v1/billing/{email}")
def billing_status(email: str):
    for line in (CUSTOMERS_FILE.read_text().splitlines() if CUSTOMERS_FILE.exists() else []):
        try:
            r = json.loads(line)
            if r.get("email", "").lower() == email.lower() and r.get("status") == "active":
                return {"email": email, "plan": r.get("plan"), "status": "active"}
        except Exception:
            continue
    return {"email": email, "plan": "beta", "status": "free_during_beta"}

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
