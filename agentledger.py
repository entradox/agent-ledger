#!/usr/bin/env python3
"""AgentLedger client wrappers — point an existing SDK at the proxy in one line.

The problem this solves: the proxy is where spend actually gets stopped, but
reaching it by hand means knowing about base URLs, two custom headers, and the
fact that the proxy path has to be preserved through the SDK's own URL joining.
That is enough friction that most people would never get there.

    from openai import OpenAI
    import agentledger

    client = agentledger.wrap(OpenAI(api_key=KEY), agent_id="my-agent",
                              agent_secret="as_...")

After that line the client talks to AgentLedger instead of the provider, and
everything the SDK already does — streaming, retries, tool calls — goes through
the metered, budget-checked path unchanged.

**What wrap() does NOT do: it does not intercept, patch, or subclass the SDK.**
It repoints the client's base URL and adds two headers, using the public
constructor fields the SDKs already expose. Nothing is monkeypatched, so a
failure here surfaces as a wrong URL, not as mysterious behaviour deep inside a
transport layer.

**Your provider credential is untouched.** It stays exactly where the SDK put
it; the wrapper never reads, copies, or stores it. Pass-through is the whole
design (see the repo README), and this module must not become the exception
that starts holding keys.
"""
import os
from typing import Optional

DEFAULT_BASE_URL = "https://aiagentscity.com"

# Where each SDK's requests land on the proxy. The proxy forwards the rest of
# the path to the provider verbatim, so the provider's own path is preserved —
# which is why the openai entry keeps /v1 and the anthropic one does not: the
# OpenAI SDK appends "chat/completions" to its base, while the Anthropic SDK
# appends a full "/v1/messages". Getting this backwards silently 404s, so
# tests/test_wrapper.py asserts the observed path for both SDKs.
PROVIDER_BASE_PATHS = {
    "openai": "/proxy/openai/v1/",
    "anthropic": "/proxy/anthropic",
    "deepseek": "/proxy/deepseek/v1/",
}


def detect_provider(client) -> str:
    """Which provider this client belongs to, from its own class, not a guess."""
    module = (type(client).__module__ or "") + "." + (type(client).__name__ or "")
    if "anthropic" in module.lower():
        return "anthropic"
    if "openai" in module.lower():
        return "openai"
    raise TypeError(
        f"agentledger.wrap() cannot tell which provider {type(client).__name__} talks to. "
        f"Pass provider='openai' or provider='anthropic' explicitly.")


def wrap(client, *, agent_id: str, agent_secret: Optional[str] = None,
         provider: Optional[str] = None, base_url: Optional[str] = None,
         workspace_key: Optional[str] = None):
    """Repoint an SDK client at AgentLedger. Returns the same client.

    agent_id / agent_secret come from `agent-ledger init` (or POST /start plus
    a first write). They identify WHICH agent gets billed; the provider
    credential the client already holds is forwarded untouched.

    `workspace_key` is accepted ONLY so that a stale example raises a useful
    error instead of a bare TypeError. It cannot authorize a proxied call: the
    proxy authenticates with the agent's own secret (X-AL-Secret), and a
    workspace_key is not one. The public snippet used to advertise
    `workspace_key=`, which meant the very first line a user copy-pasted from
    the site died with "wrap() got an unexpected keyword argument". If you are
    here because of that message, the fix is one call away — see below.
    """
    if workspace_key and not agent_secret:
        raise ValueError(
            "agentledger.wrap() takes agent_secret, not workspace_key. A "
            "workspace_key cannot authorize a proxied call — the proxy "
            "authenticates per agent. Get the agent's secret with:\n"
            "    agent-ledger init --agent <name>        # prints it once and writes .env\n"
            "or claim the agent by sending your workspace_key in the BODY of a "
            "first POST /v1/track (the response returns agent_secret). Then:\n"
            "    agentledger.wrap(client, agent_id='<name>', agent_secret='as_...')")
    if not agent_id or not agent_secret:
        raise ValueError("agentledger.wrap() needs both agent_id and agent_secret")
    provider = provider or detect_provider(client)
    if provider not in PROVIDER_BASE_PATHS:
        raise ValueError(
            f"unknown provider {provider!r} — known: {sorted(PROVIDER_BASE_PATHS)}. "
            f"A vendor the proxy does not list can still be added in providers.json "
            f"on the server side, then pass provider='<that name>' here.")

    root = (base_url or os.environ.get("AGENT_LEDGER_API_BASE") or DEFAULT_BASE_URL).rstrip("/")
    target = root + PROVIDER_BASE_PATHS[provider]

    # A plain string, deliberately. The two SDKs do not share an HTTP library —
    # openai uses httpx, anthropic 1.x uses httpx2 with its own URL class — so
    # handing either one the other's URL type raises TypeError at request time.
    # Both accept a str, and neither validates on assignment, so the failure
    # would surface on the user's first API call rather than here.
    url = target
    client.base_url = url
    # The HTTP layer underneath caches its own base_url; leaving it stale would
    # send the request to the real provider — the silent bypass this wrapper
    # exists to prevent.
    inner = getattr(client, "_client", None)
    if inner is not None and hasattr(inner, "base_url"):
        inner.base_url = url

    headers = {"X-AL-Agent": agent_id, "X-AL-Secret": agent_secret}
    if getattr(client, "default_headers", None) is None:
        client.default_headers = dict(headers)
    else:
        client.default_headers.update(headers)
    if inner is not None and hasattr(inner, "headers"):
        try:
            inner.headers.update(headers)
        except Exception:
            pass

    return client


def track(agent_id: str, *, agent_secret: str, amount_cents: Optional[int] = None,
          tokens_in: int = 0, tokens_out: int = 0, model: str = "",
          service: str = "", rail: str = "api_key",
          base_url: Optional[str] = None, timeout: int = 10) -> dict:
    """Report a spend entry yourself, for calls that never went through the proxy.

    Omit amount_cents and send tokens_in/tokens_out + model instead: the server
    prices it from its own table, so you never have to duplicate provider
    pricing. An unpriced model is refused rather than recorded as free.
    """
    import json as _json
    import urllib.request
    root = (base_url or os.environ.get("AGENT_LEDGER_API_BASE") or DEFAULT_BASE_URL).rstrip("/")
    body = {"agent_id": agent_id, "rail": rail, "agent_secret": agent_secret,
            "tokens_in": tokens_in, "tokens_out": tokens_out, "model": model}
    if amount_cents is not None:
        body["amount_cents"] = amount_cents
    if service:
        body["service"] = service
    req = urllib.request.Request(
        root + "/v1/track", data=_json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "AL-API-Version": "2026-09-01"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _json.loads(resp.read())
