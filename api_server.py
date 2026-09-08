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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ledger_engine import track, set_budget, get_budget, report, list_agents, _ledger_path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="AgentLedger API", version="0.1.0")

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
    amount_cents: int
    service: str
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""

class BudgetRequest(BaseModel):
    agent_id: str
    monthly_cents: int
    daily_cents: int = 0

@app.get("/health")
def health():
    return {"ok": True, "service": "agent-ledger", "version": "0.1.0"}

BETA_AGENT_CAP = 3  # beta: 3 tracked agents free; Pro ($19/mo) unlimited

# ── scalability: never walk the full agents tree per-request ─────────────────
# list_agents() reads every ledger row (O(agents × rows)); at 10K agents that is
# 500K+ parses per call. The beta-cap check only needs (a) does THIS agent exist
# — O(1) filesystem check — and (b) how many agents exist — a dir-name count,
# cached with a short TTL and rebuilt under a lock.
import threading as _threading
import time as _time

_agents_count_cache = {"ts": 0.0, "count": 0}
_agents_cache_lock = _threading.Lock()
AGENTS_CACHE_TTL = 30.0  # seconds; beta cap staleness window

def _cached_agent_count() -> int:
    now = _time.time()
    if now - _agents_count_cache["ts"] < AGENTS_CACHE_TTL:
        return _agents_count_cache["count"]
    with _agents_cache_lock:
        if now - _agents_count_cache["ts"] < AGENTS_CACHE_TTL:
            return _agents_count_cache["count"]
        agents_dir = DATA_DIR / "agents"
        count = 0
        if agents_dir.exists():
            for d in agents_dir.iterdir():
                if d.is_dir() and (d / "ledger.jsonl").exists():
                    count += 1
        _agents_count_cache["count"] = count
        _agents_count_cache["ts"] = _time.time()
        return count

@app.post("/v1/track")
def create_track(req: TrackRequest):
    _log_event("track")
    # O(1) membership: an agent is "tracked" iff its ledger file exists
    if not _ledger_path(req.agent_id).exists():
        if _cached_agent_count() >= BETA_AGENT_CAP:
            raise HTTPException(402, (
                f"Beta limit: {BETA_AGENT_CAP} agents tracked. Upgrade to Pro ($19/mo) "
                "for unlimited agents — https://buy.stripe.com/14AbJ0clUeoE9QN3Nl2400e "
                "— or contact entradox@icloud.com"))
    entry = track(req.agent_id, req.rail, req.amount_cents, req.service)
    # token dimension: token counts stored SEPARATELY from the dollar ledger
    # (never mixed — token counts are not cents) via meta on the entry
    if req.tokens_in or req.tokens_out:
        tok_meta = {"tokens_in": req.tokens_in, "tokens_out": req.tokens_out}
        if req.model:
            tok_meta["model"] = req.model
        track(req.agent_id, "tokens", 0, req.model or req.service, **tok_meta)
    return entry.to_dict()

@app.post("/v1/budget")
def create_budget(req: BudgetRequest):
    b = set_budget(req.agent_id, req.monthly_cents, req.daily_cents)
    return b.to_dict()

@app.get("/v1/report/{agent_id}")
def get_report(agent_id: str, days: int = 30):
    r = report(agent_id, days)
    return {"agent_id": r.agent_id, "period": r.period,
            "total_spend_cents": r.total_spend_cents, "by_rail": r.by_rail,
            "by_service": r.by_service, "budget_status": r.budget_status,
            "anomalies": r.anomalies, "entry_count": r.entry_count}

@app.get("/v1/alerts/{agent_id}")
def get_alerts(agent_id: str):
    alerts_path = DATA_DIR / "agents" / agent_id / "alerts.jsonl"
    if not alerts_path.exists():
        return {"count": 0, "alerts": []}
    alerts = [json.loads(l) for l in open(alerts_path)]
    return {"count": len(alerts), "alerts": alerts}

@app.get("/v1/agents")
def get_agents():
    return {"agents": list_agents()}

@app.get("/stats")
def stats():
    from collections import Counter
    c = Counter()
    if COUNTS_FILE.exists():
        for line in COUNTS_FILE.read_text().splitlines():
            try:
                c[json.loads(line).get("kind", "?")] += 1
            except (json.JSONDecodeError, KeyError):
                continue
    return {"tracked_agents": len(list_agents()), "events": dict(c)}

LLMS_TXT = """# AgentLedger

Per-agent spend management — the Datadog for agent spending. Track spend
across x402/MPP/API-key rails, budget caps, anomaly alerts, audit trails.

Machine-readable schema: GET /openapi.json (OpenAPI 3) · MCP manifest: GET /server.json
Human/agent status page: GET /status

## Endpoints

GET  /health                       — liveness
POST /v1/track                     — record a spend entry
     body: {"agent_id": str, "rail": str, "amount_cents": int, "service": str,
            "tokens_in": int (optional), "tokens_out": int (optional), "model": str (optional)}
POST /v1/budget                    — set budget caps
     body: {"agent_id": str, "monthly_cents": int, "daily_cents": int (optional)}
GET  /v1/report/{agent_id}         — spend report (query: days=30)
GET  /v1/tokens/{agent_id}         — token burn report: in/out totals + by model (query: days=30)
GET  /v1/alerts/{agent_id}         — alerts for agent
GET  /v1/agents                    — list all tracked agents
GET  /stats                        — usage counters

## MCP

Registry: io.github.entradox/agent-ledger
Remote:   https://agent-ledger-production-0ff8.up.railway.app/mcp/

Tools exposed at POST /mcp/:
  ledger_track          — record a spend entry
  ledger_set_budget     — set a budget cap
  ledger_report         — get a spend report
  ledger_alerts         — get alerts for an agent
  ledger_list_agents    — list all tracked agents

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
    if secret:
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
    admin_secret = os.environ.get("AL_ADMIN_SECRET", "")
    if not admin_secret or request.headers.get("x-al-admin") != admin_secret:
        raise HTTPException(401, "owner only")
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
