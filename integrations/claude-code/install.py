#!/usr/bin/env python3
"""Wire Claude Code to AgentLedger: MCP tools + automatic spend reporting.

Run once:

    python3 integrations/claude-code/install.py --agent-id claude-code

What it changes in your Claude Code settings (all of it reversible, and a
backup is written first):

1. `mcpServers["agent-ledger"]` — so the agent can call the ledger_* tools.
2. A `Stop` hook running report-session.py — so every session's token spend is
   reported when the agent finishes responding, without anyone remembering to.

It MERGES. Existing hooks, MCP servers and every other key are preserved: a
Claude Code settings file is a personal thing that other tools have already
written to, and an installer that rewrites it is an installer that breaks
someone's setup. Running it twice is a no-op rather than a duplicate.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

MARKER = "report-session.py"          # identifies OUR hook among the user's own
DEFAULT_REPORTER = Path(__file__).resolve().parent / "report-session.py"
DEFAULT_BASE = "https://agent-ledger-production-0ff8.up.railway.app"


def merge_mcp(settings: dict, url: str, name: str = "agent-ledger") -> bool:
    """Returns True if anything changed. The http shape is what this Claude
    Code version reads (same as any other remote MCP server)."""
    servers = settings.setdefault("mcpServers", {})
    entry = {"type": "http", "url": url}
    if servers.get(name) == entry:
        return False
    servers[name] = entry
    return True


def merge_stop_hook(settings: dict, command: str) -> bool:
    """Add or refresh our Stop hook, leaving every other hook untouched."""
    hooks = settings.setdefault("hooks", {})
    stop = hooks.setdefault("Stop", [])
    if not isinstance(stop, list):
        raise SystemExit("settings.json has a non-list hooks.Stop — refusing to guess")

    changed = False
    found = False
    for entry in stop:
        for hook in (entry.get("hooks") or []) if isinstance(entry, dict) else []:
            if isinstance(hook, dict) and MARKER in str(hook.get("command", "")):
                found = True
                if hook.get("command") != command:
                    hook["command"] = command      # refresh a stale path
                    changed = True
    if not found:
        # Match the shape already used in the file: one entry holding a list.
        stop.append({"hooks": [{"type": "command", "command": command, "timeout": 60}]})
        changed = True
    return changed


def uninstall(settings: dict) -> bool:
    """Remove exactly what we added, and only what we added."""
    changed = False
    servers = settings.get("mcpServers") or {}
    if "agent-ledger" in servers:
        del servers["agent-ledger"]
        changed = True
    for entry in settings.get("hooks", {}).get("Stop", []) or []:
        if not isinstance(entry, dict):
            continue
        keep = [h for h in (entry.get("hooks") or [])
                if not (isinstance(h, dict) and MARKER in str(h.get("command", "")))]
        if len(keep) != len(entry.get("hooks") or []):
            entry["hooks"] = keep
            changed = True
    return changed


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Wire Claude Code to AgentLedger")
    p.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"),
                   help="Claude Code settings file to merge into")
    p.add_argument("--api-base", default=DEFAULT_BASE)
    p.add_argument("--agent-id", default="claude-code",
                   help="the agent_id this session's spend is attributed to")
    p.add_argument("--reporter", default=str(DEFAULT_REPORTER))
    p.add_argument("--python", default=sys.executable or "python3")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    path = Path(args.settings).expanduser()
    settings = json.loads(path.read_text()) if path.exists() else {}

    if args.uninstall:
        changed = uninstall(settings)
        if not changed:
            print("nothing to remove — AgentLedger is not wired into this file")
            return 0
    else:
        reporter = Path(args.reporter).expanduser()
        if not reporter.exists() and not args.dry_run:
            print(f"reporter not found: {reporter}", file=sys.stderr)
            return 1
        command = (f"AGENT_LEDGER_API_BASE={args.api_base} "
                   f"AGENT_LEDGER_AGENT_ID={args.agent_id} "
                   f"{args.python} {reporter}")
        changed = merge_mcp(settings, args.api_base.rstrip("/") + "/mcp/")
        changed = merge_stop_hook(settings, command) or changed
        if not changed:
            print(f"already wired up in {path} — nothing to do")
            return 0

    rendered = json.dumps(settings, indent=2) + "\n"
    if args.dry_run:
        print(rendered)
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        print(f"backed up the original to {path}.bak")
    path.write_text(rendered)
    print(f"{'removed AgentLedger from' if args.uninstall else 'wired AgentLedger into'} {path}")

    if not args.uninstall:
        print(f"""
Next:
  1. create the agent and write its credentials:
       agent-ledger --api-base {args.api_base} init --agent {args.agent_id}
  2. confirm what a session cost, without posting anything:
       {args.python} {Path(args.reporter).expanduser()} --dry-run
  3. set a cap — this is the part that stops spend:
       agent-ledger --api-base {args.api_base} set-budget \\
           --agent-id {args.agent_id} --agent-secret <secret> --monthly-cents 5000

From the next session on, spend is reported automatically when the agent stops
responding. To undo: re-run with --uninstall.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
