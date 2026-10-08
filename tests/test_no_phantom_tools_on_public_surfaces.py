"""No public surface may name a tool the registry does not serve — and no
served or shipped surface may name the withdrawn tool's admin credential.

Why this exists: the 2026-10-07 audit zeroed ``ledger_list_agents`` on four
discovery surfaces (llms.txt, /v1/products, agent.json, mcp.json) — and a live
``tools/call`` then found the same phantom, plus the literal string
``admin_secret``, still served by the ``ledger_api_docs`` tool itself, the
registry server-card, the /agent-ledger and /quickstart pages, and both
packaged READMEs. Auditing a list of surfaces one at a time is how the drift
shipped; the invariant has to hold on every public-facing text at once.

The rule: every ``ledger_*`` token on a public surface must be the name of a
tool in the live registry (or an explicitly whitelisted non-tool token, e.g.
the ``ledger_engine`` module). ``admin_secret`` must appear nowhere public —
advertising it was the reason the tool was withdrawn (al_mcp_http.py).
"""
import asyncio
import inspect
import json
import re
from pathlib import Path

import al_mcp_http
import docs_content
import site_pages

ROOT = Path(__file__).resolve().parent.parent

# ledger_* tokens that are NOT MCP tools and may legitimately appear in docs.
# ledger_engine.py is the storage module named in the README architecture note.
NON_TOOL_TOKENS = {"ledger_engine"}


def _live_tool_names() -> set:
    result = al_mcp_http.mcp._list_tools()
    tools = asyncio.run(result) if hasattr(result, "__await__") else result
    return {getattr(t, "name", str(t)) for t in tools}


def _surfaces() -> dict:
    return {
        # served payloads — what a caller or crawler actually receives
        "ledger_api_docs(all topics)": "\n\n".join(
            docs_content.DOCS_TOPICS.values()
        ),
        "ledger_examples recipes": "\n".join(docs_content.RECIPES.values()),
        "server-card.json": (ROOT / "server-card.json").read_text(),
        "status.html (/status + /agent-ledger)": (ROOT / "status.html").read_text(),
        "site_pages page constants": "\n".join(
            v for v in vars(site_pages).values()
            if isinstance(v, str) and len(v) > 200
        ),
        # shipped/distributed docs — the GitHub front page, the next PyPI and
        # npm publishes, and the skills installed into agent contexts
        "README.md": (ROOT / "README.md").read_text(),
        "npm/README.md": (ROOT / "npm" / "README.md").read_text(),
        "skill/agent-ledger/SKILL.md": (ROOT / "skill" / "agent-ledger" / "SKILL.md").read_text(),
        "integrations/claude-code/SKILL.md": (
            ROOT / "integrations" / "claude-code" / "SKILL.md"
        ).read_text(),
    }


def test_no_phantom_tool_names_on_any_public_surface():
    live = _live_tool_names()
    failures = {}
    for name, text in _surfaces().items():
        phantom = set(re.findall(r"ledger_[a-z_]+", text)) - live - NON_TOOL_TOKENS
        if phantom:
            failures[name] = sorted(phantom)
    assert not failures, (
        "public surfaces name tools the registry does not serve: "
        f"{failures} — either the tool exists and the registry is missing it, "
        "or the copy names a phantom (this is the drift this test exists for)"
    )


def test_no_admin_secret_hint_on_any_public_surface():
    offenders = [name for name, text in _surfaces().items()
                 if "admin_secret" in text]
    assert not offenders, (
        f"the withdrawn operator credential is still named on: {offenders} — "
        "telling callers the key exists is why ledger_list_agents was removed"
    )


def test_server_card_tools_equal_live_registry():
    """The committed card is a snapshot; the test makes the snapshot honest."""
    card = json.loads((ROOT / "server-card.json").read_text())
    card_names = {t["name"] for t in card["tools"]}
    assert card_names == _live_tool_names(), (
        f"server-card.json drifted from tools/list: "
        f"card-only={sorted(card_names - _live_tool_names())} "
        f"live-only={sorted(_live_tool_names() - card_names)} — regenerate the "
        "card from al_mcp_http.mcp._list_tools()"
    )
