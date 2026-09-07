#!/usr/bin/env python3
"""AgentLedger MCP server — hosted streamable-http mode for Railway.

TDQS-optimized tool definitions: full descriptions, parameter docs, and
behavioral annotations for registry scoring and agent routing.
"""
import os
from fastmcp import FastMCP

mcp = FastMCP("agent-ledger")


@mcp.tool(annotations={"title": "Track Agent Spend", "readOnlyHint": False,
                        "destructiveHint": False, "idempotentHint": False})
def ledger_track(agent_id: str, rail: str, amount_cents: int, service: str) -> dict:
    """Record a spend entry for an AI agent on any payment rail.

    Every call appends to the agent's ledger and re-checks budget state.
    Use after every paid agent action to build the audit trail. Returns the
    persisted entry with timestamp.

    Args:
        agent_id: unique agent identifier (e.g. "research-agent-v2")
        rail: payment rail used — one of "mpp", "x402", "api_key", "manual"
        amount_cents: spend amount in cents (100 = $1.00)
        service: what was purchased (e.g. "search_query", "data_export")
    """
    from ledger_engine import track
    entry = track(agent_id, rail, amount_cents, service)
    return entry.to_dict()


@mcp.tool(annotations={"title": "Set Agent Budget", "readOnlyHint": False,
                        "destructiveHint": True, "idempotentHint": True})
def ledger_set_budget(agent_id: str, monthly_cents: int, daily_cents: int = 0) -> dict:
    """Set spending caps for an agent. Warns at 80%, blocks spend when exceeded.

    Monthly cap is required; daily cap is optional (0 = no daily limit).
    Overwrites any existing budget for the agent. Subsequent ledger_track
    calls enforce these caps. Destructive in that it replaces prior budget state.

    Args:
        agent_id: unique agent identifier
        monthly_cents: monthly spending cap in cents
        daily_cents: daily spending cap in cents (0 = no daily cap)
    """
    from ledger_engine import set_budget
    b = set_budget(agent_id, monthly_cents, daily_cents)
    return b.to_dict()


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
    from ledger_engine import _alerts_path
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
def ledger_list_agents() -> dict:
    """List every agent with tracked spend in this ledger. Read-only."""
    from ledger_engine import list_agents
    return {"agents": list_agents()}


def get_asgi_app():
    """Return the streamable-http ASGI app for mounting into api_server."""
    return mcp.http_app(path="/", transport="streamable-http")


if __name__ == "__main__":
    mcp.run(transport="http")