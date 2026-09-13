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


def _remote_request(base: str, method: str, path: str, body: dict | None = None,
                    headers: dict | None = None, raw: bool = False):
    """One HTTP call to a deployed instance.

    headers: some routes authenticate by header rather than body
    (X-Agent-Secret / X-Workspace-Key on the read, share and webhook routes).
    raw: return the response text as-is, for the HTML pages.
    """
    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json", "AL-API-Version": "2026-09-01"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode()
            return text if raw else json.loads(text)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()[:400]}", file=sys.stderr)
        sys.exit(1)


DEFAULT_BASE = "https://aiagentscity.com"


def _required_base(args) -> str:
    """init/share talk to a real instance, so local mode is not an option."""
    return (_api_base(args) or os.environ.get("AGENT_LEDGER_API_BASE") or DEFAULT_BASE).rstrip("/")


def cmd_init(args):
    """Zero to a metered, guarded agent in one command."""
    import re
    base = _required_base(args)
    print(f"[init against {base}]", file=sys.stderr)

    # 1. A workspace. POST /start mints one and shows its key once; GET must
    #    never mint, or a crawler would eat the launch window.
    page = _remote_request(base, "POST", "/start", raw=True)
    m = re.search(r"wk_live_[A-Za-z0-9_\-]+", page)
    if not m:
        print("could not find a workspace_key in the /start response — this "
              "instance may need an upgrade or the page changed shape",
              file=sys.stderr)
        sys.exit(1)
    workspace_key = m.group(0)

    # 2. Claim an agent_id with a zero-amount first write, which mints its
    #    secret. Re-using an existing agent_id needs the secret instead.
    body = {"agent_id": args.agent, "rail": "manual", "amount_cents": 0,
            "service": "agent-ledger-init", "workspace_key": workspace_key}
    if args.agent_secret:
        body.pop("workspace_key")
        body["agent_secret"] = args.agent_secret
    resp = _remote_request(base, "POST", "/v1/track", body)
    agent_secret = resp.get("agent_secret")
    if not agent_secret:
        print("the agent was claimed already and no --agent-secret was given. "
              "Re-run with --agent-secret, or use a new --agent name.", file=sys.stderr)
        sys.exit(1)

    env_lines = [
        "# AgentLedger — created by `agent-ledger init`",
        f"AGENT_LEDGER_API_BASE={base}",
        f"AGENT_LEDGER_WORKSPACE_KEY={workspace_key}",
        f"AGENT_LEDGER_AGENT_ID={args.agent}",
        f"AGENT_LEDGER_AGENT_SECRET={agent_secret}",
        "",
    ]
    if args.env_file:
        path = args.env_file
        existing = ""
        if os.path.exists(path):
            existing = open(path).read()
            if not existing.endswith("\n"):
                existing += "\n"
        with open(path, "w") as f:
            f.write(existing + "\n".join(env_lines))
        print(f"wrote {path}")

    print(f"""
workspace_key and agent_secret are shown ONCE — save them.

    agent  {args.agent}
    secret {agent_secret}
    key    {workspace_key}

Now point a client at the proxy (that is what makes the cap stop money):

    from openai import OpenAI
    import agentledger
    client = agentledger.wrap(OpenAI(api_key=OPENAI_KEY),
                              agent_id="{args.agent}", agent_secret="<secret>")

Or by hand, if you would rather not add a package:

    base_url = "{base}/proxy/openai/v1/"
    default_headers = {{"X-AL-Agent": "{args.agent}", "X-AL-Secret": "<secret>"}}

Then set a cap, and watch it block: 

    agent-ledger --api-base {base} set-budget --agent-id {args.agent} \
        --agent-secret <secret> --monthly-cents 5000
""")


def cmd_share(args):
    """Mint a browser-openable, read-only link to an agent's report."""
    base = _required_base(args)
    headers = {}
    if args.agent_secret:
        headers["X-Agent-Secret"] = args.agent_secret
    if args.workspace_key:
        headers["X-Workspace-Key"] = args.workspace_key
    if not headers:
        print("a read credential is required: pass --agent-secret or --workspace-key",
              file=sys.stderr)
        sys.exit(1)
    r = _remote_request(base, "POST",
                        f"/v1/report/{args.agent_id}/share?ttl_days={args.ttl_days}",
                        headers=headers)
    print(r["url"])
    print(f"(read-only, expires in {r['ttl_days']} days)", file=sys.stderr)


def _engine_or_explain():
    """The client package ships the CLI, not the engine.

    Called from _mode_banner, which is the single place a command discovers it
    is about to run in local mode — putting it in each command is how one of
    them ends up without it and crashes with a ModuleNotFoundError instead of
    saying what to do.
    """
    try:
        import ledger_engine  # noqa: F401
    except ImportError:
        print("local mode needs the ledger engine, which ships with the service, "
              "not with the client package. Point at a deployed instance instead: "
              "--api-base https://aiagentscity.com "
              "(or set AGENT_LEDGER_API_BASE).", file=sys.stderr)
        sys.exit(1)


def _mode_banner(args):
    base = _api_base(args)
    if base:
        print(f"[remote: {base}]", file=sys.stderr)
    else:
        _engine_or_explain()
        data_dir = os.environ.get("AGENT_LEDGER_DATA", os.path.expanduser("~/.agent-ledger"))
        print(f"[local: {data_dir} — NOT production unless this is your prod data dir]", file=sys.stderr)
    return base


def _local_claim(args):
    """Local mode has always written straight through ledger_engine with no
    claim step (it's the trusted operator path). If --workspace-key IS given,
    honour it: claim/verify the agent_id into that workspace first so a local
    claim binds a workspace_id the same way a remote one does. No key given =
    unchanged legacy behavior."""
    workspace_key = getattr(args, "workspace_key", None)
    if not workspace_key:
        return
    from ledger_engine import ensure_agent_secret
    secret, created = ensure_agent_secret(
        args.agent_id, getattr(args, "agent_secret", None) or None,
        workspace_key=workspace_key)
    if created:
        print(f"[claimed {args.agent_id} — agent_secret: {secret}]", file=sys.stderr)


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
        if args.workspace_key:
            body["workspace_key"] = args.workspace_key
        print(json.dumps(_remote_request(base, "POST", "/v1/track", body), indent=2))
        return
    from ledger_engine import track
    _local_claim(args)
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
        if args.workspace_key:
            body["workspace_key"] = args.workspace_key
        print(json.dumps(_remote_request(base, "POST", "/v1/budget", body), indent=2))
        return
    from ledger_engine import set_budget
    _local_claim(args)
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
    t.add_argument("--workspace-key", help="required when claiming a brand-new agent_id")
    t.set_defaults(fn=cmd_track)

    b = sub.add_parser("set-budget", help="set budget caps")
    b.add_argument("--agent-id", required=True)
    b.add_argument("--monthly-cents", type=int, required=True)
    b.add_argument("--daily-cents", type=int, default=0)
    b.add_argument("--monthly-tokens", type=int, default=0, help="monthly token-burn cap (rail=tokens agents)")
    b.add_argument("--daily-tokens", type=int, default=0, help="daily token-burn cap (rail=tokens agents)")
    b.add_argument("--agent-secret", help="required for remote writes")
    b.add_argument("--workspace-key", help="required when claiming a brand-new agent_id")
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

    i = sub.add_parser("init", help="mint a workspace + agent and write a .env (one command to metered)")
    i.add_argument("--agent", required=True, help="the agent_id to create")
    i.add_argument("--agent-secret", help="reuse an existing agent_id instead of claiming a new one")
    i.add_argument("--env-file", default=".env", help="where to write the credentials ('' to skip)")
    i.set_defaults(fn=cmd_init)

    s = sub.add_parser("share", help="mint a read-only link to an agent's report page")
    s.add_argument("--agent-id", required=True)
    s.add_argument("--agent-secret", help="the agent's own secret (read grant)")
    s.add_argument("--workspace-key", help="or the workspace key")
    s.add_argument("--ttl-days", type=int, default=7, help="1-90, default 7")
    s.set_defaults(fn=cmd_share)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
