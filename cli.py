#!/usr/bin/env python3
"""AgentLedger CLI — per-agent spend management.

Two modes, always stated explicitly so this can never silently show stale
or wrong data again:
  - local (default): reads/writes AGENT_LEDGER_DATA directly via ledger_engine.
    Only meaningful if you're running against the same data dir as a local
    `railway run` / dev server — NOT the production API.
  - remote: set AGENT_LEDGER_API_BASE (or pass --api-base) to hit a real
    deployed instance over HTTP instead. This is what you want when checking
    the actual production ledger.
"""
import argparse, json, os, sys, urllib.request, urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def _api_base(args) -> str | None:
    return getattr(args, "api_base", None) or os.environ.get("AGENT_LEDGER_API_BASE")


def _remote_request(base: str, method: str, path: str, body: dict | None = None) -> dict:
    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", "AL-API-Version": "2026-09-01",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)


def _mode_banner(args):
    base = _api_base(args)
    if base:
        print(f"[remote: {base}]", file=sys.stderr)
    else:
        data_dir = os.environ.get("AGENT_LEDGER_DATA", os.path.expanduser("~/.agent-ledger"))
        print(f"[local: {data_dir} — NOT production unless this is your prod data dir]", file=sys.stderr)
    return base


def cmd_track(args):
    base = _mode_banner(args)
    if base:
        if not args.agent_secret:
            print("--agent-secret required for a remote track write "
                  "(omit only for a NEW agent_id, which mints one)", file=sys.stderr)
        body = {"agent_id": args.agent_id, "rail": args.rail,
                 "amount_cents": args.amount_cents, "service": args.service}
        if args.agent_secret:
            body["agent_secret"] = args.agent_secret
        print(json.dumps(_remote_request(base, "POST", "/v1/track", body), indent=2))
        return
    from ledger_engine import track
    entry = track(args.agent_id, args.rail, args.amount_cents, args.service)
    print(json.dumps(entry.to_dict(), indent=2))


def cmd_budget(args):
    base = _mode_banner(args)
    if base:
        if not args.agent_secret:
            print("--agent-secret required for a remote budget write", file=sys.stderr)
            sys.exit(1)
        body = {"agent_id": args.agent_id, "agent_secret": args.agent_secret,
                 "monthly_cents": args.monthly_cents, "daily_cents": args.daily_cents,
                 "monthly_tokens": args.monthly_tokens, "daily_tokens": args.daily_tokens}
        print(json.dumps(_remote_request(base, "POST", "/v1/budget", body), indent=2))
        return
    from ledger_engine import set_budget
    b = set_budget(args.agent_id, args.monthly_cents, args.daily_cents,
                   monthly_tokens=args.monthly_tokens, daily_tokens=args.daily_tokens)
    print(json.dumps(b.to_dict(), indent=2))


def cmd_report(args):
    base = _mode_banner(args)
    if base:
        r = _remote_request(base, "GET", f"/v1/report/{args.agent_id}?days={args.days}")
        print(json.dumps(r, indent=2))
        return
    from ledger_engine import report
    r = report(args.agent_id, args.days)
    print(json.dumps({"agent_id": r.agent_id, "period": r.period,
        "total_spend_usd": r.total_spend_cents / 100, "by_rail": r.by_rail,
        "by_service": r.by_service, "budget_status": r.budget_status,
        "anomalies": r.anomalies}, indent=2))


def cmd_alerts(args):
    base = _mode_banner(args)
    if base:
        r = _remote_request(base, "GET", f"/v1/alerts/{args.agent_id}")
        if not r["count"]:
            print("No alerts.")
            return
        for a in r["alerts"]:
            print(f"  [{a['type']}] {a['message']}")
        return
    alerts_path = os.path.join(os.environ.get("AGENT_LEDGER_DATA", os.path.expanduser("~/.agent-ledger")),
                               "agents", args.agent_id, "alerts.jsonl")
    if not os.path.exists(alerts_path):
        print("No alerts.")
        return
    for line in open(alerts_path):
        a = json.loads(line)
        print(f"  [{a['type']}] {a['message']}")


def cmd_list(args):
    base = _mode_banner(args)
    if base:
        print("Remote mode has no open 'list all agents' endpoint (owner-only, "
              "needs x-al-admin — use the /v1/dashboard page instead).", file=sys.stderr)
        sys.exit(1)
    from ledger_engine import list_agents
    agents = list_agents()
    if not agents:
        print("No agents tracked.")
        return
    for a in agents:
        print(f"  {a['agent_id']:30s} {a['entries']:4d} entries  ${a['total_spend_cents']/100:.2f}")


def main():
    p = argparse.ArgumentParser(prog="agentledger", description="Per-agent spend management")
    p.add_argument("--api-base", help="query a remote AgentLedger instance instead of local data "
                                        "(or set AGENT_LEDGER_API_BASE)")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("track", help="record a spend entry")
    t.add_argument("--agent-id", required=True)
    t.add_argument("--rail", required=True, help="mpp|x402|api_key|manual")
    t.add_argument("--amount-cents", type=int, required=True)
    t.add_argument("--service", required=True)
    t.add_argument("--agent-secret", help="required for remote writes to an already-claimed agent_id")
    t.set_defaults(fn=cmd_track)

    b = sub.add_parser("set-budget", help="set budget caps")
    b.add_argument("--agent-id", required=True)
    b.add_argument("--monthly-cents", type=int, required=True)
    b.add_argument("--daily-cents", type=int, default=0)
    b.add_argument("--monthly-tokens", type=int, default=0, help="monthly token-burn cap (rail=tokens agents)")
    b.add_argument("--daily-tokens", type=int, default=0, help="daily token-burn cap (rail=tokens agents)")
    b.add_argument("--agent-secret", help="required for remote writes")
    b.set_defaults(fn=cmd_budget)

    r = sub.add_parser("report", help="generate spend report")
    r.add_argument("--agent-id", required=True)
    r.add_argument("--days", type=int, default=30)
    r.set_defaults(fn=cmd_report)

    a = sub.add_parser("alerts", help="show alerts")
    a.add_argument("--agent-id", required=True)
    a.set_defaults(fn=cmd_alerts)

    l = sub.add_parser("list", help="list tracked agents (local mode only)")
    l.set_defaults(fn=cmd_list)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
