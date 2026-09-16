#!/usr/bin/env python3
"""AgentLedger proxy — the enforcement layer (D-1222, BUILD-2).

What this changes, and why it is the whole point of the product: every other
cap in AgentLedger is advisory. `POST /v1/track` blocks the ledger entry that
would cross a cap, but the provider has already charged the card. Here the call
is refused BEFORE the provider is contacted, so the spend genuinely does not
happen.

**Option A, chosen by the principal 2026-09-13: pass-through, no key custody.**
The caller's provider credential rides in the request (`Authorization` for
OpenAI, `x-api-key` for Anthropic) and is forwarded verbatim. This module never
writes it anywhere — not to disk, not to a log, not to the ledger. The
consequence is stated plainly everywhere it matters: a caller can bypass the
proxy, and enforcement only covers traffic that goes through it.

Money handling worth knowing about:

* Costs come from the provider's own `usage` payload, never from anything the
  caller sent. A caller cannot under-report spend to dodge a cap.
* The ledger stores integer cents, but real calls cost fractions of a cent.
  Rounding each call to zero would let an agent make thousands of tiny calls and
  never trip a dollar cap, so the sub-cent residue is carried forward per agent
  and paid into the next entry. No money is lost to rounding and none is
  invented by it.
* An unpriced model is NOT blocked and NOT silently free: the call passes
  through and an alert fires, so the owner learns their table is incomplete
  instead of discovering a zero-spend report.
"""
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

PRICE_FILE = Path(__file__).with_name("model_prices.json")
DEFAULT_MAX_TOKENS = 4096          # used only to ESTIMATE when the caller omits it
CHARS_PER_TOKEN = 4                # crude, conservative, documented in /v1/pricing

PROVIDERS = {
    "openai": {
        "base_url": "https://api.openai.com",
        "credential_header": "authorization",
        "usage_in": "prompt_tokens",
        "usage_out": "completion_tokens",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "credential_header": "x-api-key",
        "usage_in": "input_tokens",
        "usage_out": "output_tokens",
        # Anthropic reports the three input buckets SEPARATELY, and
        # `input_tokens` counts only the uncached remainder. OpenAI's and
        # DeepSeek's `prompt_tokens` are totals that already include the cached
        # part. Summing one convention and not the other would misprice every
        # call, so the convention is declared here instead of assumed.
        "usage_in_excludes_cache": True,
        "usage_cache_hit": "cache_read_input_tokens",
        "usage_cache_write_5m": "cache_creation.ephemeral_5m_input_tokens",
        "usage_cache_write_1h": "cache_creation.ephemeral_1h_input_tokens",
        "usage_cache_write_total": "cache_creation_input_tokens",
    },
}

PROVIDER_FILE = Path(__file__).with_name("providers.json")
_provider_cache = {"mtime": 0.0, "data": {}}


def _extra_providers() -> dict:
    """Providers an operator added by editing providers.json.

    The point of this file is that an agent's spend must be meterable
    regardless of which vendor it calls. A hard-coded provider list guarantees
    the next vendor is invisible, which is exactly how DeepSeek went
    uncounted until someone noticed.
    """
    try:
        mtime = PROVIDER_FILE.stat().st_mtime
    except OSError:
        return {}
    if mtime != _provider_cache["mtime"]:
        try:
            data = json.loads(PROVIDER_FILE.read_text()).get("providers", {})
            _provider_cache["data"] = {k: v for k, v in data.items() if isinstance(v, dict)}
            _provider_cache["mtime"] = mtime
        except Exception:
            _provider_cache["data"] = {}
    return _provider_cache["data"]


def providers() -> dict:
    """Built-ins plus anything in providers.json. The file wins, so an operator
    can point an existing provider at a gateway without touching code."""
    merged = dict(PROVIDERS)
    merged.update(_extra_providers())
    return merged


def provider_ids() -> list:
    return sorted(providers())


# Headers that belong to THIS hop and must not be forwarded verbatim.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
})
# Our own control headers — never forwarded upstream.
_CONTROL = frozenset({"x-al-agent", "x-al-secret", "al-api-version"})


def _data_dir() -> Path:
    return Path(os.environ.get("AGENT_LEDGER_DATA",
                               os.path.expanduser("~/.agent-ledger")))


# ── price table ────────────────────────────────────────────────────────────

_cache = {"mtime": 0.0, "data": {}, "aliases": {}}


def prices() -> dict:
    """The loaded table, reloaded when the file changes so an operator can fix
    a price without a redeploy."""
    try:
        mtime = PRICE_FILE.stat().st_mtime
    except OSError:
        return {}
    if mtime != _cache["mtime"]:
        try:
            doc = json.loads(PRICE_FILE.read_text())
            _cache["data"] = doc.get("models", {})
            _cache["aliases"] = doc.get("aliases", {}) or {}
            _cache["mtime"] = mtime
        except Exception:
            _cache["data"] = {}
            _cache["aliases"] = {}
    return _cache["data"]


def aliases() -> dict:
    """Exact provider model id -> canonical priced row. Loaded with prices()."""
    prices()   # ensures the mtime check has run
    return _cache["aliases"]


# A dated snapshot suffix, e.g. claude-sonnet-4-5-20250929 / gpt-4o-2024-08-06.
_DATED_SUFFIX = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{8})$")
# Latest-alias suffixes the SDKs themselves send as the model string.
_LATEST_SUFFIX = re.compile(r"-(latest|preview|stable)$")


def resolve_model(model: str) -> tuple[Optional[str], Optional[str]]:
    """Canonical priced key for a model string, plus how it was resolved.

    Returns (canonical_key, reason) — canonical_key is None when the model is
    genuinely unpriced, and reason is "" in the exact-hit case.

    Why this exists: the wrapper and the proxy forward the caller's REAL model
    string, so the auto-pricing path saw `claude-sonnet-4-20250514` and refused
    the write with `model_not_priced` even though the same model was sitting in
    the table under its short name. Every tokens-only write from a real SDK was
    therefore rejected on the primary enforcement path.

    Three resolutions, in order — all of them claims the table actually makes:

    1. **Exact table hit.** No guessing involved.
    2. **Explicit `aliases` entry.** The table asserts these two strings are
       the same model at the same price.
    3. **Dated/latest suffix strip, ONLY when the stripped key exists AND is
       flagged `dated_variants_same_price`.** The flag is the load-bearing
       part, not the regex. A blanket strip is UNSAFE: OpenAI prices
       `gpt-4o-2024-05-13` at $5/$15 against base `gpt-4o` at $2.50/$10, so
       stripping the date there would meter a request at HALF its real cost —
       under-counting the exact number a spend cap is meant to stop. A family
       is only auto-stripped once its dates are verified to be same-price.
    """
    name = (model or "").strip()
    if not name:
        return None, ""
    table = prices()
    if name in table:
        return name, ""

    aliased = aliases().get(name)
    if aliased and aliased in table:
        return aliased, f"alias of {aliased}"

    if _LATEST_SUFFIX.search(name):
        base = _LATEST_SUFFIX.sub("", name)
        if base in table and base != name:
            return base, f"resolved to {base} (latest alias)"

    if _DATED_SUFFIX.search(name):
        base = _DATED_SUFFIX.sub("", name)
        entry = table.get(base)
        if entry is not None and entry.get("dated_variants_same_price"):
            return base, f"resolved to {base} (dated snapshot, same price)"

    return None, ""


def lookup(model: str) -> Optional[dict]:
    """The price entry for a model string, resolving provider ids to rows."""
    key, _ = resolve_model(model)
    return prices().get(key) if key else None


def price_provenance() -> dict:
    """What a user is entitled to see: which models have prices, from when, and
    whether they have been verified against the provider."""
    table = prices()
    return {
        "models": table,
        "count": len(table),
        "verified_count": sum(1 for v in table.values() if v.get("verified")),
        "source": str(PRICE_FILE.name),
        "aliases": aliases(),
        "alias_count": len(aliases()),
        "_note": ("Anyone relying on these numbers for cost decisions should check them "
                  "against their provider's pricing page. Unverified entries are "
                  "placeholders. An unlisted model is not blocked — it passes through "
                  "and raises an alert. `aliases` maps real provider model ids (e.g. "
                  "claude-sonnet-4-20250514) to the priced row they resolve to; a dated "
                  "id is only resolved automatically for rows marked "
                  "dated_variants_same_price, because some vendors price dated snapshots "
                  "differently from their base model."),
    }


def _cents(tokens: int, per_million_usd: float) -> float:
    return (tokens / 1_000_000.0) * per_million_usd * 100.0


def cost_cents_exact(model: str, tokens_in: int, tokens_out: int,
                     cache_hit_in: int = 0, cache_write_5m_in: int = 0,
                     cache_write_1h_in: int = 0) -> Optional[float]:
    """Exact cost in cents (fractional). None when the model is unpriced.

    Input is billed in up to three tiers: fresh (standard rate), read from
    cache, and written to cache. Cache writes cost MORE than fresh input
    (Claude: 1.25x for 5-minute, 2x for 1-hour) and cache reads cost far less
    (0.1x). A model whose entry carries no cache rates charges every input
    token the standard rate — the conservative direction for a cap, but wrong
    for a cache-heavy workload, where it is wrong in both directions at once.

    Cache buckets are clamped against each other in order (hit, then 5m, then
    1h) so an over-reported bucket can never produce a negative fresh count.
    """
    entry = lookup(model)
    if not entry:
        return None
    in_rate = entry.get("in", 0)
    total_in = int(tokens_in)

    hit = max(0, min(int(cache_hit_in or 0), total_in))
    write_5m = max(0, min(int(cache_write_5m_in or 0), total_in - hit))
    write_1h = max(0, min(int(cache_write_1h_in or 0), total_in - hit - write_5m))
    fresh = total_in - hit - write_5m - write_1h

    return (_cents(fresh, in_rate)
            + _cents(hit, entry.get("cache_hit", in_rate))
            + _cents(write_5m, entry.get("cache_write_5m", in_rate))
            + _cents(write_1h, entry.get("cache_write_1h", in_rate))
            + _cents(tokens_out, entry.get("out", 0)))


def merge_usage(earlier: Optional[dict], later: Optional[dict]) -> Optional[dict]:
    """Combine usage captured from successive stream events.

    Streaming splits usage across events, and the LAST event is not the whole
    story: Anthropic sends input and cache counts in `message_start`, then the
    final output count in `message_delta` — which carries no input at all.
    Assigning each event over the previous one therefore recorded a streamed
    Claude call's input tokens as ZERO, about 1% of its true cost, on the
    dominant traffic shape for the product's flagship integration.
    (Adversarial review 2026-09-13.)

    Take the max per field: input counts are constant, output counts are
    cumulative, so the largest value seen is the true one.
    """
    if not earlier:
        return later
    if not later:
        return earlier
    merged = dict(earlier)
    for key, value in later.items():
        if isinstance(value, int) and value > (merged.get(key) or 0):
            merged[key] = value
    return merged


# ── estimation (pre-call) ──────────────────────────────────────────────────

def estimate_prompt_tokens(payload: dict) -> int:
    """Deliberately crude: a serialised body divided by CHARS_PER_TOKEN. It
    only has to be good enough to decide 'would this call blow the cap' before
    the money is spent, and being slightly high is the safe direction."""
    try:
        # Every field that carries prompt text, not just 'messages'. A caller
        # sending a large `tools` array, a top-level Anthropic `system`, or
        # `instructions` was sending cost this estimate never saw — which made
        # the 402 gate optional for anyone who set them. (Adversarial review
        # 2026-09-13: 50 tool definitions estimated at 8 tokens.)
        parts = [payload[k] for k in ("messages", "input", "prompt", "system",
                                      "instructions", "tools", "functions")
                 if k in payload]
        blob = json.dumps(parts) if parts else ""
        return max(1, len(blob) // CHARS_PER_TOKEN)
    except Exception:
        return 1


def requested_max_tokens(payload: dict) -> int:
    """The most output this call can produce.

    `n` multiplies it: 128 completions of 4096 tokens is 128x the output of
    one, and a gate reading max_tokens alone passes it. (Adversarial review
    2026-09-13: n=128 drove real cost ~7x past the estimate, through the 402.)
    """
    base = DEFAULT_MAX_TOKENS
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        value = payload.get(key)
        if isinstance(value, int) and value > 0:
            base = value
            break
    n = payload.get("n")
    if isinstance(n, int) and n > 1:
        base *= n
    return base


def call_estimate_cents(model: str, payload: dict) -> Optional[int]:
    """The most this call can plausibly cost, in whole cents. None when the
    model has no price — the caller then passes through and alerts."""
    entry = lookup(model)
    if not entry:
        return None
    est_in = estimate_prompt_tokens(payload)
    est_out = requested_max_tokens(payload)
    return int(-(-(cost_cents_exact(model, est_in, est_out) or 0) // 1))  # ceil


# ── usage → tokens ─────────────────────────────────────────────────────────

def usage_tokens(provider: str, data: dict) -> Optional[dict]:
    """Pull (tokens_in, tokens_out) out of a provider response or stream chunk.

    Returns None when the payload carries no usage at all — streaming without
    an explicit usage request, for instance. Callers must treat None as
    'unmetered', never as zero.
    """
    if not isinstance(data, dict):
        return None
    cfg = providers().get(provider, {})
    usage = data.get("usage")
    if not isinstance(usage, dict):
        # Anthropic streaming: usage rides inside message_start / message_delta
        event_type = data.get("type")
        if event_type in ("message_start", "message_delta"):
            usage = (data.get("message") or {}).get("usage") or data.get("usage")
        if not isinstance(usage, dict):
            return None
    def _field(path):
        """Dotted lookup — Anthropic nests its cache-write split one level down."""
        node = usage
        for part in path.split("."):
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        return node if isinstance(node, int) and node >= 0 else None

    tout = usage.get(cfg.get("usage_out", "completion_tokens"), 0)
    if not isinstance(tout, int):
        return None

    hit = _field(cfg["usage_cache_hit"]) if cfg.get("usage_cache_hit") else None
    write_5m = _field(cfg["usage_cache_write_5m"]) if cfg.get("usage_cache_write_5m") else None
    write_1h = _field(cfg["usage_cache_write_1h"]) if cfg.get("usage_cache_write_1h") else None

    if cfg.get("usage_in_excludes_cache"):
        fresh = usage.get(cfg.get("usage_in", "input_tokens"), 0)
        if not isinstance(fresh, int):
            return None
        write_total = _field(cfg["usage_cache_write_total"]) if cfg.get("usage_cache_write_total") else None
        # Some providers report only the combined cache-write figure. Attribute
        # the whole of it to the 5-minute tier: it is the cheaper of the two,
        # so an unknown split under-charges rather than inventing a higher bill.
        if write_total and not (write_5m or write_1h):
            write_5m = write_total
        tin = fresh + (hit or 0) + (write_total or 0)
    else:
        tin = usage.get(cfg.get("usage_in", "prompt_tokens"), 0)
        if not isinstance(tin, int):
            return None
        write_5m, write_1h = 0, 0

    if tin == 0 and tout == 0:
        return None
    return {"tokens_in": tin, "tokens_out": tout, "cache_hit_in": hit or 0,
            "cache_write_5m_in": write_5m or 0, "cache_write_1h_in": write_1h or 0}


# ── sub-cent residue: never lose money to integer rounding ─────────────────

def _residue_path(agent_id: str) -> Path:
    return _data_dir() / "agents" / agent_id / "proxy_residue.json"


def take_residue(agent_id: str) -> float:
    path = _residue_path(agent_id)
    if not path.exists():
        return 0.0
    try:
        return float(json.loads(path.read_text()).get("cents", 0.0))
    except Exception:
        return 0.0


def _save_residue(agent_id: str, cents: float) -> None:
    path = _residue_path(agent_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"cents": round(cents, 6), "updated": time.time()}))
    except Exception:
        pass


def whole_cents_with_residue(agent_id: str, exact_cents: float) -> int:
    """Settle a fractional cost into whole cents, carrying the remainder."""
    total = take_residue(agent_id) + max(0.0, exact_cents)
    whole = int(total)
    _save_residue(agent_id, total - whole)
    return whole


# ── upstream plumbing ──────────────────────────────────────────────────────

def provider_config(provider: str) -> Optional[dict]:
    """Built-in first, then anything added in providers.json."""
    return providers().get(provider)


def upstream_url(provider: str, path: str) -> Optional[str]:
    """The provider endpoint for this call.

    The base is overridable per provider (AL_PROXY_OPENAI_BASE,
    AL_PROXY_ANTHROPIC_BASE) so a self-hoster can point at Azure, OpenRouter or
    their own gateway — and so the tests can point at a local fake upstream
    instead of the real API.
    """
    cfg = provider_config(provider)
    if not cfg:
        return None
    base = os.environ.get(f"AL_PROXY_{provider.upper()}_BASE", cfg["base_url"])
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def forward_headers(headers, provider: str) -> dict:
    """Everything except hop-by-hop headers and our own control headers.

    The provider credential passes through untouched: this function does not
    look at it, copy it, or store it. `credential_header` exists only so the
    caller (and tests) can assert the pass-through is wired, never so the
    platform can hold the value.
    """
    out = {}
    for key, value in dict(headers).items():
        lowered = key.lower()
        if lowered in _HOP_BY_HOP or lowered in _CONTROL:
            continue
        out[key] = value
    return out


def missing_credential(provider: str, headers) -> bool:
    cfg = provider_config(provider)
    if not cfg:
        return True
    return not dict(headers).get(cfg["credential_header"])
