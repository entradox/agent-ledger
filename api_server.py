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
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ledger_engine import track, set_budget, get_budget, report, list_agents

from fastapi import FastAPI, HTTPException
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

class BudgetRequest(BaseModel):
    agent_id: str
    monthly_cents: int
    daily_cents: int = 0

@app.get("/health")
def health():
    return {"ok": True, "service": "agent-ledger", "version": "0.1.0"}

@app.post("/v1/track")
def create_track(req: TrackRequest):
    _log_event("track")
    entry = track(req.agent_id, req.rail, req.amount_cents, req.service)
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

## Endpoints

GET  /health                       — liveness
POST /v1/track                     — record a spend entry
     body: {"agent_id": str, "rail": str, "amount_cents": int, "service": str}
POST /v1/budget                    — set budget caps
     body: {"agent_id": str, "monthly_cents": int, "daily_cents": int (optional)}
GET  /v1/report/{agent_id}         — spend report (query: days=30)
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

@app.get("/status", response_class=HTMLResponse)
def status_page():
    return (Path(__file__).parent / "status.html").read_text()

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
