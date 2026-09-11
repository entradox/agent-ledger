#!/usr/bin/env python3
"""Agent-facing REST endpoints: track spend, set budgets, read reports.
Extracted from api_server.py (2026-09-10) so this file has one
responsibility an agent (or a human) can hold fully in context, instead
of it living inside the app's every-endpoint monolith."""
import html
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from ledger_engine import (
    track, set_budget, get_budget, report, DATA_DIR,
    AuthError, ValidationError, BudgetExceededError, BetaCapExceededError,
    WorkspaceKeyRequiredError, error_envelope, AL_API_VERSION,
    IdempotencyKeyTooLongError, IdempotencyConflictError,
    idempotency_begin, idempotency_store, idempotency_release,
    validate_agent_id as le_validate_agent_id, _ledger_path,
    ensure_agent_secret, MAX_AMOUNT_CENTS, VALID_RAILS,
)
import metrics

router = APIRouter()

# Module-level constants (same pattern as api_server.py)
COUNTS_FILE = DATA_DIR / "counts.jsonl"


class TrackRequest(BaseModel):
    agent_id: str
    rail: str
    amount_cents: int = Field(ge=0, le=MAX_AMOUNT_CENTS)
    service: str
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    model: str = ""
    agent_secret: Optional[str] = None
    workspace_key: Optional[str] = None

class BudgetRequest(BaseModel):
    agent_id: str
    monthly_cents: int = Field(ge=0)
    daily_cents: int = Field(default=0, ge=0)
    monthly_tokens: int = Field(default=0, ge=0)
    daily_tokens: int = Field(default=0, ge=0)
    agent_secret: Optional[str] = None
    workspace_key: Optional[str] = None


def _claim_or_401(agent_id: str, provided_secret: Optional[str],
                 workspace_key: Optional[str] = None):
    """Shared auth gate for every write endpoint. Mints a secret on first use
    of a new agent_id (no signup), verifies it on every later write, and
    enforces the beta agent-slot cap. New claims on a workspace-bound
    agent_id require a valid workspace_key. Raises HTTPException on failure."""
    try:
        return ensure_agent_secret(agent_id, provided_secret, workspace_key=workspace_key)
    except AuthError as e:
        try:
            metrics.record_event("auth_fail")
        except Exception:
            pass
        raise HTTPException(401, detail=error_envelope(401, str(e), code="agent_secret_mismatch"))
    except BetaCapExceededError as e:
        # Pro exemption is handled inside ensure_agent_secret(); anything that
        # still reaches here is a genuinely free-tier cap hit.
        try:
            metrics.record_event("cap_blocked")
        except Exception:
            pass
        raise HTTPException(402, detail=error_envelope(402, str(e), code="beta_cap_exceeded"))
    except WorkspaceKeyRequiredError as e:
        try:
            metrics.record_event("workspace_key_required")
        except Exception:
            pass
        raise HTTPException(401, detail=error_envelope(401, str(e), code="workspace_key_required"))
    except ValidationError as e:
        # malformed agent_id (traversal, bad charset) — 422, not a 500
        try:
            metrics.record_event("validation_fail")
        except Exception:
            pass
        raise HTTPException(422, detail=error_envelope(422, str(e), code="invalid_agent_id"))


def _check_api_version(request: Request):
    """Every /v1/* write must send AL-API-Version: <current>. Missing or
    stale/invalid value -> 400 version_header (launch-kit v0.3 item 1.2)."""
    version = request.headers.get("AL-API-Version")
    if version != AL_API_VERSION:
        try:
            metrics.record_event("validation_fail")
        except Exception:
            pass
        raise HTTPException(400, detail=error_envelope(
            400, f"AL-API-Version header must be '{AL_API_VERSION}' (got {version!r})",
            code="version_header"))


def _idempotency_gate(request: Request, agent_id: str, op: str):
    """Shared Idempotency-Key handling for POST endpoints. Returns a cached
    JSONResponse to return immediately, or None if the caller should proceed
    (and must call idempotency_store() itself once the write succeeds)."""
    key = request.headers.get("Idempotency-Key")
    try:
        status, cached = idempotency_begin(key, agent_id, op)
    except IdempotencyKeyTooLongError as e:
        raise HTTPException(400, detail=error_envelope(
            400, str(e), code="idempotency_key_too_long"))
    except IdempotencyConflictError as e:
        raise HTTPException(409, detail=error_envelope(
            409, str(e), code="idempotency_conflict"))
    if status == "cached":
        try:
            metrics.record_event("idempotency_hit")
        except Exception:
            pass
        return JSONResponse(status_code=cached["status_code"], content=cached["response"])
    return None


def _authorize_agent_read(agent_id: str, request: Request) -> None:
    """Reads used to be fully open (pre-workspace design). Now require the
    agent's own secret, its workspace's key, or a logged-in session cookie
    for that workspace — all checked via identity.authorize_agent_access(),
    the same function the claim gate (ledger_engine.ensure_agent_secret)
    resolves identity through. This wrapper's only job is pulling headers
    and cookies off Request and raising the HTTP error — it holds no
    credential-comparison logic of its own.

    The cookie arm exists because the dashboard links to
    /v1/report/{agent_id}/html and a browser following that link sends no
    headers at all; it is authorized through the same workspace-ownership
    comparison as X-Workspace-Key, never a special case."""
    import identity
    if not identity.authorize_agent_access(
            agent_id,
            agent_secret=request.headers.get("x-agent-secret", ""),
            workspace_key=request.headers.get("x-workspace-key", ""),
            session_cookie=request.cookies.get("al_session", "")):
        raise HTTPException(401, detail=error_envelope(
            401, "this agent's data requires its agent_secret or workspace_key",
            code="agent_secret_mismatch"))


def _log_event(kind):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(COUNTS_FILE, "a") as f:
            f.write(json.dumps({"ts": time.time(), "kind": kind}) + "\n")
    except Exception:
        pass


@router.post("/v1/track")
def create_track(req: TrackRequest, request: Request):
    _log_event("track")
    _check_api_version(request)
    # Validate BEFORE the claim/mint step: a garbage rail or amount must not
    # burn a free-tier agent slot (the minted secret is only returned on a
    # successful write, so failing after minting would squat the agent_id).
    try:
        le_validate_agent_id(req.agent_id)
    except ValidationError as e:
        try:
            metrics.record_event("validation_fail")
        except Exception:
            pass
        raise HTTPException(422, detail=error_envelope(422, str(e), code="invalid_agent_id"))
    if req.rail != "tokens":
        from ledger_engine import VALID_RAILS
        if req.rail not in VALID_RAILS:
            try:
                metrics.record_event("validation_fail")
            except Exception:
                pass
            raise HTTPException(422, detail=error_envelope(
                422, f"rail must be one of {sorted(VALID_RAILS)} (got '{req.rail}')",
                code="rail_not_allowed"))
    # SECURITY ORDER: auth (claim) runs BEFORE the idempotency gate — an
    # unauthenticated caller must not be able to reserve/409 a legitimate
    # retry or read the cache (Morgan review 2026-09-09). Failed writes
    # release their in-flight row so honest retries re-attempt.
    idem_key = request.headers.get("Idempotency-Key")
    secret, created = _claim_or_401(req.agent_id, req.agent_secret, req.workspace_key)
    cached = _idempotency_gate(request, req.agent_id, "track")
    if cached is not None:
        return cached
    # token dimension: token counts stored SEPARATELY from the dollar ledger
    # (never mixed — token counts are not cents) via meta on the entry. When
    # rail="tokens" IS the primary write, tok_meta rides on that single entry;
    # a dollar-rail write that also reports token burn gets a second, separate
    # 0-cent "tokens" row below. Attaching it twice for a tokens-primary call
    # would leave a duplicate, metadata-less garbage row behind.
    tok_meta = {}
    if req.tokens_in or req.tokens_out:
        tok_meta = {"tokens_in": req.tokens_in, "tokens_out": req.tokens_out}
        if req.model:
            tok_meta["model"] = req.model
    primary_meta = tok_meta if req.rail == "tokens" else {}
    try:
        entry = track(req.agent_id, req.rail, req.amount_cents, req.service, **primary_meta)
    except ValidationError as e:
        idempotency_release(idem_key, req.agent_id, "track")
        try:
            metrics.record_event("validation_fail")
        except Exception:
            pass
        raise HTTPException(422, detail=error_envelope(422, str(e)))
    except BudgetExceededError as e:
        idempotency_release(idem_key, req.agent_id, "track")
        raise HTTPException(402, detail=error_envelope(402, str(e)))
    if req.rail != "tokens" and tok_meta:
        track(req.agent_id, "tokens", 0, req.model or req.service, **tok_meta)
    result = entry.to_dict()
    if created:
        result["agent_secret"] = secret
        result["_note"] = ("Save this agent_secret — required for every future write "
                            "to this agent_id (track/budget). It will not be shown again.")
    # SECURITY: the minted agent_secret must never be persisted in the
    # idempotency cache — a replayable cached response would re-expose the
    # secret to anyone who can name agent_id + key (Morgan review 2026-09-09).
    # First-call clients that lose the secret re-register a new agent_id.
    cached_payload = dict(result)
    cached_payload.pop("agent_secret", None)
    cached_payload["_note"] = ("agent_secret is shown once at claim time and is "
                               "not included in cached (idempotent) replays.")
    idempotency_store(idem_key, req.agent_id, "track", cached_payload, 200)
    try:
        metrics.record_event("track_ok")
    except Exception:
        pass
    return result

@router.post("/v1/budget")
def create_budget(req: BudgetRequest, request: Request):
    _check_api_version(request)
    try:
        le_validate_agent_id(req.agent_id)
    except ValidationError as e:
        try:
            metrics.record_event("validation_fail")
        except Exception:
            pass
        raise HTTPException(422, detail=error_envelope(422, str(e), code="invalid_agent_id"))
    # SECURITY ORDER: same as /v1/track — auth before gate; failed writes
    # release the in-flight row. Secret never enters the cache.
    idem_key = request.headers.get("Idempotency-Key")
    secret, created = _claim_or_401(req.agent_id, req.agent_secret, req.workspace_key)
    cached = _idempotency_gate(request, req.agent_id, "budget")
    if cached is not None:
        return cached
    try:
        b = set_budget(req.agent_id, req.monthly_cents, req.daily_cents,
                       monthly_tokens=req.monthly_tokens, daily_tokens=req.daily_tokens)
    except ValidationError as e:
        idempotency_release(idem_key, req.agent_id, "budget")
        try:
            metrics.record_event("validation_fail")
        except Exception:
            pass
        raise HTTPException(422, detail=error_envelope(422, str(e)))
    result = b.to_dict()
    if created:
        result["agent_secret"] = secret
        result["_note"] = ("Save this agent_secret — required for every future write "
                           "to this agent_id (track/budget). It will not be shown again.")
    cached_payload = dict(result)
    cached_payload.pop("agent_secret", None)
    cached_payload["_note"] = ("agent_secret is shown once at claim time and is "
                               "not included in cached (idempotent) replays.")
    idempotency_store(idem_key, req.agent_id, "budget", cached_payload, 200)
    try:
        metrics.record_event("budget_set")
    except Exception:
        pass
    return result

@router.get("/v1/report/{agent_id}")
def get_report(agent_id: str, request: Request, days: int = 30):
    _authorize_agent_read(agent_id, request)
    r = report(agent_id, days)
    return {"agent_id": r.agent_id, "period": r.period,
            "total_spend_cents": r.total_spend_cents, "by_rail": r.by_rail,
            "by_service": r.by_service, "budget_status": r.budget_status,
            "anomalies": r.anomalies, "entry_count": r.entry_count,
            "plan": r.plan, "pro_until": r.pro_until}

@router.get("/v1/report/{agent_id}/html", response_class=HTMLResponse)
def get_report_html(agent_id: str, request: Request, days: int = 30):
    """Human-readable version of /v1/report/{agent_id} — same
    workspace/agent-scoped read (requires X-Agent-Secret or X-Workspace-Key),
    just rendered instead of raw JSON. This is the page an agent's owner (or
    the agent itself, sharing a link) actually looks at, rather than
    curl-ing JSON to see if a budget is close to tripping."""
    from ledger_engine import validate_agent_id
    try:
        validate_agent_id(agent_id)
    except ValidationError as e:
        raise HTTPException(422, str(e))
    _authorize_agent_read(agent_id, request)
    r = report(agent_id, days)
    budget = r.budget_status or {}
    cap_cents = budget.get("monthly_cap_cents")
    token_cap = budget.get("monthly_token_cap")
    pct = budget.get("pct_used", 0)
    token_pct = budget.get("token_pct_used", 0)
    exceeded = bool(budget.get("exceeded") or budget.get("token_exceeded"))
    safe_agent_id = html.escape(agent_id)
    plan_badge = {"free": "Free", "pro_scarcity": "Pro (launch window)",
                  "pro_stripe": "Pro"}.get(r.plan, html.escape(r.plan))

    def bar(used_pct: float, danger: bool) -> str:
        used_pct = max(0.0, min(100.0, used_pct))
        color = "#f85149" if danger else "#3fb950"
        return (f'<div style="background:#21262d;border-radius:6px;height:10px;width:100%;max-width:360px">'
                f'<div style="background:{color};height:10px;border-radius:6px;width:{used_pct:.0f}%"></div></div>')

    rail_rows = "".join(
        f"<tr><td>{html.escape(rail)}</td><td>${cents/100:.2f}</td></tr>"
        for rail, cents in r.by_rail.items()) or '<tr><td colspan=2>No spend yet.</td></tr>'
    anomaly_rows = "".join(
        f"<li>{html.escape(str(a))}</li>" for a in r.anomalies) or "<li>None</li>"

    budget_html = ""
    if cap_cents:
        budget_html += (f'<p>Dollar budget: <b>${cap_cents/100:.2f}/mo</b> — '
                         f'{pct:.0f}% used</p>{bar(pct, pct >= 80)}')
    if token_cap:
        budget_html += (f'<p style="margin-top:14px">Token budget: <b>{token_cap:,}/mo</b> — '
                         f'{token_pct:.0f}% used</p>{bar(token_pct, token_pct >= 80)}')
    if not cap_cents and not token_cap:
        budget_html = '<p style="color:#8b949e">No budget cap set — spend is tracked but not enforced.</p>'
    if exceeded:
        budget_html += '<p style="color:#f85149;font-weight:600">⚠️ Budget exceeded — writes are being blocked.</p>'

    page_html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>AgentLedger — {safe_agent_id}</title>
<style>
body{{background:#0d1117;color:#e6edf3;font-family:-apple-system,sans-serif;padding:24px;max-width:640px;margin:0 auto}}
table{{border-collapse:collapse;width:100%;margin:8px 0 16px}}
th,td{{text-align:left;padding:6px 10px;border-bottom:1px solid #30363d;font-size:13px}}
th{{color:#8b949e;font-weight:600}}
h1{{font-size:20px;margin-bottom:2px}} .sub{{color:#8b949e;font-size:12px;margin-bottom:20px}}
.badge{{display:inline-block;background:#238636;color:#fff;border-radius:4px;padding:2px 8px;font-size:11px}}
</style></head><body>
<h1>{safe_agent_id} <span class="badge">{plan_badge}</span></h1>
<div class="sub">Last {days} days · total spend ${r.total_spend_cents/100:.2f} · {r.entry_count} entries</div>
{budget_html}
<h3 style="margin-top:24px;font-size:14px">Spend by rail</h3>
<table>{rail_rows}</table>
<h3 style="font-size:14px">Anomalies</h3>
<ul style="font-size:13px;color:#8b949e">{anomaly_rows}</ul>
</body></html>"""
    return HTMLResponse(content=page_html)

@router.get("/v1/alerts/{agent_id}")
def get_alerts(agent_id: str, request: Request):
    from ledger_engine import validate_agent_id
    try:
        validate_agent_id(agent_id)
    except ValidationError as e:
        raise HTTPException(422, str(e))
    _authorize_agent_read(agent_id, request)
    alerts_path = DATA_DIR / "agents" / agent_id / "alerts.jsonl"
    if not alerts_path.exists():
        return {"count": 0, "alerts": []}
    alerts = [json.loads(l) for l in open(alerts_path)]
    return {"count": len(alerts), "alerts": alerts}

@router.get("/v1/tokens/{agent_id}")
def token_report(agent_id: str, request: Request, days: int = 30):
    """Token burn report: totals in/out, by model, per period. Separate from
    dollar spend — answers 'what is this agent burning on?'"""
    from ledger_engine import validate_agent_id
    try:
        validate_agent_id(agent_id)
    except ValidationError as e:
        raise HTTPException(422, str(e))
    _authorize_agent_read(agent_id, request)
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


@router.delete("/v1/agents/{agent_id}")
def delete_agent(agent_id: str, request: Request):
    """Remove an agent's ledger entirely. Owner-only (cron secret) — beta slots
    are per-product, so the operator can clear test/demo agents to free slots."""
    from ledger_engine import validate_agent_id, _delete_agent_row
    import hmac
    admin_secret = os.environ.get("AL_ADMIN_SECRET", "")
    if not admin_secret or not hmac.compare_digest(request.headers.get("x-al-admin", ""), admin_secret):
        raise HTTPException(401, "owner only")
    try:
        validate_agent_id(agent_id)
    except ValidationError as e:
        raise HTTPException(422, str(e))
    import shutil
    agent_dir = DATA_DIR / "agents" / agent_id
    if not agent_dir.exists():
        raise HTTPException(404, f"agent not found: {agent_id}")
    # D-819: the row goes FIRST, then the dir. A dir removed without its row
    # is an orphan row that diverges the scarcity counter from the window
    # gate — if the row delete fails, abort with BOTH intact rather than
    # leaving a silent orphan behind.
    try:
        _delete_agent_row(agent_id)
    except Exception as e:
        raise HTTPException(500, detail=error_envelope(
            500, f"could not delete agent record for '{agent_id}': {e}"))
    shutil.rmtree(agent_dir)
    return {"deleted": agent_id}
