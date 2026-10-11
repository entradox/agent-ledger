#!/usr/bin/env python3
"""AgentLedger MCP server — per-agent spend management as native tools.

STDIO/LOCAL ONLY. These tools carry NO authentication — that is correct for
a server the operator runs on their own machine over stdio, and it is exactly
why this module must NEVER be mounted over an HTTP transport: the hosted
authenticated MCP surface is al_mcp_http.py. Serving this file remotely would
expose unauthenticated cross-tenant writes and the full agent list.
"""
import json
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from ledger_engine import (track, set_budget, get_budget, report, list_agents,
                           validate_agent_id, ValidationError)
import metrics

from fastmcp import FastMCP
mcp = FastMCP("agent-ledger")

# This process IS the consumer — report its real identity so the event feed
# can detect a restart and re-serve unacked events (D-1625).
import time as _time
_PROC_STARTED = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())


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
def ledger_set_budget(agent_id: str, monthly_cents: int, daily_cents: int = 0,
                      monthly_tokens: int = 0, daily_tokens: int = 0) -> dict:
    """Set an agent's budget caps.

    Dollar caps (monthly_cents/daily_cents) only cover non-"tokens" rails;
    token caps (monthly_tokens/daily_tokens) only cover rail="tokens"
    bookkeeping rows. Independent dimensions — set both if the agent uses both.

    Args:
        agent_id: unique agent identifier
        monthly_cents: monthly spending cap in cents
        daily_cents: daily spending cap in cents (0 = no daily cap)
        monthly_tokens: monthly token-burn cap (0 = no cap)
        daily_tokens: daily token-burn cap (0 = no cap)
    """
    b = set_budget(agent_id, monthly_cents, daily_cents,
                   monthly_tokens=monthly_tokens, daily_tokens=daily_tokens)
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
    try:
        validate_agent_id(agent_id)  # rejects ../ — agent_id goes into a path
    except ValidationError as e:
        return {"error": str(e)}
    alerts_path = os.path.join(os.environ.get("AGENT_LEDGER_DATA", os.path.expanduser("~/.agent-ledger")),
                               "agents", agent_id, "alerts.jsonl")
    if not os.path.exists(alerts_path):
        return {"count": 0, "alerts": []}
    alerts = [json.loads(l) for l in open(alerts_path)]
    _record_mcp_call()
    return {"count": len(alerts), "alerts": alerts}


@mcp.tool()
def ledger_wait_events(agent_id: str, consumer_id: str = "local",
                       cursor: Optional[int] = None,
                       timeout: Optional[float] = None) -> dict:
    """Long-poll an agent's event feed — alerts as envelope-shaped events.

    Holds open up to `timeout` seconds (default 5, max 30). Returns events
    plus a `cursor` — pass it back once processed (that is the ack). Reuse
    one consumer_id; this process reports its own pid/started, so after a
    restart unacked events re-serve with redelivered:true.

    Args:
        agent_id: unique agent identifier
        consumer_id: durable consumer name (default "local")
        cursor: line offset already processed (omit to continue)
        timeout: seconds to hold open (default 5, max 30)
    """
    try:
        validate_agent_id(agent_id)
    except ValidationError as e:
        return {"error": str(e)}
    import event_delivery
    try:
        result = event_delivery.wait(agent_id, consumer_id=consumer_id,
                                     cursor=cursor, timeout=timeout,
                                     pid=os.getpid(),
                                     started=_PROC_STARTED,
                                     name="stdio-mcp")
    except event_delivery.ConsumerError as e:
        return {"error": str(e), "error_code": "invalid_consumer"}
    except event_delivery.WaitCongestedError as e:
        return {"error": str(e), "error_code": "wait_congested"}
    _record_mcp_call()
    return result


@mcp.tool()
def ledger_list_agents() -> dict:
    """List all tracked agents with spend summaries."""
    _record_mcp_call()
    return {"agents": list_agents()}


@mcp.tool()
def ledger_api_docs(topic: str = "") -> dict:
    """Self-serve documentation for AgentLedger — quickstart, MCP tools, REST
    endpoints, budget caps, error codes, and idempotency usage, as markdown.

    Args:
        topic: "quickstart" | "mcp" | "rest" | "budget" | "errors" | "idempotency" | "metering" | "events" | "all"
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