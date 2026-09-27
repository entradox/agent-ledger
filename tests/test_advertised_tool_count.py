"""The advertised MCP tool count must match the tools that actually exist.

Why this exists: the published count drifted twice without anything noticing.
It said "12 tools" after ledger_check_spend made it 13, and "13 tools" after
ledger_price and ledger_proxy_attach made it 14. Nothing was wrong with either
change on its own; the copy simply stopped tracking the code, and a page that
advertises the wrong number is the same class of defect as a page that
advertises a feature that does not exist.

This asserts the LIVE registered tool list against the number printed on the
public pages, so the next person to add a tool is told to update the copy.
"""
import asyncio
import inspect

import al_mcp_http
import city_site


def _live_tool_count() -> int:
    lister = getattr(al_mcp_http.mcp, "_list_tools", None)
    assert lister is not None, "fastmcp exposes no _list_tools; update this test"
    result = lister()
    tools = asyncio.run(result) if hasattr(result, "__await__") else result
    return len(list(tools))


def test_the_advertised_tool_count_matches_reality():
    live = _live_tool_count()
    src = inspect.getsource(city_site)
    advertised = set()
    import re
    for m in re.finditer(r"(\d+) tools", src):
        advertised.add(int(m.group(1)))
    # The changelog is dated history and legitimately keeps old numbers, so it
    # is allowed to contain other values; the count on the live cards is not.
    assert live in advertised, (
        f"the site advertises {sorted(advertised)} but {live} tools are actually "
        f"registered — update the copy (this is the drift this test exists for)")
    # Any number higher than reality is a claim we cannot honour.
    overclaims = [n for n in advertised if n > live]
    assert not overclaims, f"site claims more tools than exist: {overclaims} > {live}"


def test_the_operator_tool_is_not_counted():
    """It was removed from the surface, so it must not be in the count either."""
    lister = al_mcp_http.mcp._list_tools
    result = lister()
    tools = asyncio.run(result) if hasattr(result, "__await__") else result
    names = {getattr(t, "name", str(t)) for t in tools}
    assert "ledger_list_agents" not in names
    assert _live_tool_count() == len(names)


def test_the_new_tools_are_both_counted():
    """Each tool added for the audit's gaps must be present and counted."""
    names = set()
    lister = al_mcp_http.mcp._list_tools
    result = lister()
    tools = asyncio.run(result) if hasattr(result, "__await__") else result
    names = {getattr(t, "name", str(t)) for t in tools}
    for expected in ("ledger_check_spend", "ledger_proxy_attach", "ledger_price"):
        assert expected in names, f"{expected} is missing from tools/list"
