#!/opt/miniconda3/bin/python3
"""Audit that the city's public catalog tells the truth about its own MCP tools.

`/v1/products` publishes a per-product `tools` list, and `/llms.txt` publishes tool
lists and versions. An agent that reads either and acts on it must not be misled:
a name that does not exist costs a failed call, and a name that was deliberately
withdrawn can re-publish a hint that was taken down on purpose.

This drives a real MCP session against each mount the catalog advertises and compares
the catalogue's claim to the tools/list the mount actually returns.

    /opt/miniconda3/bin/python3 scripts/audit_city_tools.py            # live
    /opt/miniconda3/bin/python3 scripts/audit_city_tools.py --host URL # other host

Exit codes: 0 = every claim matches, 3 = a mismatch or an unreadable mount (so it
cannot pass silently on a network error).

Found by this audit on 2026-10-07: the catalog advertised `ledger_list_agents`, which
does not exist — it had been removed from the MCP surface on 2026-09-26 because
advertising it told every arriving agent that a cross-tenant listing exists and that
its key is called `admin_secret`.

NOTE on why a tools/call check cannot be used here: the city gateway translates
satellite tool calls to REST, so calling a NONEXISTENT tool still returns HTTP 200
with a payload (verified: `ts_health` returned 200 while absent). Only tools/list
distinguishes a real tool from a translated name, which is what this script reads.
"""
import argparse
import json
import sys
import urllib.request

DEFAULT_HOST = "https://aiagentscity.com"


def parse_names(raw: str):
    """Return the tool names in a JSON-RPC reply. Handles plain JSON and SSE."""
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
        except Exception:
            return None
        t = (d.get("result") or {}).get("tools")
        return [x["name"] for x in t] if t else None
    for ln in raw.splitlines():
        ln = ln.strip()
        if ln.startswith("data:"):
            try:
                d = json.loads(ln[5:].strip())
            except Exception:
                continue
            t = (d.get("result") or {}).get("tools")
            if t:
                return [x["name"] for x in t]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=DEFAULT_HOST)
    args = ap.parse_args()
    host = args.host.rstrip("/")

    def get(path):
        with urllib.request.urlopen(host + path, timeout=30) as r:
            return r.status, r.read().decode()

    def post(path, payload, sid=None):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if sid:
            h["mcp-session-id"] = sid
        req = urllib.request.Request(host + path,
                                     data=json.dumps(payload).encode(), headers=h)
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.status, r.read().decode(), dict(r.headers)
        except Exception as e:
            return getattr(e, "code", 0), str(e), {}

    st, body = get("/v1/products")
    if st != 200:
        print(f"FATAL: {host}/v1/products -> HTTP {st}")
        return 3
    catalog = json.loads(body)["products"]

    # /llms.txt carries its own version strings; they must not contradict the catalog.
    st, llms = get("/llms.txt")
    llms_ok = st == 200

    failures = 0
    print(f"{'mount':22}{'claimed':>8}{'served':>8}   verdict")
    print("-" * 64)
    for p in catalog:
        mount = p["mcp_url"].replace(host, "") or "/mcp/"
        if not mount.endswith("/"):
            mount += "/"
        claimed = p["tools"]

        _, _, hdrs = post(mount, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-06-18",
                                             "capabilities": {},
                                             "clientInfo": {"name": "audit",
                                                            "version": "1"}}})
        sid = hdrs.get("mcp-session-id") or hdrs.get("Mcp-Session-Id")
        post(mount, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        _, raw, _ = post(mount, {"jsonrpc": "2.0", "id": 2,
                                 "method": "tools/list", "params": {}}, sid)

        served = parse_names(raw)
        if served is None:
            print(f"{p['id']:22}{len(claimed):>8}{'?':>8}   UNVERIFIED (no tools/list)")
            failures += 1
            continue

        phantom = [t for t in claimed if t not in served]
        missing = [t for t in served if t not in claimed]
        verdict = "OK"
        if phantom:
            verdict = f"PHANTOM {phantom}"
        elif missing:
            verdict = f"UNDOCUMENTED {missing}"
        if phantom or missing:
            failures += 1
        print(f"{p['id']:22}{len(claimed):>8}{len(served):>8}   {verdict}")

        if llms_ok:
            if p["version"] not in llms:
                print(f"{'':22}{'':>8}{'':>8}   llms.txt omits version {p['version']}")
                failures += 1

    if llms_ok:
        for stale in ("v1.30.0", "v4.0.3", "v0.4.1", "ledger_list_agents"):
            if stale in llms:
                print(f"  llms.txt still contains the withdrawn/stale token {stale!r}")
                failures += 1

    print()
    if failures:
        print(f"RESULT: {failures} problem(s) — a public surface is telling agents "
              f"something untrue.")
        return 3
    print("RESULT: every advertised tool exists, and every served tool is advertised.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
