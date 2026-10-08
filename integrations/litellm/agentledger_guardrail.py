"""AgentLedger budget guardrail for LiteLLM Proxy.

Checks each LLM call against an AgentLedger budget BEFORE the provider is
charged (pre_call -> ledger_check_spend) and records actual cost after the
call completes (post_call -> ledger_track). LiteLLM caps the traffic inside
its own gateway; AgentLedger keeps the same per-agent books across every
other rail the agent touches (x402, MPP, direct API keys, manual entries).

Requires the AgentLedger hosted MCP surface (default
https://aiagentscity.com/mcp/) and a workspace_key + agent_secret minted by
`ledger_start` / `ledger_track`. Self-hosted AgentLedger deployments work by
pointing api_base at that deployment's /mcp/ URL.

The module imports without litellm installed so it can be tested and read
standalone; CustomGuardrail is only required at proxy runtime.
"""

import json
import os
from typing import TYPE_CHECKING, Literal, Optional

try:  # real base class inside LiteLLM proxy
    from litellm.integrations.custom_guardrail import CustomGuardrail
except Exception:  # pragma: no cover - standalone/test path
    class CustomGuardrail:  # type: ignore[no-redef]
        def __init__(self, **kwargs):
            pass

import httpx

if TYPE_CHECKING:  # pragma: no cover
    from litellm.types.utils import GenericGuardrailAPIInputs

MCP_URL_DEFAULT = "https://aiagentscity.com/mcp/"

_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "litellm-agentledger-guardrail", "version": "1"},
    },
}

_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _sse_payload(text: str) -> dict:
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return {}


class _LedgerMCP:
    """Minimal streamable-HTTP MCP client for the hosted ledger surface."""

    def __init__(self, url: str, timeout: float = 10.0):
        self._client = httpx.AsyncClient(timeout=timeout)
        self._url = url
        self._sid: Optional[str] = None

    async def _ensure_session(self) -> None:
        if self._sid:
            return
        r = await self._client.post(self._url, headers=dict(_MCP_HEADERS),
                                    json=_INIT_BODY)
        r.raise_for_status()
        self._sid = r.headers.get("mcp-session-id")
        h = dict(_MCP_HEADERS)
        if self._sid:
            h["mcp-session-id"] = self._sid
        await self._client.post(
            self._url, headers=h,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def call(self, tool: str, args: dict) -> dict:
        await self._ensure_session()
        h = dict(_MCP_HEADERS)
        if self._sid:
            h["mcp-session-id"] = self._sid
        r = await self._client.post(self._url, headers=h, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": tool, "arguments": args}})
        r.raise_for_status()
        payload = _sse_payload(r.text)
        content = (payload.get("result") or {}).get("content") or []
        if not content or "text" not in content[0]:
            raise RuntimeError(f"ledger tool {tool} returned no content: "
                               f"{r.text[:300]}")
        return json.loads(content[0]["text"])

    async def aclose(self) -> None:
        await self._client.aclose()


def _estimate_tokens(request_data: dict) -> tuple:
    """Best-effort (tokens_in, tokens_out) for the model-priced check."""
    messages = request_data.get("messages") or []
    approx_in = max(1, sum(len(str(m.get("content", ""))) for m in messages) // 4)
    max_out = int(request_data.get("max_tokens")
                  or request_data.get("max_completion_tokens") or 0)
    return approx_in, max_out


class AgentLedgerBudgetGuardrail(CustomGuardrail):
    """Block LiteLLM calls that would exceed an AgentLedger budget.

    request_data metadata -> agent_id resolution order:
      1. metadata["agentledger_agent_id"] (explicit per-key/team tag)
      2. metadata["user_api_key_alias"] / key name
      3. metadata["user_api_key_user_id"] / team id
      4. the guardrail's configured default agent_id
    """

    def __init__(self,
                 api_base: Optional[str] = None,
                 workspace_key: Optional[str] = None,
                 agent_secret: Optional[str] = None,
                 agent_id: Optional[str] = None,
                 block_on_ledger_error: bool = False,
                 **kwargs):
        self.api_base = (api_base
                         or os.getenv("AGENTLEDGER_API_BASE", MCP_URL_DEFAULT))
        self.workspace_key = (workspace_key
                              or os.getenv("AGENTLEDGER_WORKSPACE_KEY", ""))
        self.agent_secret = (agent_secret
                             or os.getenv("AGENTLEDGER_AGENT_SECRET", ""))
        self.agent_id = agent_id or os.getenv("AGENTLEDGER_AGENT_ID", "")
        # fail-open by default: a ledger outage should not take the
        # customer's LLM gateway down unless they ask for it
        self.block_on_error = block_on_ledger_error or bool(
            os.getenv("AGENTLEDGER_BLOCK_ON_ERROR"))
        self._mcp: Optional[_LedgerMCP] = None
        super().__init__(**kwargs)

    def _ledger(self) -> _LedgerMCP:
        if self._mcp is None:
            self._mcp = _LedgerMCP(self.api_base)
        return self._mcp

    def _resolve_agent_id(self, request_data: dict) -> str:
        meta = request_data.get("metadata") or {}
        for key in ("agentledger_agent_id", "user_api_key_alias",
                    "user_api_key_user_id", "user_api_key_team_id"):
            v = meta.get(key)
            if v:
                return str(v)
        return self.agent_id or "litellm-unknown-agent"

    def _auth_args(self, agent_id: str) -> dict:
        args = {"agent_id": agent_id}
        if self.agent_secret:
            args["agent_secret"] = self.agent_secret
        if self.workspace_key:
            args["workspace_key"] = self.workspace_key
        return args

    async def apply_guardrail(
        self,
        inputs: "GenericGuardrailAPIInputs",
        request_data: dict,
        input_type: Literal["request", "response"],
        logging_obj=None,
    ) -> "GenericGuardrailAPIInputs":
        if input_type == "request":
            await self._check_budget(request_data)
        else:
            await self._record_spend(request_data, logging_obj)
        return inputs

    async def _check_budget(self, request_data: dict) -> None:
        agent_id = self._resolve_agent_id(request_data)
        model = str(request_data.get("model") or "")
        tokens_in, tokens_out = _estimate_tokens(request_data)
        try:
            verdict = await self._ledger().call(
                "ledger_check_spend",
                {**self._auth_args(agent_id), "model": model,
                 "tokens_in": tokens_in, "tokens_out": tokens_out})
        except Exception as e:
            if self.block_on_error:
                raise Exception(f"AgentLedger budget check failed: {e}")
            return
        if verdict.get("error"):
            raise Exception(
                f"AgentLedger rejected spend check: {verdict.get('error')}")
        if not verdict.get("allowed", True):
            raise Exception(
                f"AgentLedger budget exceeded for {agent_id}: "
                f"{verdict.get('reason')} — {verdict.get('message', '')}")

    async def _record_spend(self, request_data: dict, logging_obj) -> None:
        try:
            cost_usd = None
            if logging_obj is not None:
                cost_usd = getattr(logging_obj, "response_cost", None)
            if cost_usd is None:
                try:
                    import litellm  # type: ignore
                    cost_usd = litellm.completion_cost(
                        completion_response=getattr(logging_obj, "model_call_details", {}).get("result"))
                except Exception:
                    cost_usd = None
            if cost_usd is None:
                return  # never fabricate a number
            agent_id = self._resolve_agent_id(request_data)
            model = str(request_data.get("model") or "unknown")
            await self._ledger().call(
                "ledger_track",
                {**self._auth_args(agent_id), "rail": "api_key",
                 "amount_cents": max(1, round(float(cost_usd) * 100)),
                 "service": f"litellm:{model}"})
        except Exception:
            return  # recording must never break the response path


__all__ = ["AgentLedgerBudgetGuardrail"]
