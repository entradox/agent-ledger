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

_cache = {"mtime": 0.0, "data": {}}


def prices() -> dict:
    """The loaded table, reloaded when the file changes so an operator can fix
    a price without a redeploy."""
    try:
        mtime = PRICE_FILE.stat().st_mtime
    except OSError:
        return {}
    if mtime != _cache["mtime"]:
        try:
            _cache["data"] = json.loads(PRICE_FILE.read_text()).get("models", {})
            _cache["mtime"] = mtime
        except Exception:
            _cache["data"] = {}
    return _cache["data"]


def lookup(model: str) -> Optional[dict]:
    return prices().get(model or "")


def price_provenance() -> dict:
    """What a user is entitled to see: which models have prices, from when, and
    whether they have been verified against the provider."""
    table = prices()
    return {
        "models": table,
        "count": len(table),
        "verified_count": sum(1 for v in table.values() if v.get("verified")),
        "source": str(PRICE_FILE.name),
        "_note": ("Anyone relying on these numbers for cost decisions should check them "
                  "against their provider's pricing page. Unverified entries are "
                  "placeholders. An unlisted model is not blocked — it passes through "
                  "and raises an alert."),
    }


def _cents(tokens: int, per_million_usd: float) -> float:
    return (tokens / 1_000_000.0) * per_million_usd * 100.0


def cost_cents_exact(model: str, tokens_in: int, tokens_out: int,
                     cache_hit_in: int = 0) -> Optional[float]:
    """Exact cost in cents (fractional). None when the model is unpriced.

    A price entry may carry a `cache_hit` rate, used for the cached portion of
    the input. When it does not, every input token is charged the standard
    rate, which is the conservative direction for a spend cap.
    """
    entry = lookup(model)
    if not entry:
        return None
    hit_rate = entry.get("cache_hit")
    if hit_rate is None:
        return _cents(tokens_in, entry.get("in", 0)) + _cents(tokens_out, entry.get("out", 0))
    hit = max(0, min(int(cache_hit_in or 0), int(tokens_in)))
    miss = int(tokens_in) - hit
    return (_cents(miss, entry.get("in", 0)) + _cents(hit, hit_rate)
            + _cents(tokens_out, entry.get("out", 0)))


# ── estimation (pre-call) ──────────────────────────────────────────────────

def estimate_prompt_tokens(payload: dict) -> int:
    """Deliberately crude: a serialised body divided by CHARS_PER_TOKEN. It
    only has to be good enough to decide 'would this call blow the cap' before
    the money is spent, and being slightly high is the safe direction."""
    try:
        blob = json.dumps(payload.get("messages")
                          or payload.get("input")
                          or payload.get("prompt") or "")
        return max(1, len(blob) // CHARS_PER_TOKEN)
    except Exception:
        return 1


def requested_max_tokens(payload: dict) -> int:
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        value = payload.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return DEFAULT_MAX_TOKENS


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
    tin = usage.get(cfg.get("usage_in", "prompt_tokens"), 0)
    tout = usage.get(cfg.get("usage_out", "completion_tokens"), 0)
    if not isinstance(tin, int) or not isinstance(tout, int):
        return None
    if tin == 0 and tout == 0:
        return None
    # Cached input is a SUBSET of the input count, billed far cheaper. Not
    # reporting it would price every cached token at the cache-miss rate —
    # DeepSeek's hit rate is ~50x lower, so the error runs one direction and
    # it runs large.
    hit = 0
    cache_key = cfg.get("usage_cache_hit")
    if cache_key:
        value = usage.get(cache_key)
        if isinstance(value, int) and value > 0:
            hit = min(value, tin)
    return {"tokens_in": tin, "tokens_out": tout, "cache_hit_in": hit}


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
