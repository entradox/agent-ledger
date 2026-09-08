#!/usr/bin/env python3
"""AgentLedger MCP server — hosted streamable-http mode for Railway.

TDQS-optimized tool definitions: full descriptions, parameter docs, and
behavioral annotations for registry scoring and agent routing.
"""
import os
from fastmcp import FastMCP

mcp = FastMCP("agent-ledger")


def _claim_or_error(agent_id: str, agent_secret: str):
    """Returns (secret, created, error_message_or_None)."""
    from ledger_engine import ensure_agent_secret, AuthError, BetaCapExceededError
    try:
        secret, created = ensure_agent_secret(agent_id, agent_secret or None)
        return secret, created, None
    except (AuthError, BetaCapExceededError) as e:
        return None, None, str(e)


@mcp.tool(annotations={"title": "Track Agent Spend", "readOnlyHint": False,
                        "destructiveHint": False, "idempotentHint": False})
def ledger_track(agent_id: str, rail: str, amount_cents: int, service: str,
                 tokens_in: int = 0, tokens_out: int = 0, model: str = "",
                 agent_secret: str = "") -> dict:
    """Record a spend entry for an AI agent on any payment rail, with optional token counts.

    No signup: the first call for a new agent_id mints an agent_secret and
    returns it in the response — save it, every later call for that same
    agent_id must pass it back or the write is rejected. Amounts are capped
    at $100,000/entry and must be >= 0. If a budget is set for this agent,
    an entry that would cross the monthly/daily cap is blocked, not just
    logged. Include tokens_in/tokens_out + model on every LLM call so token
    burn shows up in the /v1/tokens report.

    Args:
        agent_id: unique agent identifier (e.g. "research-agent-v2")
        rail: payment rail used — one of "mpp", "x402", "api_key", "manual"
        amount_cents: spend amount in cents (100 = $1.00), 0-10000000
        service: what was purchased (e.g. "search_query", "data_export")
        tokens_in: prompt tokens consumed (0 if unknown)
        tokens_out: completion tokens consumed (0 if unknown)
        model: model name (e.g. "gpt-4o") — token burn is reported per model
        agent_secret: required for every call after the first for this agent_id
    """
    from ledger_engine import track, ValidationError, BudgetExceededError
    secret, created, err = _claim_or_error(agent_id, agent_secret)
    if err:
        return {"error": err}
    meta = {}
    if tokens_in or tokens_out:
        meta = {"tokens_in": tokens_in, "tokens_out": tokens_out}
        if model:
            meta["model"] = model
    try:
        entry = track(agent_id, rail, amount_cents, service, **meta)
    except (ValidationError, BudgetExceededError) as e:
        return {"error": str(e)}
    result = entry.to_dict()
    if created:
        result["agent_secret"] = secret
        result["_note"] = ("Save this agent_secret — required for every future write "
                            "to this agent_id (track/budget). It will not be shown again.")
    return result


@mcp.tool(annotations={"title": "Set Agent Budget", "readOnlyHint": False,
                        "destructiveHint": True, "idempotentHint": True})
def ledger_set_budget(agent_id: str, monthly_cents: int, daily_cents: int = 0,
                      agent_secret: str = "") -> dict:
    """Set spending caps for an agent. Warns at 80%, blocks spend when exceeded
    — enforced: a ledger_track call that would cross the cap is rejected.

    Monthly cap is required; daily cap is optional (0 = no daily limit).
    Overwrites any existing budget for the agent. No signup: the first call
    for a new agent_id mints an agent_secret (returned once — save it); later
    calls for that agent_id must pass it back.

    Args:
        agent_id: unique agent identifier
        monthly_cents: monthly spending cap in cents
        daily_cents: daily spending cap in cents (0 = no daily cap)
        agent_secret: required for every call after the first for this agent_id
    """
    from ledger_engine import set_budget, ValidationError
    secret, created, err = _claim_or_error(agent_id, agent_secret)
    if err:
        return {"error": err}
    try:
        b = set_budget(agent_id, monthly_cents, daily_cents)
    except ValidationError as e:
        return {"error": str(e)}
    result = b.to_dict()
    if created:
        result["agent_secret"] = secret
        result["_note"] = ("Save this agent_secret — required for every future write "
                            "to this agent_id (track/budget). It will not be shown again.")
    return result


@mcp.tool(annotations={"title": "Agent Spend Report", "readOnlyHint": True,
                        "destructiveHint": False, "idempotentHint": True})
def ledger_report(agent_id: str, days: int = 30) -> dict:
    """Spend report for an agent over a rolling window.

    Returns total spend, breakdown by rail and by service, budget status
    (ok/warning/exceeded), detected anomalies, and entry count.

    Args:
        agent_id: unique agent identifier
        days: report window in days (default 30)
    """
    from ledger_engine import report
    r = report(agent_id, days)
    return {"agent_id": r.agent_id, "period": r.period,
            "total_spend_cents": r.total_spend_cents, "by_rail": r.by_rail,
            "by_service": r.by_service, "budget_status": r.budget_status,
            "anomalies": r.anomalies, "entry_count": r.entry_count}


@mcp.tool(annotations={"title": "Agent Budget Alerts", "readOnlyHint": True,
                        "destructiveHint": False, "idempotentHint": True})
def ledger_alerts(agent_id: str) -> dict:
    """Alert history for an agent: budget warnings (80% threshold) and spending spikes."""
    import json as _json
    from ledger_engine import _alerts_path, validate_agent_id, ValidationError
    try:
        validate_agent_id(agent_id)  # REST/MCP parity — same guard as GET /v1/alerts
    except ValidationError as e:
        return {"error": str(e)}
    p = _alerts_path(agent_id)
    if not p.exists():
        return {"alerts": []}
    alerts = []
    for line in p.read_text().splitlines():
        try:
            alerts.append(_json.loads(line))
        except Exception:
            continue
    return {"alerts": alerts}


@mcp.tool(annotations={"title": "List Tracked Agents", "readOnlyHint": True,
                        "destructiveHint": False, "idempotentHint": True})
def ledger_list_agents(admin_secret: str = "") -> dict:
    """Owner-only: full cross-tenant listing of every agent ever claimed on
    this instance, with totals. Requires the operator's admin_secret — this
    is a portfolio-wide view, not a per-agent report (use ledger_report for
    that, which needs no secret).

    Args:
        admin_secret: operator admin secret (not the same as an agent_secret)
    """
    import os as _os
    real_admin = _os.environ.get("AL_ADMIN_SECRET", "")
    if not real_admin or admin_secret != real_admin:
        return {"error": "owner only"}
    from ledger_engine import list_agents
    return {"agents": list_agents()}


def get_asgi_app():
    """Return the streamable-http ASGI app for mounting into api_server."""
    return mcp.http_app(path="/", transport="streamable-http")


if __name__ == "__main__":
    mcp.run(transport="http")