#!/usr/bin/env python3
"""AgentLedger proxy routes — the enforcement surface (D-1222, BUILD-2).

Flow for one call:

    caller ──► identity check (X-AL-Agent / X-AL-Secret)
           ──► PRE-CALL budget check on the estimated maximum cost
                 └─ over cap? → 402 to the caller, upstream NEVER contacted
           ──► forward to the provider with the caller's own credential
           ──► POST-CALL meter from the provider's real `usage` tokens
                 └─ recorded through the normal ledger path, so reports,
                    alerts and webhooks all see proxied spend

The pre-call check is the entire point. Every other cap in this product blocks
a ledger write; this one stops the money.

No retries anywhere on the upstream call: a retried provider request is a
double charge. A failure surfaces as a typed error instead.
"""
import json
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import identity
import metrics
import proxy as proxy_core
from ledger_engine import BudgetExceededError, error_envelope

router = APIRouter()

UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)
RESPONSE_HEADER_BLOCKLIST = {"content-length", "transfer-encoding", "connection",
                             "keep-alive", "content-encoding"}


def _err(status: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content=error_envelope(status, message, code=code))


def _proxy_identity(request: Request) -> str:
    """X-AL-Agent + X-AL-Secret. The provider credential is NOT part of this:
    it stays in Authorization / x-api-key and is only ever forwarded."""
    agent_id = (request.headers.get("x-al-agent") or "").strip()
    secret = request.headers.get("x-al-secret") or ""
    if not agent_id:
        raise HTTPException(401, detail=error_envelope(
            401, "proxied calls require X-AL-Agent (the agent_id to bill) and X-AL-Secret",
            code="proxy_agent_required"))
    if not identity.authorize_agent_access(agent_id, agent_secret=secret):
        raise HTTPException(401, detail=error_envelope(
            401, f"X-AL-Secret does not match agent_id '{agent_id}'",
            code="agent_secret_mismatch"))
    return agent_id


def _meter(agent_id: str, provider: str, model: str,
           tokens_in: int, tokens_out: int, cache_hit_in: int = 0,
           cache_write_5m_in: int = 0, cache_write_1h_in: int = 0) -> int:
    """Record what the call actually cost. Never raises: the provider has
    already charged, so failing here would report a problem the caller cannot
    act on. Uses the ordinary ledger path so caps, alerts and webhooks apply."""
    from ledger_engine import track, _log_alert_daily
    exact = proxy_core.cost_cents_exact(model, tokens_in, tokens_out, cache_hit_in,
                                       cache_write_5m_in, cache_write_1h_in)
    if exact is None:
        # Unpriced model: record the real token burn at zero dollars and make
        # noise. A silent zero would read as 'this agent spends nothing'.
        try:
            track(agent_id, "tokens", 0, provider,
                  tokens_in=tokens_in, tokens_out=tokens_out, model=model,
                  cache_hit_in=cache_hit_in,
                  cache_write_5m_in=cache_write_5m_in,
                  cache_write_1h_in=cache_write_1h_in)
        except Exception:
            pass
        try:
            _log_alert_daily(
                agent_id, "unpriced_model",
                f"proxied call on '{model}' was NOT priced — no entry in "
                f"model_prices.json, so its cost is missing from your dollar "
                f"total. Tokens were recorded. Add the model to the price table.")
        except Exception:
            pass
        return 0
    fields = dict(tokens_in=tokens_in, tokens_out=tokens_out, model=model,
                  cache_hit_in=cache_hit_in,
                  cache_write_5m_in=cache_write_5m_in,
                  cache_write_1h_in=cache_write_1h_in)
    cents = proxy_core.whole_cents_with_residue(agent_id, exact)
    try:
        track(agent_id, "api_key", cents, provider, **fields)
    except BudgetExceededError as exc:
        # Post-hoc: the money is already spent. Record the miss loudly rather
        # than discarding the entry and hiding it.
        try:
            from ledger_engine import _log_alert_daily
            _log_alert_daily(agent_id, "budget_blocked",
                             f"a proxied call completed over budget and was recorded "
                             f"anyway (the provider had already charged): {exc}")
        except Exception:
            pass
        # The money is gone whether or not the ledger agrees. Force it in: a
        # ledger that drops the charge it could not block is worse than useless,
        # because the cap, the report and the alerts all read those totals.
        try:
            track(agent_id, "api_key", cents, provider, force=True, **fields)
        except Exception:
            pass
    # The token burn goes in its OWN row, the same way POST /v1/track splits a
    # dollar write that also reports tokens. Without this, a sub-cent call —
    # where amount_cents rounds to 0 — records tokens that no report shows:
    # GET /v1/tokens only reads rail="tokens" rows, so the burn would be
    # invisible while the money was real. Found by a test, not by review.
    if tokens_in or tokens_out:
        try:
            # force=True for the same reason as the dollar row: these tokens were
            # really burned, and a cap that rejects the bookkeeping row leaves
            # the burn invisible in GET /v1/tokens while the money was real.
            track(agent_id, "tokens", 0, provider, force=True, **fields)
        except Exception:
            pass
    return cents


def _pre_call_check(agent_id: str, model: str, payload: dict) -> JSONResponse | None:
    """The property this whole feature exists for. Returns a 402 response to
    hand straight back to the caller when the call must not go out, else None."""
    from ledger_engine import get_budget, _month_spend, _today_spend, _log_alert_daily
    estimate = proxy_core.call_estimate_cents(model, payload)
    budget = get_budget(agent_id)
    if not budget:
        return None
    if estimate is None:
        return None  # unpriced: pass through, _meter() alerts afterwards
    checks = []
    if budget.monthly_cap_cents > 0:
        checks.append(("monthly", budget.monthly_cap_cents, _month_spend(agent_id)))
    if getattr(budget, "daily_cap_cents", 0) > 0:
        checks.append(("daily", budget.daily_cap_cents, _today_spend(agent_id)))
    for label, cap, spent in checks:
        if spent + estimate > cap:
            try:
                metrics.record_event("proxy_blocked")
            except Exception:
                pass
            try:
                _log_alert_daily(
                    agent_id, "budget_blocked",
                    f"proxy blocked a call to '{model}': est. {estimate}c would put "
                    f"{agent_id} over its {label} cap ({spent}c of {cap}c). "
                    f"The provider was never contacted.")
            except Exception:
                pass
            return _err(402,
                        f"blocked before the provider: this call's estimated maximum cost "
                        f"({estimate} cents) would put {agent_id} over its {label} budget "
                        f"({spent} of {cap} cents). Nothing was sent upstream.",
                        "budget_exceeded")
    return None


@router.get("/v1/pricing")
def pricing():
    """The price table in use, with provenance. Open read: it is public
    information and an agent deciding whether to route through the proxy needs
    it. Unverified entries are placeholders — see the _note."""
    return proxy_core.price_provenance()


@router.post("/proxy/{provider}/{path:path}")
async def proxy_call(provider: str, path: str, request: Request):
    """Forward one provider call, metering it and blocking it if the budget
    says so. See the module docstring for the sequence."""
    cfg = proxy_core.provider_config(provider)
    if not cfg:
        return _err(404, f"unknown provider '{provider}' — supported: "
                         f"{proxy_core.provider_ids()} (add one in providers.json; "
                         f"no code change needed)", "unknown_provider")

    agent_id = _proxy_identity(request)

    if proxy_core.missing_credential(provider, request.headers):
        return _err(400,
                    f"your provider credential must be sent as '{cfg['credential_header']}' — "
                    f"AgentLedger forwards it and never stores it",
                    "provider_credential_missing")

    raw = await request.body()
    try:
        payload = json.loads(raw) if raw else {}
    except Exception:
        return _err(422, "request body must be JSON", "invalid_json")

    model = payload.get("model") or ""
    _t0 = time.time()
    blocked = _pre_call_check(agent_id, model, payload)
    try:
        metrics.record_latency("pre_call_check", (time.time() - _t0) * 1000)
    except Exception:
        pass
    if blocked is not None:
        return blocked

    # OpenAI only reports usage on streams when asked; requesting it is the
    # difference between metering a streamed call and guessing at it.
    if payload.get("stream") and provider == "openai":
        options = payload.get("stream_options") or {}
        if not options.get("include_usage"):
            options["include_usage"] = True
            payload["stream_options"] = options
            raw = json.dumps(payload).encode()

    url = proxy_core.upstream_url(provider, path)
    headers = proxy_core.forward_headers(request.headers, provider)
    try:
        metrics.record_event("proxy_call")
    except Exception:
        pass

    if payload.get("stream"):
        return StreamingResponse(
            _stream_upstream(url, headers, raw, agent_id, provider, model),
            media_type="text/event-stream")

    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
            upstream = await client.post(url, content=raw, headers=headers)
    except httpx.HTTPError as exc:
        return _err(502, f"upstream {provider} unreachable: {type(exc).__name__}",
                    "upstream_unreachable")

    out_headers = {k: v for k, v in upstream.headers.items()
                   if k.lower() not in RESPONSE_HEADER_BLOCKLIST}
    body = upstream.content
    if upstream.headers.get("content-type", "").startswith("application/json"):
        try:
            data = json.loads(body)
            tokens = proxy_core.usage_tokens(provider, data)
            if tokens:
                _meter(agent_id, provider, model, **tokens)
            else:
                _unmetered(agent_id, provider, model, "response carried no usage block")
        except Exception:
            _unmetered(agent_id, provider, model, "response was not parseable JSON")
    return Response(content=body, status_code=upstream.status_code, headers=out_headers)


async def _stream_upstream(url, headers, raw, agent_id, provider, model):
    """Pass a stream through byte-for-byte while watching for the usage block.

    Chunk boundaries do not respect line boundaries, so partial lines are
    buffered. If no usage ever arrives the call is recorded as UNMETERED —
    never as costing zero.

    Usage is MERGED across events, not overwritten: providers split it, and the
    event carrying the final output count carries no input at all.
    """
    captured = None
    buffer = ""
    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
            async with client.stream("POST", url, content=raw, headers=headers) as upstream:
                async for chunk in upstream.aiter_bytes():
                    buffer += chunk.decode("utf-8", "ignore")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if line.startswith("data:"):
                            try:
                                data = json.loads(line[5:].strip())
                                tokens = proxy_core.usage_tokens(provider, data)
                                if tokens:
                                    # MERGE, never assign: message_delta carries
                                    # only the output count, so assigning wiped
                                    # the input+cache totals from message_start.
                                    captured = proxy_core.merge_usage(captured, tokens)
                            except Exception:
                                pass
                    yield chunk
    except httpx.HTTPError as exc:
        yield f"data: {json.dumps({'error': {'type': 'upstream_error', 'message': str(exc)[:200]}})}\n\n".encode()
    if captured:
        _meter(agent_id, provider, model, **captured)
    else:
        _unmetered(agent_id, provider, model,
                   "stream ended without a usage block (client may not have requested one)")


def _unmetered(agent_id: str, provider: str, model: str, why: str) -> None:
    """A call we could not price must be visible. Silence here is how an agent
    spends real money and the ledger shows nothing."""
    try:
        from ledger_engine import _log_alert_daily
        _log_alert_daily(agent_id, "unmetered_proxy_call",
                         f"a proxied call to '{model}' ({provider}) completed but could "
                         f"not be metered: {why}. Its cost is missing from your totals.")
    except Exception:
        pass
