"""ledger_proxy_attach: the brake must be reachable from MCP.

Why this exists: an independent agent's-eye audit found the ONLY money-stopping
feature (pre-call refusal in /proxy/{provider}) was unreachable from the MCP
surface that agents actually connect through — tools/list had 12 tools and none
was proxy- or billing-related. An MCP-native agent got post-hoc bookkeeping and
zero enforcement. This tool is the fix, so these tests guard the thing that
makes the product worth anything to an agent.

The tool must also never invent its own facts: it asks the proxy module which
providers actually exist and which credential header each one needs, because a
hard-coded list would advertise a rail this deployment has not enabled.
"""
import os

import pytest

import al_mcp_http


@pytest.fixture()
def tool(monkeypatch):
    """The tool's raw function, with the credential check stubbed out.

    The authorization rule itself is covered by its own tests; this file is
    about whether the tool returns the truth about the proxy.
    """
    monkeypatch.setattr(al_mcp_http, "_authorize_read_or_error",
                        lambda *a, **k: None)
    return al_mcp_http.ledger_proxy_attach


def test_the_tool_is_on_the_mcp_surface():
    """The audit's finding was reachability. If this name is gone, so is the fix."""
    assert hasattr(al_mcp_http, "ledger_proxy_attach")


def test_it_returns_a_usable_openai_base_url(tool):
    r = tool("probe-agent", "openai", "sec", "")
    assert "error" not in r, r
    assert r["base_url"].endswith("/proxy/openai")
    assert r["base_url"].startswith("https://")
    # The two headers the proxy actually demands (routes_proxy._proxy_identity).
    assert set(r["required_headers"]) == {"X-AL-Agent", "X-AL-Secret"}
    assert r["headers"]["X-AL-Agent"] == "probe-agent"


def test_the_proxy_requires_exactly_the_headers_this_tool_hands_back(tool):
    """Guard against header drift: the tool must not name a header the proxy ignores."""
    import routes_proxy
    src = routes_proxy.__file__
    body = open(src).read()
    for h in tool("probe-agent", "openai", "sec", "")["required_headers"]:
        assert h.lower() in body.lower(), (
            f"tool advertises {h} but the proxy never reads it")


def test_it_can_attach_every_provider_the_proxy_actually_supports(tool):
    """No hard-coded provider list: whatever the proxy enables must be attachable."""
    import proxy as proxy_core
    for provider in proxy_core.provider_ids():
        r = tool("probe-agent", provider, "sec", "")
        assert "error" not in r, f"{provider} is enabled but not attachable: {r}"
        assert r["base_url"].endswith(f"/proxy/{provider}")


def test_an_unknown_provider_is_refused_and_lists_the_real_ones(tool):
    """A tool that accepts a nonexistent rail hands the agent a dead base_url."""
    import proxy as proxy_core
    r = tool("probe-agent", "definitely-not-a-provider", "sec", "")
    assert r["error_code"] == "unsupported_provider"
    for provider in proxy_core.provider_ids():
        assert provider in r["error"]


def test_the_credential_header_comes_from_the_proxy_not_from_this_file(tool):
    """The agent needs to know where to put its provider key; do not guess it."""
    import proxy as proxy_core
    for provider in proxy_core.provider_ids():
        expected = (proxy_core.provider_config(provider) or {}).get("credential_header")
        got = tool("probe-agent", provider, "sec", "")["provider_credential_header"]
        if expected:
            assert got == expected


def test_it_does_not_pretend_the_credential_moves(tool):
    """False comfort about key handling is a liability claim, not copy."""
    r = tool("probe-agent", "openai", "sec", "")
    assert "never stores" in r["do_not_move"] or "never writes" in r["do_not_move"]
    # It must be explicit that a caller who skips the proxy is not stopped.
    assert any("bypass" in limit for limit in r["limits"])


def test_the_base_url_honours_the_public_origin_env(monkeypatch, tool):
    """Hard-coding the production host would break every non-prod deployment."""
    monkeypatch.setenv("AL_PUBLIC_BASE_URL", "https://staging.example.test")
    r = tool("probe-agent", "openai", "sec", "")
    assert r["base_url"] == "https://staging.example.test/proxy/openai"


def test_an_invalid_agent_id_is_refused_before_anything_else(tool):
    r = tool("../etc/passwd", "openai", "sec", "")
    assert r["error_code"] == "invalid_agent_id"
