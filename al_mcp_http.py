#!/usr/bin/env python3
"""AgentLedger MCP server — hosted streamable-http mode for Railway.

TDQS-optimized tool definitions: full descriptions, parameter docs, and
behavioral annotations for registry scoring and agent routing.
"""
import os
from typing import Optional
from fastmcp import FastMCP
import metrics

mcp = FastMCP("agent-ledger")


def _record_mcp_call():
    try:
        metrics.record_event("mcp_call")
    except Exception:
        pass


def _claim_or_error(agent_id: str, agent_secret: str, workspace_key: str = ""):
    """Returns (secret, created, error_dict_or_None).

    The error dict mirrors the REST typed-error codes from
    routes_agents._claim_or_401 so an agent gets the same machine-readable
    reason on either surface — MCP tools can't raise HTTP status codes, so
    the code travels in the payload instead. A missing workspace_key on a
    NEW agent_id is a typed `workspace_key_required` error here, not an
    unhandled exception (which surfaced as a 500 before this fix).
    """
    from ledger_engine import (ensure_agent_secret, AuthError, BetaCapExceededError,
                               WorkspaceKeyRequiredError)
    try:
        secret, created = ensure_agent_secret(
            agent_id, agent_secret or None, workspace_key=workspace_key or None)
        return secret, created, None
    except AuthError as e:
        return None, None, {"error": str(e), "error_code": "agent_secret_mismatch"}
    except BetaCapExceededError as e:
        return None, None, {"error": str(e), "error_code": "beta_cap_exceeded"}
    except WorkspaceKeyRequiredError as e:
        return None, None, {"error": str(e), "error_code": "workspace_key_required"}


def _owned_or_error(agent_id: str, workspace_key: str):
    """Ownership check for the credential-lifecycle tools. Returns
    (workspace_id, error_dict_or_None) — same shape as _claim_or_error, so an
    agent reads the same typed error_code on MCP as it would on REST."""
    import identity
    from ledger_engine import agent_exists, validate_agent_id, ValidationError
    try:
        validate_agent_id(agent_id)
    except ValidationError as e:
        return None, {"error": str(e), "error_code": "invalid_agent_id"}
    workspace_id = identity.resolve_workspace_key(workspace_key or "")
    if not workspace_id:
        return None, {"error": ("rotating or revoking an agent_secret requires the "
                                "workspace_key that owns this agent_id"),
                      "error_code": "workspace_key_required"}
    if not agent_exists(agent_id):
        return None, {"error": f"agent_id '{agent_id}' is not claimed",
                      "error_code": "agent_not_claimed"}
    if not identity.agent_belongs_to_workspace(agent_id, workspace_id):
        return None, {"error": f"agent_id '{agent_id}' is not claimed in this workspace",
                      "error_code": "not_your_agent"}
    return workspace_id, None


@mcp.tool(annotations={"title": "Rotate Agent Secret", "readOnlyHint": False,
                        "destructiveHint": True, "idempotentHint": False})
def ledger_rotate_secret(agent_id: str, workspace_key: str) -> dict:
    """Mint a NEW agent_secret for an agent_id your workspace already owns, invalidating the old one.

    Use this to RECOVER an agent whose secret was lost: the previous credential
    stops working immediately. Requires the workspace_key that owns agent_id —
    an agent's own agent_secret cannot rotate itself, because a leaked agent
    credential must not be able to lock its real owner out. Unlike ledger_track
    this never claims a new agent_id: an unknown id returns agent_not_claimed.

    The new secret is returned ONCE. Store it before you drop the response.

    Returns {"agent_id", "agent_secret", "_note"}, or {"error", "error_code"}.
    """
    _record_mcp_call()
    from ledger_engine import rotate_agent_secret as _rotate
    workspace_id, err = _owned_or_error(agent_id, workspace_key)
    if err:
        return err
    secret = _rotate(agent_id, workspace_id)
    return {"agent_id": agent_id, "agent_secret": secret,
            "_note": ("Save this agent_secret — it replaced the previous one, which no "
                      "longer works. It will not be shown again.")}


@mcp.tool(annotations={"title": "Revoke Agent Secret", "readOnlyHint": False,
                        "destructiveHint": True, "idempotentHint": False})
def ledger_revoke_secret(agent_id: str, workspace_key: str) -> dict:
    """Invalidate an agent_id's agent_secret WITHOUT deleting its spend history.

    Use when a credential may have leaked, or to stop an agent writing.
    Subsequent writes to that agent fail with agent_secret_mismatch until you
    rotate a new secret in. The agent_id stays claimed, so no other workspace
    can claim it and inherit the ledger. Requires the workspace_key that owns
    agent_id.

    Returns {"agent_id", "revoked": True, "_note"}, or {"error", "error_code"}.
    """
    _record_mcp_call()
    from ledger_engine import revoke_agent_secret as _revoke
    workspace_id, err = _owned_or_error(agent_id, workspace_key)
    if err:
        return err
    _revoke(agent_id, workspace_id)
    return {"agent_id": agent_id, "revoked": True,
            "_note": ("The previous agent_secret no longer works. Spend history is "
                      "intact — call ledger_rotate_secret for a new one.")}


@mcp.tool(annotations={"title": "Track Agent Spend", "readOnlyHint": False,
                        "destructiveHint": False, "idempotentHint": False})
def ledger_track(agent_id: str, rail: str, amount_cents: int, service: str,
                 tokens_in: int = 0, tokens_out: int = 0, model: str = "",
                 agent_secret: str = "", workspace_key: str = "") -> dict:
    """Record a spend entry for an AI agent on any payment rail, with optional token counts.

    Claiming a brand-new agent_id requires your workspace_key (get one via
    x402 at POST /v1/billing/x402 — no human, no login — or at /start).
    That first call mints an
    agent_secret and returns it in the response — save it, every later call
    for that same agent_id must pass it back (no workspace_key needed again)
    or the write is rejected. Amounts are capped
    at $100,000/entry and must be >= 0. If a budget is set for this agent,
    an entry that would cross the monthly/daily cap is blocked, not just
    logged. Include tokens_in/tokens_out + model on every LLM call so token
    burn shows up in the /v1/tokens report.

    Args:
        agent_id: unique agent identifier (e.g. "research-agent-v2")
        rail: payment rail used — one of "mpp", "x402", "api_key", "manual"
        amount_cents: spend amount in cents (100 = $1.00), 0-10000000
        service: what was purchased (e.g. "search_query", "data_export")
        tokens_in: prompt tokens consumed (0 if unknown)
        tokens_out: completion tokens consumed (0 if unknown)
        model: model name (e.g. "gpt-4o") — token burn is reported per model
        agent_secret: required for every call after the first for this agent_id
        workspace_key: required when claiming a brand-new agent_id; not
                       needed once the agent_id has been claimed
    """
    from ledger_engine import track, ValidationError, BudgetExceededError
    secret, created, err = _claim_or_error(agent_id, agent_secret, workspace_key)
    if err:
        return err
    meta = {}
    if tokens_in or tokens_out:
        meta = {"tokens_in": tokens_in, "tokens_out": tokens_out}
        if model:
            meta["model"] = model
    try:
        entry = track(agent_id, rail, amount_cents, service, **meta)
    except (ValidationError, BudgetExceededError) as e:
        return {"error": str(e)}
    result = entry.to_dict()
    if created:
        result["agent_secret"] = secret
        result["_note"] = ("Save this agent_secret — required for every future write "
                            "to this agent_id (track/budget). It will not be shown again.")
    _record_mcp_call()
    return result


@mcp.tool(annotations={"title": "Set Agent Budget", "readOnlyHint": False,
                        "destructiveHint": True, "idempotentHint": True})
def ledger_set_budget(agent_id: str, monthly_cents: int, daily_cents: int = 0,
                      monthly_tokens: int = 0, daily_tokens: int = 0,
                      agent_secret: str = "", workspace_key: str = "") -> dict:
    """Set spending caps for an agent. Warns at 80%, blocks spend when exceeded
    — enforced: a ledger_track call that would cross the cap is rejected.

    Dollar caps (monthly_cents/daily_cents) and token caps (monthly_tokens/
    daily_tokens) are independent dimensions: dollar caps only cover
    non-"tokens" rails, token caps only cover rail="tokens" bookkeeping rows
    (tokens_in/tokens_out). Set both if the agent uses both.

    Monthly cap is required; the rest are optional (0 = no limit).
    Overwrites any existing budget for the agent. Claiming a brand-new
    agent_id requires your workspace_key; that first call mints an
    agent_secret (returned once — save it); later calls for that agent_id
    must pass the agent_secret back (no workspace_key needed again).

    Args:
        agent_id: unique agent identifier
        monthly_cents: monthly spending cap in cents
        daily_cents: daily spending cap in cents (0 = no daily cap)
        monthly_tokens: monthly token-burn cap (0 = no cap)
        daily_tokens: daily token-burn cap (0 = no cap)
        agent_secret: required for every call after the first for this agent_id
        workspace_key: required when claiming a brand-new agent_id; not
                       needed once the agent_id has been claimed
    """
    from ledger_engine import set_budget, ValidationError
    secret, created, err = _claim_or_error(agent_id, agent_secret, workspace_key)
    if err:
        return err
    try:
        b = set_budget(agent_id, monthly_cents, daily_cents,
                       monthly_tokens=monthly_tokens, daily_tokens=daily_tokens)
    except ValidationError as e:
        return {"error": str(e)}
    result = b.to_dict()
    if created:
        result["agent_secret"] = secret
        result["_note"] = ("Save this agent_secret — required for every future write "
                            "to this agent_id (track/budget). It will not be shown again.")
    _record_mcp_call()
    return result


def _authorize_read_or_error(agent_id: str, agent_secret: str, workspace_key: str):
    """MCP-side mirror of routes_agents._authorize_agent_read. Returns a
    typed error dict if neither credential authorizes, else None.

    Reads on the REST side are credential-gated (spec 3c); leaving the MCP
    reads open would be a straight bypass of that gate — an agent refused
    by GET /v1/report could read the same data through ledger_report with
    no credential at all. Same identity.authorize_agent_access call, so
    there is still exactly one place either credential is decided.
    """
    import identity
    if identity.authorize_agent_access(agent_id,
                                       agent_secret=agent_secret or None,
                                       workspace_key=workspace_key or None):
        return None
    return {"error": "this agent's data requires its agent_secret or workspace_key",
            "error_code": "agent_secret_mismatch"}


@mcp.tool(annotations={"title": "Agent Spend Report", "readOnlyHint": True,
                        "destructiveHint": False, "idempotentHint": True})
def ledger_report(agent_id: str, days: int = 30, agent_secret: str = "",
                  workspace_key: str = "") -> dict:
    """Spend report for an agent over a rolling window.

    Returns total spend, breakdown by rail and by service, budget status
    (ok/warning/exceeded), detected anomalies, and entry count.

    Requires a credential: either the agent's own agent_secret or its
    workspace's workspace_key (same rule as GET /v1/report).

    Args:
        agent_id: unique agent identifier
        days: report window in days (default 30)
        agent_secret: the agent's own secret (either this or workspace_key)
        workspace_key: the owning workspace's key (either this or agent_secret)
    """
    from ledger_engine import report
    err = _authorize_read_or_error(agent_id, agent_secret, workspace_key)
    if err:
        return err
    r = report(agent_id, days)
    _record_mcp_call()
    return {"agent_id": r.agent_id, "period": r.period,
            "total_spend_cents": r.total_spend_cents, "by_rail": r.by_rail,
            "by_service": r.by_service, "budget_status": r.budget_status,
            "anomalies": r.anomalies, "entry_count": r.entry_count}


@mcp.tool(annotations={"title": "Attach the Budget-Enforcing Proxy",
                        "readOnlyHint": True, "destructiveHint": False,
                        "idempotentHint": True})
def ledger_proxy_attach(agent_id: str, provider: str = "openai",
                        agent_secret: str = "", workspace_key: str = "") -> dict:
    """Point your provider traffic at the proxy so budget caps are enforced
    BEFORE the provider is contacted, instead of being reported afterwards.

    This closes the gap where the brake was unreachable from MCP: an agent
    connected over MCP could record spend (ledger_track) but nothing could
    refuse a call. With this, the cap is enforced on every LLM call.

    Returns the base_url to use, the two headers to send, and the exact change
    for the OpenAI and Anthropic SDKs. Your provider credential is NOT part of
    this: it stays in Authorization / x-api-key and is only forwarded, never
    stored.

    Args:
        agent_id: the agent whose budget the proxied calls are billed to
        provider: which upstream to proxy: openai or anthropic
        agent_secret: the agent's own secret (either this or workspace_key)
        workspace_key: the owning workspace's key (either this or agent_secret)
    """
    from ledger_engine import validate_agent_id, ValidationError
    import proxy as proxy_core
    try:
        validate_agent_id(agent_id)
    except ValidationError as e:
        return {"error": str(e), "error_code": "invalid_agent_id"}
    err = _authorize_read_or_error(agent_id, agent_secret, workspace_key)
    if err:
        return err

    provider = (provider or "").strip().lower()
    # Ask the proxy module which providers actually exist; never hard-code the
    # list here, or this tool advertises a rail the deployment has not enabled.
    supported = proxy_core.provider_ids()
    if provider not in supported:
        return {"error": f"provider must be one of {', '.join(supported)}; "
                         f"got '{provider}'",
                "error_code": "unsupported_provider"}
    cfg = proxy_core.provider_config(provider) or {}
    credential_header = cfg.get("credential_header") or "Authorization"

    base = os.environ.get("AL_PUBLIC_BASE_URL",
                          "https://aiagentscity.com").rstrip("/")
    proxy_base = f"{base}/proxy/{provider}"
    try:
        metrics.record_event("proxy_attach", via="mcp")
    except Exception:
        pass
    return {
        "base_url": proxy_base,
        "headers": {"X-AL-Agent": agent_id, "X-AL-Secret": agent_secret or "<your agent_secret>"},
        "required_headers": ["X-AL-Agent", "X-AL-Secret"],
        "provider_credential_header": credential_header,
        "do_not_move": "Your provider key stays where it is. Send it as "
                       f"{credential_header} exactly as you do today; the proxy forwards it "
                       "upstream and never stores it.",
        "how_to_use": {
            "openai_sdk": f"OpenAI(base_url=\"{proxy_base}\", "
                          f"default_headers={{\"X-AL-Agent\": \"{agent_id}\", "
                          f"\"X-AL-Secret\": \"<agent_secret>\"}})",
            "anthropic_sdk": f"Anthropic(base_url=\"{proxy_base}\", "
                             f"default_headers={{\"X-AL-Agent\": \"{agent_id}\", "
                             f"\"X-AL-Secret\": \"<agent_secret>\"}})",
            "raw_http": f"POST {proxy_base} + your normal provider path, with X-AL-Agent and "
                        f"X-AL-Secret added alongside {credential_header}",
        },
        "what_happens_when_over_budget": "HTTP 402 with the reason, and the provider is never "
                                         "contacted, so the money is never spent.",
        "limits": ["Only calls routed through this base_url are enforced. A caller that "
                   "bypasses the proxy is recorded but not stopped.",
                   "Dollar caps are enforced before the call; token caps are recorded after."],
    }


@mcp.tool(annotations={"title": "Agent Budget Alerts", "readOnlyHint": True,
                        "destructiveHint": False, "idempotentHint": True})
def ledger_alerts(agent_id: str, agent_secret: str = "",
                  workspace_key: str = "") -> dict:
    """Alert history for an agent: budget warnings (80% threshold) and spending spikes.

    Requires a credential: either the agent's own agent_secret or its
    workspace's workspace_key (same rule as GET /v1/alerts).

    Args:
        agent_id: unique agent identifier
        agent_secret: the agent's own secret (either this or workspace_key)
        workspace_key: the owning workspace's key (either this or agent_secret)
    """
    import json as _json
    from ledger_engine import _alerts_path, validate_agent_id, ValidationError
    try:
        validate_agent_id(agent_id)  # REST/MCP parity — same guard as GET /v1/alerts
    except ValidationError as e:
        return {"error": str(e), "error_code": "invalid_agent_id"}
    err = _authorize_read_or_error(agent_id, agent_secret, workspace_key)
    if err:
        return err
    p = _alerts_path(agent_id)
    if not p.exists():
        return {"alerts": []}
    alerts = []
    for line in p.read_text().splitlines():
        try:
            alerts.append(_json.loads(line))
        except Exception:
            continue
    _record_mcp_call()
    return {"alerts": alerts}


@mcp.tool(annotations={"title": "Check Before Spending", "readOnlyHint": True,
                        "destructiveHint": False, "idempotentHint": True})
def ledger_check_spend(agent_id: str, amount_cents: Optional[int] = None,
                       model: str = "", tokens_in: int = 0, tokens_out: int = 0,
                       agent_secret: str = "", workspace_key: str = "") -> dict:
    """Ask BEFORE you spend: may this agent spend this much right now?

    Returns allowed (true/false), a stable reason code (within_budget,
    over_monthly_cap, over_daily_cap, over_monthly_token_cap,
    over_daily_token_cap, no_budget_set, unpriced_model), a one-line message,
    the cost estimate, the price used (with its source and as_of date) and
    every budget window with cap, spent and remaining.

    Same decision the /proxy/{provider} gate enforces. Read-only: nothing is
    recorded or reserved, so record the spend with ledger_track afterwards.

    Give exactly one spend shape: amount_cents (any rail, e.g. an x402
    purchase), OR model with tokens_in/tokens_out.

    Args:
        agent_id: the agent that would spend
        amount_cents: the spend in cents, if you already know it
        model: model id to price from tokens (instead of amount_cents)
        tokens_in: expected input tokens (with model)
        tokens_out: expected output tokens, e.g. your max_tokens (with model)
        agent_secret: the agent's own secret (either this or workspace_key)
        workspace_key: the owning workspace's key (either this or agent_secret)
    """
    import spend_policy
    from ledger_engine import validate_agent_id, ValidationError
    try:
        validate_agent_id(agent_id)
    except ValidationError as e:
        return {"error": str(e), "error_code": "invalid_agent_id"}
    err = _authorize_read_or_error(agent_id, agent_secret, workspace_key)
    if err:
        return err
    try:
        result = spend_policy.check_spend(agent_id, amount_cents, model,
                                          tokens_in, tokens_out)
    except spend_policy.CheckInputError as e:
        return {"error": str(e), "error_code": e.code}
    try:
        metrics.record_event("spend_check_" + result["decision"], via="mcp")
    except Exception:
        pass
    _record_mcp_call()
    return result


def _removed_ledger_list_agents(admin_secret: str = "") -> dict:
    """OPERATOR ROUTE MOVED TO REST — see GET /v1/agents (X-Al-Admin header).

    Kept as an unregistered function so anyone grepping for the old MCP tool name
    lands here. It is NOT decorated with @mcp.tool, so it does not appear in
    tools/list. Original contract, unchanged: full cross-tenant listing of every
    agent ever claimed on this instance, requiring the operator's admin_secret.
    """
    import os as _os
    real_admin = _os.environ.get("AL_ADMIN_SECRET", "")
    if not real_admin or admin_secret != real_admin:
        return {"error": "owner only"}
    from ledger_engine import list_agents
    _record_mcp_call()
    return {"agents": list_agents()}


# ledger_list_agents was REMOVED from the MCP surface on 2026-09-26 (VALUE-BUILD-3, item 5).
# An agent's-eye audit found it advertised in the public `tools/list` to every connecting
# agent while requiring the operator's AL_ADMIN_SECRET. The leak was the advertisement, not
# the access: the tool never exposed data (it returns {"error": "owner only"} without the
# secret), but it told every arriving agent that a cross-tenant listing exists and that its
# key is called admin_secret. Probing for that key is work an agent does not need to be
# handed. The operator route still exists and is unchanged: GET /v1/agents, guarded by
# X-Al-Admin (api_server.py:425). Nothing was lost; it just stopped being advertised.
# If the MCP door is ever wanted back, gate it behind an authenticated operator identity
# rather than a secret-name in a public tool list.


@mcp.tool(annotations={"title": "Price a Call Before You Make It",
                        "readOnlyHint": True, "destructiveHint": False,
                        "idempotentHint": True})
def ledger_price(model: str, tokens_in: int = 0, tokens_out: int = 0) -> dict:
    """What will this call cost? Priced from the same table the caps use.

    Exists because ledger_track requires the CALLER to supply amount_cents, so an
    agent whose spend is capped could under-report its own cost and stay under
    the cap. This returns the number the enforcement path would use, so an agent
    can report honestly (and plan before it spends).

    Cost = (tokens_in * in_rate + tokens_out * out_rate) / 1_000_000. If the
    model has cache rates, the standard in-rate is used, which is the
    conservative direction for a spend cap.

    Every price carries the source it came from and the date it was read, and
    `verified: false` means the number was NOT read off the provider's own
    pricing page. Treat an unverified price as an estimate, not a measurement.

    Args:
        model: the model id you are about to call (e.g. gpt-4o, claude-sonnet-4)
        tokens_in: expected input tokens
        tokens_out: expected output tokens, e.g. your max_tokens
    """
    if not model or not str(model).strip():
        return {"error": "model is required", "error_code": "model_required"}
    if tokens_in < 0 or tokens_out < 0:
        return {"error": "tokens_in and tokens_out must be >= 0",
                "error_code": "invalid_tokens"}
    from spend_policy import _estimate, CheckInputError
    try:
        cents, tokens, basis, price = _estimate(None, model, tokens_in, tokens_out, None)
    except CheckInputError as e:
        return {"error": str(e), "error_code": e.code}
    if cents is None:
        # An unpriced model is a fact about the request, not a failure: say so
        # plainly instead of returning a made-up zero.
        return {"model": model, "priced": False, "reason": "unpriced_model",
                "estimate_cents": None, "tokens": tokens,
                "message": f"no price on file for '{model}'; a capped spend on it "
                           f"cannot be enforced up front"}
    try:
        metrics.record_event("price_lookup", via="mcp")
    except Exception:
        pass
    return {"model": model, "priced": True, "estimate_cents": cents,
            "tokens": tokens, "basis": basis, "price": price}


@mcp.tool(annotations={"title": "Start a Workspace", "readOnlyHint": False,
                        "destructiveHint": False, "idempotentHint": False,
                        "openWorldHint": False})
def ledger_start() -> dict:
    """Get a FREE AgentLedger workspace with no credential and no arguments —
    the MCP equivalent of opening POST /start in a browser.

    Call this FIRST if you have no credentials yet. Every other tool here
    (ledger_track, ledger_set_budget, ledger_report, ledger_alerts) needs a
    workspace_key or an agent_secret, so a caller arriving with neither must
    start here or it has nowhere to go.

    Takes NO arguments on purpose: the goal is zero friction. It returns a
    `workspace_key` (shown exactly once — it cannot be re-revealed, so store it
    before continuing) which you then send as `workspace_key` on your first
    ledger_track for a NEW agent_id. That first write returns the agent's own
    `agent_secret`, which authenticates every write after it.

    The free tier includes every rail, enforced budget caps, alerts, reports and
    the MCP server, capped at 3 agents per workspace. Minting is rate-limited
    per caller IP, the same limit the human door uses.

    Prefer to pay? POST /v1/billing/x402 with a wallet-signed payment needs no
    human and buys 24h of Pro (unlimited agents).
    """
    from fastmcp.server.dependencies import get_http_request
    import api_server

    # Reuse the human door's limiter rather than bypassing it: an
    # unauthenticated mint costs disk, and unmetered it is an abuse vector
    # (the reason _START_MAX_PER_IP exists at all).
    try:
        request = get_http_request()
    except Exception:
        request = None

    if request is not None:
        allowed = api_server._start_mint_allowed(request)
    else:  # pragma: no cover — no HTTP context (stdio/in-process call)
        allowed = True

    if not allowed:
        _record_mcp_call()
        return {
            "error": ("Mint rate limit reached for this caller — the free door "
                      "allows 3 workspaces per day per IP. Pay via "
                      "POST /v1/billing/x402 for an immediate workspace, or "
                      "retry tomorrow."),
            "error_code": "mint_rate_limited",
        }

    import workspace_engine
    workspace_id, raw_key = workspace_engine.create_workspace(grant_scarcity=False)

    try:
        metrics.record_onboarding("workspace_minted", workspace_id,
                                  plan="free", grant_scarcity=False, via="mcp")
        if raw_key:
            metrics.record_onboarding("key_revealed", workspace_id)
    except Exception:
        pass

    _record_mcp_call()
    if not raw_key:
        # create_workspace is idempotent per identity; with no identity passed
        # it always mints fresh, so this should not happen. Fail loudly rather
        # than hand back a keyless "success".
        return {
            "workspace_id": workspace_id,
            "error": ("No key was issued for this workspace. Request a new one "
                      "by calling ledger_start again."),
            "error_code": "no_key_issued",
        }

    return {
        "workspace_id": workspace_id,
        "workspace_key": raw_key,
        "key_shown_once": True,
        "plan": "free",
        "agents_included": 3,
        "next_steps": [
            "Store workspace_key now — it cannot be shown again.",
            "Call ledger_track with a NEW agent_id and this workspace_key; the "
            "response carries that agent's agent_secret, used for every write "
            "after the first.",
            "Call ledger_set_budget to set caps that are ENFORCED (a call over "
            "budget is refused before the provider is contacted).",
        ],
        "docs": "Call ledger_api_docs with topic='quickstart' for the full walkthrough.",
    }


@mcp.tool(annotations={"title": "AgentLedger API Docs", "readOnlyHint": True,
                        "destructiveHint": False, "idempotentHint": True})
def ledger_api_docs(topic: str = "") -> dict:
    """Self-serve documentation for AgentLedger — quickstart, MCP tools, REST
    endpoints, budget caps, error codes, and idempotency usage, as markdown.

    Args:
        topic: "quickstart" | "mcp" | "rest" | "budget" | "errors" | "idempotency" | "all"
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


@mcp.tool(annotations={"title": "AgentLedger Recipes", "readOnlyHint": True,
                        "destructiveHint": False, "idempotentHint": True})
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


def get_asgi_app():
    """Return the streamable-http ASGI app for mounting into api_server."""
    return mcp.http_app(path="/", transport="streamable-http")


# --- Product Skill Surface (SEP-2640 shape) --------------------------------
# Serves this product's SKILL.md as a skill:// resource so a connecting agent
# gets the manual with the tools. Registration is non-fatal by design: if the
# skill dir is missing the MCP surface must still mount. The SEP-2640 extension
# capability is deliberately NOT advertised — see product_skills.py for the gate.
try:
    from product_skills_adapter import register_product_skills
    _SKILL_REPORT = register_product_skills(mcp, "agent-ledger", "skill")
except Exception as _skill_exc:  # pragma: no cover - never break the MCP mount
    _SKILL_REPORT = {"skipped": str(_skill_exc)}
    import logging
    logging.warning(f"product-skill registration skipped: {_skill_exc}")


if __name__ == "__main__":
    mcp.run(transport="http")