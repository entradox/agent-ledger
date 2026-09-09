#!/usr/bin/env python3
"""AgentLedger MCP server — per-agent spend management as native tools."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from ledger_engine import track, set_budget, get_budget, report, list_agents
import metrics

from fastmcp import FastMCP
mcp = FastMCP("agent-ledger")


def _record_mcp_call():
    try:
        metrics.record_event("mcp_call")
    except Exception:
        pass

@mcp.tool()
def ledger_track(agent_id: str, rail: str, amount_cents: int, service: str) -> dict:
    """Record a spend entry for an agent.

    Args:
        agent_id: unique agent identifier
        rail: payment rail used ("mpp", "x402", "api_key", "manual")
        amount_cents: spend amount in cents (100 = $1.00)
        service: what was purchased (e.g. "search_query", "data_export")
    """
    entry = track(agent_id, rail, amount_cents, service)
    _record_mcp_call()
    return entry.to_dict()

@mcp.tool()
def ledger_set_budget(agent_id: str, monthly_cents: int, daily_cents: int = 0) -> dict:
    """Set an agent's budget caps.

    Args:
        agent_id: unique agent identifier
        monthly_cents: monthly spending cap in cents
        daily_cents: daily spending cap in cents (0 = no daily cap)
    """
    b = set_budget(agent_id, monthly_cents, daily_cents)
    _record_mcp_call()
    return b.to_dict()

@mcp.tool()
def ledger_report(agent_id: str, days: int = 30) -> dict:
    """Generate a spend report for an agent.

    Args:
        agent_id: unique agent identifier
        days: lookback period in days (default 30)
    """
    r = report(agent_id, days)
    _record_mcp_call()
    return {"agent_id": r.agent_id, "period": r.period,
            "total_spend_cents": r.total_spend_cents, "by_rail": r.by_rail,
            "by_service": r.by_service, "budget_status": r.budget_status,
            "anomalies": r.anomalies, "entry_count": r.entry_count}

@mcp.tool()
def ledger_alerts(agent_id: str) -> dict:
    """Show budget/spending alerts for an agent."""
    alerts_path = os.path.join(os.environ.get("AGENT_LEDGER_DATA", os.path.expanduser("~/.agent-ledger")),
                               "agents", agent_id, "alerts.jsonl")
    if not os.path.exists(alerts_path):
        return {"count": 0, "alerts": []}
    alerts = [json.loads(l) for l in open(alerts_path)]
    _record_mcp_call()
    return {"count": len(alerts), "alerts": alerts}

@mcp.tool()
def ledger_list_agents() -> dict:
    """List all tracked agents with spend summaries."""
    _record_mcp_call()
    return {"agents": list_agents()}


@mcp.tool()
def ledger_api_docs(topic: str = "") -> dict:
    """Self-serve documentation for AgentLedger — quickstart, MCP tools, REST
    endpoints, error codes, and idempotency usage, as markdown.

    Args:
        topic: "quickstart" | "mcp" | "rest" | "errors" | "idempotency" | "all"
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


@mcp.tool()
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


if __name__ == "__main__":
    mcp.run()