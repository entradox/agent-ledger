#!/usr/bin/env python3
"""AgentLedger CLI — per-agent spend management."""
import argparse, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from ledger_engine import track, set_budget, get_budget, report, list_agents

def cmd_track(args):
    entry = track(args.agent_id, args.rail, args.amount_cents, args.service)
    print(json.dumps(entry.to_dict(), indent=2))

def cmd_budget(args):
    b = set_budget(args.agent_id, args.monthly_cents, args.daily_cents)
    print(json.dumps(b.to_dict(), indent=2))

def cmd_report(args):
    r = report(args.agent_id, args.days)
    print(json.dumps({"agent_id": r.agent_id, "period": r.period,
        "total_spend_usd": r.total_spend_cents / 100, "by_rail": r.by_rail,
        "by_service": r.by_service, "budget_status": r.budget_status,
        "anomalies": r.anomalies}, indent=2))

def cmd_alerts(args):
    import os
    alerts_path = os.path.join(os.environ.get("AGENT_LEDGER_DATA", os.path.expanduser("~/.agent-ledger")),
                               "agents", args.agent_id, "alerts.jsonl")
    if not os.path.exists(alerts_path):
        print("No alerts.")
        return
    for line in open(alerts_path):
        a = json.loads(line)
        print(f"  [{a['type']}] {a['message']}")

def cmd_list(_):
    agents = list_agents()
    if not agents:
        print("No agents tracked.")
        return
    for a in agents:
        print(f"  {a['agent_id']:30s} {a['entries']:4d} entries  ${a['total_spend_cents']/100:.2f}")

def main():
    p = argparse.ArgumentParser(prog="agentledger", description="Per-agent spend management")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("track", help="record a spend entry")
    t.add_argument("--agent-id", required=True)
    t.add_argument("--rail", required=True, help="mpp|x402|api_key|manual")
    t.add_argument("--amount-cents", type=int, required=True)
    t.add_argument("--service", required=True)
    t.set_defaults(fn=cmd_track)

    b = sub.add_parser("set-budget", help="set budget caps")
    b.add_argument("--agent-id", required=True)
    b.add_argument("--monthly-cents", type=int, required=True)
    b.add_argument("--daily-cents", type=int, default=0)
    b.set_defaults(fn=cmd_budget)

    r = sub.add_parser("report", help="generate spend report")
    r.add_argument("--agent-id", required=True)
    r.add_argument("--days", type=int, default=30)
    r.set_defaults(fn=cmd_report)

    a = sub.add_parser("alerts", help="show alerts")
    a.add_argument("--agent-id", required=True)
    a.set_defaults(fn=cmd_alerts)

    l = sub.add_parser("list", help="list tracked agents")
    l.set_defaults(fn=cmd_list)

    args = p.parse_args()
    args.fn(args)

if __name__ == "__main__":
    main()