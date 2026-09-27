"""ledger_price, and the removal of the operator tool from the public MCP surface.

Two findings from the agent's-eye audit, both about what an agent can see:

1. `ledger_track` requires the CALLER to supply amount_cents, so an agent whose
   spend is capped could under-report its own cost and stay under its cap. A
   price lookup gives it the number the enforcement path would actually use.

2. `ledger_list_agents` required the operator's AL_ADMIN_SECRET yet was
   ADVERTISED in the public tools/list to every connecting agent. The leak was
   the advertisement, not the access: it never returned data without the secret,
   but it told every arriving agent that a cross-tenant listing exists and what
   its key is called. Probing for that key is work nobody needs handed to them.
"""
import asyncio
import inspect

import pytest

import al_mcp_http


def _registered_tool_names():
    lister = getattr(al_mcp_http.mcp, "_list_tools", None)
    assert lister is not None, "fastmcp exposes no _list_tools; update this test"
    result = lister()
    tools = asyncio.run(result) if hasattr(result, "__await__") else result
    return {getattr(t, "name", str(t)) for t in tools}


# ── item 4: ledger_price ────────────────────────────────────────────────────

def test_price_tool_is_registered():
    """It must be a real MCP tool, not just a function in the module."""
    assert "ledger_price" in _registered_tool_names()


def test_a_known_model_is_priced_and_carries_its_provenance():
    """A price without a source and a date is a number pretending to be a fact."""
    import proxy as proxy_core
    model = next(iter(proxy_core.prices()))
    r = al_mcp_http.ledger_price(model, tokens_in=1000, tokens_out=1000)
    assert r["priced"] is True, r
    assert isinstance(r["estimate_cents"], int) and r["estimate_cents"] > 0
    assert r["price"] is not None
    assert "verified" in r["price"] and "as_of" in r["price"]


def test_more_tokens_never_costs_less():
    """Monotonicity: a pricer that can go down as tokens go up is broken."""
    import proxy as proxy_core
    model = next(iter(proxy_core.prices()))
    small = al_mcp_http.ledger_price(model, 100, 100)["estimate_cents"]
    big = al_mcp_http.ledger_price(model, 10000, 10000)["estimate_cents"]
    assert big >= small


def test_an_unpriced_model_says_so_instead_of_returning_zero():
    """The whole point is honest accounting: unknown is not free."""
    r = al_mcp_http.ledger_price("definitely-not-a-real-model-xyz", 1000, 1000)
    assert r["priced"] is False
    assert r["estimate_cents"] is None
    assert r["reason"] == "unpriced_model"


def test_the_price_matches_what_the_enforcement_path_would_use():
    """It must reuse spend_policy's estimator, not a second pricer that can drift."""
    import proxy as proxy_core
    from spend_policy import _estimate
    model = next(iter(proxy_core.prices()))
    mine = al_mcp_http.ledger_price(model, 1234, 567)["estimate_cents"]
    theirs, _tokens, _basis, _price = _estimate(None, model, 1234, 567, None)
    assert mine == theirs


def test_negative_tokens_and_a_missing_model_are_refused():
    assert al_mcp_http.ledger_price("gpt-4o", -1, 0)["error_code"] == "invalid_tokens"
    assert al_mcp_http.ledger_price("", 1, 1)["error_code"] == "model_required"


def test_the_price_tool_requires_no_credential_read():
    """It is a public price card lookup; demanding a secret would make it useless
    to the caller it exists for (an agent planning a spend)."""
    sig = inspect.signature(al_mcp_http.ledger_price)
    for bad in ("agent_secret", "workspace_key", "admin_secret"):
        assert bad not in sig.parameters


# ── item 5: the operator tool is off the public surface ─────────────────────

def test_the_operator_tool_is_not_advertised_to_arriving_agents():
    """The finding: a cross-tenant listing and its key name were in the public
    tools/list. If this fails, the advertisement is back."""
    names = _registered_tool_names()
    assert "ledger_list_agents" not in names, (
        f"the operator tool is advertised again; tools/list = {sorted(names)}")


def test_the_operator_function_still_exists_for_greppability():
    """Removed from the surface, not from the codebase: a future reader grepping
    for the old name must find the explainer, not silence."""
    assert hasattr(al_mcp_http, "_removed_ledger_list_agents")


def test_the_operator_route_still_refuses_without_the_secret(monkeypatch):
    """Nothing was lost: the operator path still gates correctly."""
    monkeypatch.setenv("AL_ADMIN_SECRET", "correct-horse")
    assert al_mcp_http._removed_ledger_list_agents("wrong") == {"error": "owner only"}
    assert al_mcp_http._removed_ledger_list_agents("") == {"error": "owner only"}


def test_the_rest_operator_route_is_untouched():
    """The alternative door for the operator must still exist."""
    src = open(al_mcp_http.__file__).read()
    assert "GET /v1/agents" in src
