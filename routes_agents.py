#!/usr/bin/env python3
"""Agent-facing REST endpoints: track spend, set budgets, read reports.
Extracted from api_server.py (2026-09-10) so this file has one
responsibility an agent (or a human) can hold fully in context, instead
of it living inside the app's every-endpoint monolith."""
import html
import json
import os
import re
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
    # Optional (BUILD-4): when omitted, the amount is computed from tokens +
    # model against the price table, so a caller that already knows its token
    # usage never has to do the dollar arithmetic itself.
    amount_cents: Optional[int] = Field(default=None, ge=0, le=MAX_AMOUNT_CENTS)
    service: Optional[str] = None
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    # Input is billed in up to three tiers. A caller that reports only
    # tokens_in gets every input token charged at the standard rate; these let
    # it report the split so cached input is priced as cached input. Claude
    # Code, for instance, spends most of its input on 1-hour cache writes.
    cache_hit_in: int = Field(default=0, ge=0)
    cache_write_5m_in: int = Field(default=0, ge=0)
    cache_write_1h_in: int = Field(default=0, ge=0)
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
    """Shared auth gate for every write endpoint. Claiming a NEW agent_id
    requires a valid workspace_key (obtained by paying via
    POST /v1/billing/x402 — no human, no login — or at /start, which mints
    a workspace and shows its key once); that first write mints the
    agent's own secret, which every later write to that agent_id must carry
    instead. Also enforces the workspace's agent-slot cap. Raises
    HTTPException on failure."""
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

    Credentials come from headers only. The browser-session arm is gone with
    Google sign-in (D-1162): there is no login, so there is no cookie that
    could authorize a read."""
    import identity
    if not identity.authorize_agent_access(
            agent_id,
            agent_secret=request.headers.get("x-agent-secret", ""),
            workspace_key=request.headers.get("x-workspace-key", "")):
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
    #
    # It runs before PRICING as well: whole_cents_with_residue writes per-agent
    # state, so pricing an unauthenticated request let anyone create files
    # under a foreign agent_id and perturb another tenant's carried residue
    # (adversarial review 2026-09-13).
    secret, created = _claim_or_401(req.agent_id, req.agent_secret, req.workspace_key)
    idem_key = request.headers.get("Idempotency-Key")
    #
    # BUILD-4: a tokens-only write is priced here, from the same table the
    # proxy uses. Refusing an unpriced model is deliberate — recording 0 would
    # be a silent lie about money that really was spent.
    auto_priced = False
    amount = req.amount_cents
    service = req.service
    if amount is None:
        if req.rail == "tokens":
            amount = 0
        else:
            if not req.model or not (req.tokens_in or req.tokens_out):
                raise HTTPException(422, detail=error_envelope(
                    422, "amount_cents is required unless you send tokens_in/tokens_out "
                         "AND model, in which case it is computed for you",
                    code="amount_required"))
            import proxy as _proxy
            exact = _proxy.cost_cents_exact(
                req.model, req.tokens_in, req.tokens_out,
                cache_hit_in=req.cache_hit_in,
                cache_write_5m_in=req.cache_write_5m_in,
                cache_write_1h_in=req.cache_write_1h_in)
            if exact is None:
                raise HTTPException(422, detail=error_envelope(
                    422, f"no price for model '{req.model}' — send amount_cents explicitly, "
                         f"or check GET /v1/pricing",
                    code="model_not_priced"))
            amount = _proxy.whole_cents_with_residue(req.agent_id, exact)
            auto_priced = True
            if not service:
                service = (_proxy.lookup(req.model) or {}).get("provider") or "unknown"
    if not service:
        service = req.model or "manual"

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
        # Carried onto the token row too, so the burn report shows HOW the
        # input was billed (fresh vs cached) and not just the total.
        if req.cache_hit_in:
            tok_meta["cache_hit_in"] = req.cache_hit_in
        if req.cache_write_5m_in:
            tok_meta["cache_write_5m_in"] = req.cache_write_5m_in
        if req.cache_write_1h_in:
            tok_meta["cache_write_1h_in"] = req.cache_write_1h_in
    primary_meta = tok_meta if req.rail == "tokens" else {}
    try:
        entry = track(req.agent_id, req.rail, amount, service, **primary_meta)
    except ValidationError as e:
        idempotency_release(idem_key, req.agent_id, "track")
        try:
            metrics.record_event("validation_fail")
        except Exception:
            pass
        raise HTTPException(422, detail=error_envelope(422, str(e)))
    except BudgetExceededError as e:
        idempotency_release(idem_key, req.agent_id, "track")
        # A blocked write is the ONLY way "budget exceeded" is ever real: the
        # entry that would cross 100% never lands, so _check_budget (which runs
        # after a successful write) can never see it. Alert at the rejection.
        try:
            from ledger_engine import _log_alert_daily
            _log_alert_daily(req.agent_id, "budget_blocked", str(e))
        except Exception:
            pass
        raise HTTPException(402, detail=error_envelope(402, str(e)))
    if req.rail != "tokens" and tok_meta:
        track(req.agent_id, "tokens", 0, req.model or service, **tok_meta)
    result = entry.to_dict()
    if auto_priced:
        result["priced"] = "auto"
        import proxy as _proxy
        resolved, how = _proxy.resolve_model(req.model)
        if how:
            # The caller sent a real provider id (claude-sonnet-4-20250514), not
            # the short key. Say so explicitly: a silently substituted price is
            # exactly the kind of thing a spend product must not do quietly.
            result["priced_model"] = resolved
            result["_note"] = (f"amount_cents was computed from {req.tokens_in + req.tokens_out} "
                               f"tokens on '{req.model}' ({how}). Send amount_cents to override.")
        else:
            result["_note"] = (f"amount_cents was computed from {req.tokens_in + req.tokens_out} "
                               f"tokens on '{req.model}'. Send amount_cents to override.")
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
        # Activation (D-1163): `created` means this same call CLAIMED the
        # agent, i.e. the workspace's first successful write. That is the step
        # that separates a real signup from a workspace nobody ever used.
        if created:
            try:
                import identity
                ws = identity.resolve_workspace_key(req.workspace_key)
                if ws:
                    metrics.record_onboarding("agent_claimed", ws)
                    metrics.record_onboarding("track_written", ws)
            except Exception:
                pass
        # Stickiness (D-1270 follow-up): traffic and mint counts answer "did
        # anyone show up" -- this answers "is the same workspace still relying
        # on it a day later." Runs on EVERY write, not just the first, since a
        # workspace only "returns" on a write that happens after its first one.
        try:
            import identity, workspace_engine
            ws_id = identity.workspace_of_agent(req.agent_id)
            if ws_id and workspace_engine.mark_write_and_check_returning(
                    ws_id, returning_after_seconds=metrics.RETURNING_WORKSPACE_SECONDS):
                metrics.record_onboarding("workspace_returned", ws_id)
        except Exception:
            pass
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

def _render_report_page(agent_id: str, r, days: int) -> str:
    """The report page itself. Shared by the header-auth path and the
    share-link path so the two can never drift into rendering differently."""
    budget = r.budget_status or {}
    cap_cents = budget.get("monthly_cap_cents")
    token_cap = budget.get("monthly_token_cap")
    pct = budget.get("pct_used", 0)
    token_pct = budget.get("token_pct_used", 0)
    exceeded = bool(budget.get("exceeded") or budget.get("token_exceeded"))
    safe_agent_id = html.escape(agent_id)
    plan_badge = {"free": "Free", "pro_scarcity": "Pro (launch window)",
                  "pro_stripe": "Pro", "pro_workspace": "Pro"}.get(
                      r.plan, html.escape(r.plan))

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
    return page_html


@router.get("/v1/report/{agent_id}/html", response_class=HTMLResponse)
def get_report_html(agent_id: str, request: Request, days: int = 30,
                    t: Optional[str] = None):
    """Human-readable version of /v1/report/{agent_id} — rendered instead of
    raw JSON. Two ways in:

      * `X-Agent-Secret` / `X-Workspace-Key` header (the agent's own path), or
      * `?t=<share token>` minted by POST /v1/report/{agent_id}/share.

    The header path is for agents. The token path exists because a browser
    cannot send a header — so before D-1217 the "shareable link" these docs
    advertised could not actually be opened by a human. A bad, expired or
    revoked token gets a friendly HTML page, never raw JSON.
    """
    from ledger_engine import validate_agent_id
    try:
        validate_agent_id(agent_id)
    except ValidationError as e:
        raise HTTPException(422, str(e))
    if t:
        import share_tokens
        ok, reason = share_tokens.verify(t, agent_id)
        if not ok:
            return HTMLResponse(content=share_tokens.error_page(reason),
                                status_code=403)
    else:
        _authorize_agent_read(agent_id, request)
    r = report(agent_id, days)
    return HTMLResponse(content=_render_report_page(agent_id, r, days))


@router.post("/v1/report/{agent_id}/share")
def mint_share_link(agent_id: str, request: Request, ttl_days: int = 7):
    """Mint a read-only, expiring link to this agent's report page.

    Authorized by the agent's own secret OR its workspace_key: this is a READ
    grant, so either credential that can already read the report may share it.
    The token is scoped to THIS agent_id — it cannot write, cannot rotate, and
    cannot read another agent's report. TTL default 7 days, hard max 90.
    """
    from ledger_engine import validate_agent_id
    import share_tokens
    try:
        validate_agent_id(agent_id)
    except ValidationError as e:
        raise HTTPException(422, detail=error_envelope(
            422, str(e), code="invalid_agent_id"))
    _authorize_agent_read(agent_id, request)
    if ttl_days < 1 or ttl_days > share_tokens.MAX_TTL_DAYS:
        raise HTTPException(422, detail=error_envelope(
            422, f"ttl_days must be between 1 and {share_tokens.MAX_TTL_DAYS}",
            code="invalid_ttl"))
    token, expires_at = share_tokens.mint(agent_id, ttl_days)
    base = str(request.base_url).rstrip("/")
    return {"agent_id": agent_id,
            "url": f"{base}/v1/report/{agent_id}/html?t={token}",
            "expires_at": expires_at,
            "ttl_days": ttl_days,
            "_note": ("Anyone holding this URL can read this agent's report until it "
                      "expires. It cannot write and cannot read other agents. "
                      "POST /v1/report/{agent_id}/share/revoke kills every "
                      "outstanding link for this agent.")}


@router.post("/v1/report/{agent_id}/share/revoke")
def revoke_share_links(agent_id: str, request: Request):
    """Invalidate every outstanding share link for this agent_id.

    Bumps the agent's share epoch, so tokens already handed out stop verifying.
    Same credentials as minting. Safe to call repeatedly.
    """
    from ledger_engine import validate_agent_id
    import share_tokens
    try:
        validate_agent_id(agent_id)
    except ValidationError as e:
        raise HTTPException(422, detail=error_envelope(
            422, str(e), code="invalid_agent_id"))
    _authorize_agent_read(agent_id, request)
    epoch = share_tokens.bump_epoch(agent_id)
    return {"agent_id": agent_id, "revoked": True, "share_epoch": epoch,
            "_note": "All previously issued share links for this agent no longer work."}


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


class RotateSecretRequest(BaseModel):
    workspace_key: Optional[str] = None


class WebhookRequest(BaseModel):
    url: str
    events: Optional[list] = None
    label: str = ""


@router.post("/v1/webhooks")
def create_webhook(req: WebhookRequest, request: Request):
    """Register a destination for this workspace's alerts.

    Workspace-scoped (X-Workspace-Key). `url` must be http(s) — point it at
    Slack, Discord, Zapier or your own endpoint. Email delivery is not offered
    (removed 2026-09-13): the product does not send mail to arbitrary addresses
    on a user's behalf. Events: alert.raised, budget.warning (80%),
    budget.exceeded, anomaly.detected — omit `events` to receive all of them.

    Payloads carry cost metadata only: event, agent_id, message, timestamp,
    report URL. Never a secret, never a prompt, never a response.
    """
    import alert_delivery
    workspace_id = _workspace_key_or_401(
        request, purpose="configuring alert delivery")
    try:
        entry = alert_delivery.register(workspace_id, req.url, req.events,
                                       label=req.label)
    except alert_delivery.WebhookError as e:
        raise HTTPException(422, detail=error_envelope(422, str(e), code="invalid_webhook"))
    return {"webhook": entry,
            "_note": ("Events are delivered as they happen, with retries. Every "
                      "attempt is recorded — GET /v1/webhooks/deliveries shows "
                      "what was delivered and what was not.")}


@router.get("/v1/webhooks")
def list_webhooks(request: Request):
    """This workspace's registered alert destinations."""
    import alert_delivery
    workspace_id = _workspace_key_or_401(
        request, purpose="configuring alert delivery")
    entries = alert_delivery.load_registry(workspace_id)
    return {"count": len(entries), "webhooks": entries,
            "events": list(alert_delivery.EVENTS)}


@router.get("/v1/webhooks/deliveries")
def list_deliveries(request: Request, limit: int = 50):
    """Delivery receipts, newest last. A failure here is visible on purpose —
    a silently dropped alert is the exact thing this feature exists to stop."""
    import alert_delivery
    workspace_id = _workspace_key_or_401(
        request, purpose="configuring alert delivery")
    items = alert_delivery.recent_deliveries(workspace_id, limit=max(1, min(limit, 200)))
    failed = [i for i in items if i.get("status") != "delivered"]
    return {"count": len(items), "failed": len(failed), "deliveries": items}


@router.delete("/v1/webhooks/{webhook_id}")
def delete_webhook(webhook_id: str, request: Request):
    """Remove one registered destination."""
    import alert_delivery
    workspace_id = _workspace_key_or_401(
        request, purpose="configuring alert delivery")
    if not re.match(r"^wh_[a-f0-9]{16}$", webhook_id or ""):
        raise HTTPException(422, detail=error_envelope(422, "malformed webhook id",
                                                       code="invalid_webhook"))
    if not alert_delivery.unregister(workspace_id, webhook_id):
        raise HTTPException(404, detail=error_envelope(
            404, f"no such webhook in this workspace: {webhook_id}",
            code="webhook_not_found"))
    return {"deleted": webhook_id}


def _workspace_key_or_401(request: Request,
                          body_key: Optional[str] = None,
                          purpose: str = "this endpoint") -> str:
    """Credential-lifecycle gate (D-1216): ONLY the workspace_key authorizes
    rotating or revoking an agent_secret — never the agent_secret itself.

    The workspace owner must always be able to recover an agent whose secret
    was lost. If the agent_secret could rotate, then a leaked agent credential
    would let whoever holds it lock the real owner out of their own agent
    permanently — turning a recovery feature into a takeover primitive.

    Runs BEFORE any existence check, deliberately. Checking existence first
    made an unauthenticated request answer 404 for an unclaimed agent_id and
    401 for a claimed one, which lets anyone enumerate real tenants by name
    (confirmed live 2026-09-13). Order: authenticate, then authorize, then
    reveal existence.
    """
    import identity
    raw = request.headers.get("x-workspace-key") or (body_key or "")
    workspace_id = identity.resolve_workspace_key(raw)
    if not workspace_id:
        raise HTTPException(401, detail=error_envelope(
            401, f"{purpose} requires a valid X-Workspace-Key",
            code="workspace_key_required"))
    return workspace_id


def _owned_or_404(agent_id: str, workspace_id: str) -> None:
    """Require that this workspace owns the agent — answering 404, not 403.

    A 403 for 'exists but is not yours' next to a 404 for 'never existed' is a
    tenant-enumeration oracle: anyone holding any valid workspace_key could ask
    which agent_ids exist in other workspaces. Both cases now return an
    identical agent_not_claimed response. (Adversarial review 2026-09-13 — the
    earlier fix covered the anonymous caller and missed the authenticated
    non-owner, which is the case that needs a valid credential to exploit and is
    therefore the easier one to reach.)
    """
    import identity
    if not identity.agent_belongs_to_workspace(agent_id, workspace_id):
        raise HTTPException(404, detail=error_envelope(
            404, f"agent_id '{agent_id}' is not claimed", code="agent_not_claimed"))


def _claimed_or_404(agent_id: str) -> None:
    """Validate the id, then require that it is already claimed. Rotation and
    revocation are recovery operations on an id you own — neither is a claim
    path, so an unknown id is 404 rather than a silent mint."""
    from ledger_engine import agent_exists
    try:
        le_validate_agent_id(agent_id)
    except ValidationError as e:
        raise HTTPException(422, detail=error_envelope(422, str(e), code="invalid_agent_id"))
    if not agent_exists(agent_id):
        raise HTTPException(404, detail=error_envelope(
            404, f"agent_id '{agent_id}' is not claimed", code="agent_not_claimed"))


@router.post("/v1/agents/{agent_id}/rotate-secret")
def rotate_secret(agent_id: str, request: Request,
                  body: Optional[RotateSecretRequest] = None):
    """Recover a lost agent_secret: mint a new one for an agent_id you own.

    Authenticated by the WORKSPACE_KEY only — header X-Workspace-Key, or
    workspace_key in the body. The previous secret stops working the moment
    this returns; the new one is shown once, the same contract as a first
    claim. The rotation is written to the agent's audit trail. An unclaimed
    agent_id is 404 (this is not a claim path), and an agent belonging to a
    different workspace is ALSO 404 with the identical body — a different status
    would confirm that the id exists somewhere else.
    """
    from ledger_engine import rotate_agent_secret as _rotate, AuthError as _LedgerAuthError
    workspace_id = _workspace_key_or_401(
        request, body.workspace_key if body else None,
        purpose="rotating or revoking an agent_secret")
    _claimed_or_404(agent_id)
    _owned_or_404(agent_id, workspace_id)
    try:
        secret = _rotate(agent_id, workspace_id)
    except _LedgerAuthError as e:
        raise HTTPException(404, detail=error_envelope(404, str(e), code="agent_not_claimed"))
    return {"agent_id": agent_id, "agent_secret": secret,
            "_note": ("Save this agent_secret — it replaced the previous one, which no "
                      "longer works. It will not be shown again.")}


@router.post("/v1/agents/{agent_id}/revoke-secret")
def revoke_secret(agent_id: str, request: Request,
                  body: Optional[RotateSecretRequest] = None):
    """Invalidate an agent_id's secret without deleting its ledger.

    Same workspace_key-only auth as rotate. Writes to the agent then fail with
    401 agent_secret_mismatch until the owner rotates a new secret in. The id
    stays CLAIMED, so no other workspace can claim it and inherit the spend
    history.
    """
    from ledger_engine import revoke_agent_secret as _revoke, AuthError as _LedgerAuthError
    workspace_id = _workspace_key_or_401(
        request, body.workspace_key if body else None,
        purpose="rotating or revoking an agent_secret")
    _claimed_or_404(agent_id)
    _owned_or_404(agent_id, workspace_id)
    try:
        _revoke(agent_id, workspace_id)
    except _LedgerAuthError as e:
        raise HTTPException(404, detail=error_envelope(404, str(e), code="agent_not_claimed"))
    return {"agent_id": agent_id, "revoked": True,
            "_note": ("The previous agent_secret no longer works. The agent's spend "
                      "history is intact — POST /v1/agents/{agent_id}/rotate-secret "
                      "issues a new one when you want it writing again.")}


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
