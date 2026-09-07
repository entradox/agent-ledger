#!/usr/bin/env python3
"""AgentLedger MCP server — hosted streamable-http mode for Railway.

Same tools as mcp_server.py (stdio variant), exposed as a hosted
streamable-http ASGI app that api_server.py mounts at /mcp.
This is what the Official MCP Registry 'remotes' field points at.
"""
import os
from fastmcp import FastMCP

mcp = FastMCP("agent-ledger")


@mcp.tool()
def ledger_track(agent_id: str, rail: str, amount_cents: int, service: str) -> dict:
    """Record a spend entry for an agent.

    Args:
        agent_id: unique agent identifier
        rail: payment rail used ("mpp", "x402", "api_key", "manual")
        amount_cents: spend amount in cents (100 = $1.00)
        service: what was purchased (e.g. "search_query", "data_export")
    """
    from ledger_engine import track
    entry = track(agent_id, rail, amount_cents, service)
    return entry.to_dict()


@mcp.tool()
def ledger_set_budget(agent_id: str, monthly_cents: int, daily_cents: int = 0) -> dict:
    """Set an agent's budget caps.

    Args:
        agent_id: unique agent identifier
        monthly_cents: monthly spending cap in cents
        daily_cents: daily spending cap in cents (0 = no daily cap)
    """
    from ledger_engine import set_budget
    b = set_budget(agent_id, monthly_cents, daily_cents)
    return b.to_dict()


@mcp.tool()
def ledger_report(agent_id: str, days: int = 30) -> dict:
    """Spend report for an agent: totals, by rail, by service, budget status, anomalies.

    Args:
        agent_id: unique agent identifier
        days: report window in days
    """
    from ledger_engine import report
    r = report(agent_id, days)
    return {"agent_id": r.agent_id, "period": r.period,
            "total_spend_cents": r.total_spend_cents, "by_rail": r.by_rail,
            "by_service": r.by_service, "budget_status": r.budget_status,
            "anomalies": r.anomalies, "entry_count": r.entry_count}


@mcp.tool()
def ledger_alerts(agent_id: str) -> dict:
    """Alerts for an agent (budget warnings, spending spikes)."""
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


@mcp.tool()
def ledger_list_agents() -> dict:
    """List all tracked agents."""
    from ledger_engine import list_agents
    return {"agents": list_agents()}


def get_asgi_app():
    """Return the streamable-http ASGI app for mounting into api_server."""
    return mcp.http_app(path="/", transport="streamable-http")


if __name__ == "__main__":
    mcp.run(transport="http")