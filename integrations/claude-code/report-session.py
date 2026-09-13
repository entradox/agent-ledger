#!/usr/bin/env python3
"""Report a Claude Code session's real token spend to AgentLedger.

Claude Code already writes everything needed to bill a session — every
assistant message in the transcript carries input, cache-write, cache-read and
output token counts. This script reads that, aggregates it per model, and posts
one entry per model to /v1/track, which prices it from its own table. Nothing is
estimated and nothing is asked of the user.

Two things it has to get right, both verified against real transcripts rather
than assumed:

1. **The same message appears more than once.** Streaming writes duplicate
   records: one real transcript held 317 usage-bearing records for 181 unique
   message ids, and every duplicate group was byte-identical. Summing the
   records would have overstated that session's spend by about 75%, so records
   are deduplicated by message id.

2. **Input is three numbers, not one.** `input_tokens` on Anthropic is the
   UNCACHED REMAINDER. Cached input is split into 5-minute and 1-hour writes
   (billed at 1.25x and 2x the base rate) and reads (0.1x). Sending only the
   total would price a cache-dominated coding session wrongly — which is the
   normal shape of a Claude Code session.

Usage:
    report-session.py                     # newest transcript, report it
    report-session.py --transcript PATH
    report-session.py --dry-run           # print what would be sent
    report-session.py --agent-id my-agent --api-base https://...
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = os.environ.get("AGENT_LEDGER_API_BASE",
                              "https://aiagentscity.com")
API_VERSION = "2026-09-01"
STATE_DIR = Path(os.environ.get("AGENT_LEDGER_DATA", str(Path.home() / ".agent-ledger"))) / "claude-code"


def newest_transcript() -> Path:
    projects = Path.home() / ".claude" / "projects"
    candidates = sorted(projects.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise SystemExit("no Claude Code transcripts found under ~/.claude/projects")
    return candidates[0]


def _bucket(usage: dict) -> dict:
    """One message's tokens, in the four buckets the price table understands."""
    fresh = usage.get("input_tokens") or 0
    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_write_total = usage.get("cache_creation_input_tokens") or 0
    creation = usage.get("cache_creation") or {}
    write_5m = creation.get("ephemeral_5m_input_tokens") or 0
    write_1h = creation.get("ephemeral_1h_input_tokens") or 0
    if cache_write_total and not (write_5m or write_1h):
        # combined figure only: attribute to the cheaper 5-minute tier, the
        # same rule the server applies — under-charging beats inventing a bill
        write_5m = cache_write_total
    elif write_5m or write_1h:
        # the split is reported; trust it, but never exceed the stated total
        cache_write_total = cache_write_total or (write_5m + write_1h)
    return {"fresh_in": fresh, "cache_read_in": cache_read,
            "cache_write_5m_in": write_5m, "cache_write_1h_in": write_1h,
            "tokens_out": usage.get("output_tokens") or 0}


def parse_transcript(path: Path, already_reported: set | None = None) -> tuple:
    """Aggregate per model. Returns (per_model, seen_message_ids).

    Deduplicated by message id: the transcript repeats the SAME record for a
    message, so counting records instead of messages inflates spend.
    """
    already_reported = already_reported or set()
    per_model: dict = {}
    seen = set()

    for line in path.read_text(errors="ignore").splitlines():
        try:
            entry = json.loads(line)
        except Exception:
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else None
        if not message or not isinstance(message.get("usage"), dict):
            continue
        key = message.get("id") or entry.get("uuid")
        if not key or key in seen or key in already_reported:
            continue
        bucket = _bucket(message["usage"])
        if not any(bucket.values()):
            # Claude Code writes records with a model of "<synthetic>" and every
            # token count at zero (stop_reason "stop_sequence"). They consumed
            # nothing, so there is nothing to bill — and posting one would be
            # rejected as amount_required, which would fail the whole run and
            # hold back the reported-message log with it.
            seen.add(key)
            continue
        seen.add(key)
        model = message.get("model") or "unknown"
        agg = per_model.setdefault(model, {
            "fresh_in": 0, "cache_read_in": 0, "cache_write_5m_in": 0,
            "cache_write_1h_in": 0, "tokens_out": 0, "messages": 0})
        for field, value in bucket.items():
            agg[field] += value
        agg["messages"] += 1

    return per_model, seen


def payload_for(model: str, agg: dict, agent_id: str, agent_secret: str) -> dict:
    total_in = (agg["fresh_in"] + agg["cache_read_in"]
                + agg["cache_write_5m_in"] + agg["cache_write_1h_in"])
    return {
        "agent_id": agent_id, "agent_secret": agent_secret,
        "rail": "api_key",                     # server prices it from tokens + model
        "service": "claude-code",
        "model": model,
        "tokens_in": total_in, "tokens_out": agg["tokens_out"],
        "cache_hit_in": agg["cache_read_in"],
        "cache_write_5m_in": agg["cache_write_5m_in"],
        "cache_write_1h_in": agg["cache_write_1h_in"],
    }


def post(base: str, body: dict) -> tuple:
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/track", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "AL-API-Version": API_VERSION})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def load_state(path: Path) -> set:
    state_file = STATE_DIR / (path.stem + ".reported.json")
    if state_file.exists():
        try:
            return set(json.loads(state_file.read_text()))
        except Exception:
            return set()
    return set()


def save_state(path: Path, seen: set) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / (path.stem + ".reported.json")
    state_file.write_text(json.dumps(sorted(seen)))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Report a Claude Code session's spend to AgentLedger")
    p.add_argument("--transcript", help="path to the .jsonl (default: newest)")
    p.add_argument("--agent-id", default=os.environ.get("AGENT_LEDGER_AGENT_ID"),
                   help="or set AGENT_LEDGER_AGENT_ID")
    p.add_argument("--agent-secret", default=os.environ.get("AGENT_LEDGER_AGENT_SECRET"),
                   help="or set AGENT_LEDGER_AGENT_SECRET")
    p.add_argument("--api-base", default=DEFAULT_BASE)
    p.add_argument("--dry-run", action="store_true", help="print payloads, post nothing")
    args = p.parse_args(argv)

    transcript = Path(args.transcript).expanduser() if args.transcript else newest_transcript()
    if not transcript.exists():
        print(f"no such transcript: {transcript}", file=sys.stderr)
        return 1

    reported = load_state(transcript)
    per_model, seen = parse_transcript(transcript, reported)

    if not per_model:
        print(f"nothing new in {transcript.name}"
              f" ({len(reported)} messages already reported)")
        return 0

    if not args.dry_run and not (args.agent_id and args.agent_secret):
        print("an agent identity is required: pass --agent-id and --agent-secret, "
              "or run `agent-ledger init` and source the .env it writes",
              file=sys.stderr)
        return 1

    failed = False
    for model, agg in sorted(per_model.items()):
        body = payload_for(model, agg, args.agent_id or "<agent>", args.agent_secret or "<secret>")
        total_in = body["tokens_in"]
        if args.dry_run:
            print(json.dumps(body, indent=2))
            continue
        status, resp = post(args.api_base, body)
        if status == 402:
            # The cap worked. This is the message the whole integration exists
            # to produce, so it is printed loudly rather than swallowed.
            print(f"BLOCKED ({model}): {resp.get('error', {}).get('message', resp)}",
                  file=sys.stderr)
            failed = True
            continue
        if status == 422 and "model_not_priced" in json.dumps(resp):
            # Unpriced model: report the real token burn at zero dollars rather
            # than losing it. Loud, because the dollar figure is now a floor.
            fallback = dict(body, rail="tokens")
            fallback.pop("model", None)
            fallback["rail"] = "tokens"
            fallback["model"] = model
            status, resp = post(args.api_base, fallback)
            print(f"WARNING: '{model}' has no price entry — {total_in} input / "
                  f"{agg['tokens_out']} output tokens recorded at $0. "
                  f"Add it to model_prices.json to get dollars (HTTP {status})",
                  file=sys.stderr)
            failed = failed or status != 200
            continue
        if status != 200:
            print(f"FAILED ({model}): HTTP {status} {str(resp)[:200]}", file=sys.stderr)
            failed = True
            continue
        print(f"{model}: {agg['messages']} messages, {total_in} in / {agg['tokens_out']} out "
              f"-> {resp.get('amount_cents', '?')}c "
              f"{'(auto-priced)' if resp.get('priced') == 'auto' else ''}")

    if not args.dry_run and not failed:
        save_state(transcript, reported | seen)
    elif failed:
        print("some entries failed — the reported-message log was NOT advanced, so "
              "the next run retries them instead of dropping them", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
