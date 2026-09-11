#!/usr/bin/env python3
"""AgentLedger MCP server — hosted streamable-http mode for Railway.

TDQS-optimized tool definitions: full descriptions, parameter docs, and
behavioral annotations for registry scoring and agent routing.
"""
import os
from fastmcp import FastMCP
import metrics

mcp = FastMCP("agent-ledger")


def _record_mcp_call():
    try:
        metrics.record_event("mcp_call")
    except Exception:
        pass


def _claim_or_error(agent_id: str, agent_secret: str, workspace_key: str = ""):
    """Returns (secret, created, error_dict_or_None).

    The error dict mirrors the REST typed-error codes from
    routes_agents._claim_or_401 so an agent gets the same machine-readable
    reason on either surface — MCP tools can't raise HTTP status codes, so
    the code travels in the payload instead. A missing workspace_key on a
    NEW agent_id is a typed `workspace_key_required` error here, not an
    unhandled exception (which surfaced as a 500 before this fix).
    """
    from ledger_engine import (ensure_agent_secret, AuthError, BetaCapExceededError,
                               WorkspaceKeyRequiredError)
    try:
        secret, created = ensure_agent_secret(
            agent_id, agent_secret or None, workspace_key=workspace_key or None)
        return secret, created, None
    except AuthError as e:
        return None, None, {"error": str(e), "error_code": "agent_secret_mismatch"}
    except BetaCapExceededError as e:
        return None, None, {"error": str(e), "error_code": "beta_cap_exceeded"}
    except WorkspaceKeyRequiredError as e:
        return None, None, {"error": str(e), "error_code": "workspace_key_required"}


@mcp.tool(annotations={"title": "Track Agent Spend", "readOnlyHint": False,
                        "destructiveHint": False, "idempotentHint": False})
def ledger_track(agent_id: str, rail: str, amount_cents: int, service: str,
                 tokens_in: int = 0, tokens_out: int = 0, model: str = "",
                 agent_secret: str = "", workspace_key: str = "") -> dict:
    """Record a spend entry for an AI agent on any payment rail, with optional token counts.

    Claiming a brand-new agent_id requires your workspace_key (get one at
    /login, or via x402 at POST /v1/billing/x402). That first call mints an
    agent_secret and returns it in the response — save it, every later call
    for that same agent_id must pass it back (no workspace_key needed again)
    or the write is rejected. Amounts are capped
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
        workspace_key: required when claiming a brand-new agent_id; not
                       needed once the agent_id has been claimed
    """
    from ledger_engine import track, ValidationError, BudgetExceededError
    secret, created, err = _claim_or_error(agent_id, agent_secret, workspace_key)
    if err:
        return err
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
    _record_mcp_call()
    return result


@mcp.tool(annotations={"title": "Set Agent Budget", "readOnlyHint": False,
                        "destructiveHint": True, "idempotentHint": True})
def ledger_set_budget(agent_id: str, monthly_cents: int, daily_cents: int = 0,
                      monthly_tokens: int = 0, daily_tokens: int = 0,
                      agent_secret: str = "", workspace_key: str = "") -> dict:
    """Set spending caps for an agent. Warns at 80%, blocks spend when exceeded
    — enforced: a ledger_track call that would cross the cap is rejected.

    Dollar caps (monthly_cents/daily_cents) and token caps (monthly_tokens/
    daily_tokens) are independent dimensions: dollar caps only cover
    non-"tokens" rails, token caps only cover rail="tokens" bookkeeping rows
    (tokens_in/tokens_out). Set both if the agent uses both.

    Monthly cap is required; the rest are optional (0 = no limit).
    Overwrites any existing budget for the agent. Claiming a brand-new
    agent_id requires your workspace_key; that first call mints an
    agent_secret (returned once — save it); later calls for that agent_id
    must pass the agent_secret back (no workspace_key needed again).

    Args:
        agent_id: unique agent identifier
        monthly_cents: monthly spending cap in cents
        daily_cents: daily spending cap in cents (0 = no daily cap)
        monthly_tokens: monthly token-burn cap (0 = no cap)
        daily_tokens: daily token-burn cap (0 = no cap)
        agent_secret: required for every call after the first for this agent_id
        workspace_key: required when claiming a brand-new agent_id; not
                       needed once the agent_id has been claimed
    """
    from ledger_engine import set_budget, ValidationError
    secret, created, err = _claim_or_error(agent_id, agent_secret, workspace_key)
    if err:
        return err
    try:
        b = set_budget(agent_id, monthly_cents, daily_cents,
                       monthly_tokens=monthly_tokens, daily_tokens=daily_tokens)
    except ValidationError as e:
        return {"error": str(e)}
    result = b.to_dict()
    if created:
        result["agent_secret"] = secret
        result["_note"] = ("Save this agent_secret — required for every future write "
                            "to this agent_id (track/budget). It will not be shown again.")
    _record_mcp_call()
    return result


def _authorize_read_or_error(agent_id: str, agent_secret: str, workspace_key: str):
    """MCP-side mirror of routes_agents._authorize_agent_read. Returns a
    typed error dict if neither credential authorizes, else None.

    Reads on the REST side are credential-gated (spec 3c); leaving the MCP
    reads open would be a straight bypass of that gate — an agent refused
    by GET /v1/report could read the same data through ledger_report with
    no credential at all. Same identity.authorize_agent_access call, so
    there is still exactly one place either credential is decided.
    """
    import identity
    if identity.authorize_agent_access(agent_id,
                                       agent_secret=agent_secret or None,
                                       workspace_key=workspace_key or None):
        return None
    return {"error": "this agent's data requires its agent_secret or workspace_key",
            "error_code": "agent_secret_mismatch"}


@mcp.tool(annotations={"title": "Agent Spend Report", "readOnlyHint": True,
                        "destructiveHint": False, "idempotentHint": True})
def ledger_report(agent_id: str, days: int = 30, agent_secret: str = "",
                  workspace_key: str = "") -> dict:
    """Spend report for an agent over a rolling window.

    Returns total spend, breakdown by rail and by service, budget status
    (ok/warning/exceeded), detected anomalies, and entry count.

    Requires a credential: either the agent's own agent_secret or its
    workspace's workspace_key (same rule as GET /v1/report).

    Args:
        agent_id: unique agent identifier
        days: report window in days (default 30)
        agent_secret: the agent's own secret (either this or workspace_key)
        workspace_key: the owning workspace's key (either this or agent_secret)
    """
    from ledger_engine import report
    err = _authorize_read_or_error(agent_id, agent_secret, workspace_key)
    if err:
        return err
    r = report(agent_id, days)
    _record_mcp_call()
    return {"agent_id": r.agent_id, "period": r.period,
            "total_spend_cents": r.total_spend_cents, "by_rail": r.by_rail,
            "by_service": r.by_service, "budget_status": r.budget_status,
            "anomalies": r.anomalies, "entry_count": r.entry_count}


@mcp.tool(annotations={"title": "Agent Budget Alerts", "readOnlyHint": True,
                        "destructiveHint": False, "idempotentHint": True})
def ledger_alerts(agent_id: str, agent_secret: str = "",
                  workspace_key: str = "") -> dict:
    """Alert history for an agent: budget warnings (80% threshold) and spending spikes.

    Requires a credential: either the agent's own agent_secret or its
    workspace's workspace_key (same rule as GET /v1/alerts).

    Args:
        agent_id: unique agent identifier
        agent_secret: the agent's own secret (either this or workspace_key)
        workspace_key: the owning workspace's key (either this or agent_secret)
    """
    import json as _json
    from ledger_engine import _alerts_path, validate_agent_id, ValidationError
    try:
        validate_agent_id(agent_id)  # REST/MCP parity — same guard as GET /v1/alerts
    except ValidationError as e:
        return {"error": str(e), "error_code": "invalid_agent_id"}
    err = _authorize_read_or_error(agent_id, agent_secret, workspace_key)
    if err:
        return err
    p = _alerts_path(agent_id)
    if not p.exists():
        return {"alerts": []}
    alerts = []
    for line in p.read_text().splitlines():
        try:
            alerts.append(_json.loads(line))
        except Exception:
            continue
    _record_mcp_call()
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
    _record_mcp_call()
    return {"agents": list_agents()}


@mcp.tool(annotations={"title": "AgentLedger API Docs", "readOnlyHint": True,
                        "destructiveHint": False, "idempotentHint": True})
def ledger_api_docs(topic: str = "") -> dict:
    """Self-serve documentation for AgentLedger — quickstart, MCP tools, REST
    endpoints, budget caps, error codes, and idempotency usage, as markdown.

    Args:
        topic: "quickstart" | "mcp" | "rest" | "budget" | "errors" | "idempotency" | "all"
               (default "" == "all"). Unknown topics fall back to the full docs.
    """
    from docs_content import get_api_docs, _TOPIC_ORDER
    requested = (topic or "all").strip().lower()
    md = get_api_docs(requested)
    try:
        metrics.record_event("meta_doc_call", tool="ledger_api_docs", topic=requested)
    except Exception:
        pass
    _record_mcp_call()
    return {"topic": requested if requested in _TOPIC_ORDER or requested == "all" else "all",
            "markdown": md}


@mcp.tool(annotations={"title": "AgentLedger Recipes", "readOnlyHint": True,
                        "destructiveHint": False, "idempotentHint": True})
def ledger_examples(pattern: str) -> dict:
    """Complete, runnable Python recipe for a common AgentLedger integration
    pattern.

    Args:
        pattern: "python_tracking" | "budget_enforcement" | "weekly_report" |
                 "retry_safe_writes"
    """
    from docs_content import get_example
    requested = (pattern or "").strip().lower()
    code = get_example(requested)
    try:
        metrics.record_event("meta_doc_call", tool="ledger_examples", topic=requested)
    except Exception:
        pass
    _record_mcp_call()
    return {"pattern": requested, "code": code}


def get_asgi_app():
    """Return the streamable-http ASGI app for mounting into api_server."""
    return mcp.http_app(path="/", transport="streamable-http")


if __name__ == "__main__":
    mcp.run(transport="http")