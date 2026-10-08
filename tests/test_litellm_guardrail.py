"""Guardrail contract tests — LiteLLM calls into AgentLedger via the hosted
MCP surface. The _LedgerMCP transport is faked; what is under test is the
guardrail's decision logic: block on denied, allow on approved, track on
response, identity resolution, and fail-open posture.

The module must also import without litellm installed — the integration
ships to users who may read/copy the file outside a litellm env.
"""
import asyncio
import sys
import types

import pytest

sys.path.insert(0, "integrations/litellm")
import agentledger_guardrail as g  # noqa: E402


class FakeMCP:
    """Stands in for _LedgerMCP: records (tool, args) and returns scripted
    verdicts keyed by tool name."""
    def __init__(self, responses=None, fail=None):
        self.calls = []
        self.responses = responses or {}
        self.fail = fail

    async def call(self, tool, args):
        self.calls.append((tool, args))
        if self.fail and tool in self.fail:
            raise RuntimeError("ledger unreachable")
        return self.responses.get(tool, {})


def _guardrail(fake, **kw):
    gr = g.AgentLedgerBudgetGuardrail(api_base="http://test/mcp/",
                                      workspace_key="wk_live_test",
                                      agent_secret="ags_test", **kw)
    gr._mcp = fake
    return gr


def _req(agent_id="agent-coder-1", model="gpt-5"):
    return {"model": model, "max_tokens": 500,
            "messages": [{"role": "user", "content": "hi" * 50}],
            "metadata": {"agentledger_agent_id": agent_id}}


def test_imports_without_litellm():
    assert "litellm" not in sys.modules or g.CustomGuardrail is not None
    assert issubclass(g.AgentLedgerBudgetGuardrail, g.CustomGuardrail)


def test_pre_call_blocks_when_budget_denies():
    fake = FakeMCP({"ledger_check_spend": {
        "allowed": False, "reason": "over_monthly_cap",
        "message": "$50.00 cap, $49.80 spent"}})
    gr = _guardrail(fake)
    with pytest.raises(Exception, match="over_monthly_cap"):
        asyncio.run(gr.apply_guardrail({"texts": []}, _req(), "request"))
    tool, args = fake.calls[0]
    assert tool == "ledger_check_spend"
    assert args["agent_id"] == "agent-coder-1"
    assert args["model"] == "gpt-5"
    assert args["workspace_key"] == "wk_live_test"
    assert args["agent_secret"] == "ags_test"


def test_pre_call_allows_when_budget_approves():
    fake = FakeMCP({"ledger_check_spend": {"allowed": True,
                                           "reason": "within_budget"}})
    gr = _guardrail(fake)
    out = asyncio.run(gr.apply_guardrail({"texts": ["x"]}, _req(), "request"))
    assert out["texts"] == ["x"]


def test_post_call_tracks_real_cost():
    fake = FakeMCP()
    gr = _guardrail(fake)
    logging_obj = types.SimpleNamespace(response_cost=0.0512,
                                        model_call_details={})
    asyncio.run(gr.apply_guardrail({"texts": []}, _req(), "response",
                                   logging_obj=logging_obj))
    tool, args = fake.calls[0]
    assert tool == "ledger_track"
    assert args["rail"] == "api_key"
    assert args["amount_cents"] == 5
    assert args["service"] == "litellm:gpt-5"


def test_agent_id_resolution_order():
    fake = FakeMCP({"ledger_check_spend": {"allowed": True}})
    gr = _guardrail(fake)
    # explicit tag wins
    assert gr._resolve_agent_id(
        {"metadata": {"agentledger_agent_id": "tagged",
                      "user_api_key_alias": "alias"}}) == "tagged"
    # falls back through key alias / user id
    assert gr._resolve_agent_id(
        {"metadata": {"user_api_key_alias": "alias"}}) == "alias"
    assert gr._resolve_agent_id(
        {"metadata": {"user_api_key_user_id": "u7"}}) == "u7"
    # configured default, then last-resort
    gr2 = _guardrail(FakeMCP(), agent_id="fleet-default")
    assert gr2._resolve_agent_id({"metadata": {}}) == "fleet-default"
    assert gr._resolve_agent_id({"metadata": {}}) == "litellm-unknown-agent"


def test_fails_open_when_ledger_unreachable():
    fake = FakeMCP(fail={"ledger_check_spend"})
    gr = _guardrail(fake)
    # must NOT raise — gateway survives a ledger outage
    out = asyncio.run(gr.apply_guardrail({"texts": ["x"]}, _req(), "request"))
    assert out["texts"] == ["x"]


def test_fails_closed_when_configured():
    fake = FakeMCP(fail={"ledger_check_spend"})
    gr = _guardrail(fake, block_on_ledger_error=True)
    with pytest.raises(Exception, match="AgentLedger budget check failed"):
        asyncio.run(gr.apply_guardrail({"texts": []}, _req(), "request"))


def test_track_never_raises_on_ledger_error():
    fake = FakeMCP(fail={"ledger_track"})
    gr = _guardrail(fake)
    logging_obj = types.SimpleNamespace(response_cost=0.5,
                                        model_call_details={})
    out = asyncio.run(gr.apply_guardrail({"texts": ["x"]}, _req(),
                                         "response", logging_obj=logging_obj))
    assert out["texts"] == ["x"]
