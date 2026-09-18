#!/usr/bin/env python3
"""The front door must WORK for an agent that arrives at /mcp/ (D-1375).

WHY THIS EXISTS (2026-09-18)
----------------------------
`reach` shows /mcp/ at 368 distinct visitors — the strongest machine-discovery
signal on the product. Probing the journey those agents actually take found two
dead ends:

1. `tools/list` returns 11 tools and EVERY one of them requires a credential the
   arriving agent does not have (ledger_track -> "Missing required argument
   agent_id"; ledger_report likewise; ledger_list_agents -> "owner only").
   There was no tool that mints a workspace, so an agent discovering us through
   MCP could read the manual and then had nowhere to go.

2. Three live surfaces told agents "GET /start returns a workspace_key (shown
   once)". GET /start deliberately does NOT mint — its docstring says a mint on
   GET would let a crawler or link-preview bot burn a launch-window workspace
   and orphan a key nobody saw. test_start_flow.py already pins that. So the
   machine-readable front door was misdirecting every agent that read it.

These tests pin the fix: a keyless mint is reachable from MCP, it obeys the same
rate limit as the human door, and no surface claims GET mints.
"""
import inspect
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# ── 1. The mint tool exists, and needs no credential ────────────────────────

def test_mcp_exposes_a_keyless_mint_tool():
    """An agent arriving at /mcp/ must be able to get a workspace without a key.

    This is the whole point of D-1375: 368 agents reached the MCP door and the
    door had no handle.
    """
    import al_mcp_http as al

    assert hasattr(al, "ledger_start"), (
        "al_mcp_http has no ledger_start — an agent arriving at /mcp/ still has "
        "no way to mint a workspace without leaving MCP (D-1375)")
    fn = al.ledger_start
    sig = inspect.signature(fn)
    required = [
        p.name for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    ]
    assert required == [], (
        f"ledger_start must take NO required arguments — a caller with no "
        f"credential has nothing to pass. Required: {required}")


def test_mint_tool_is_registered_in_the_mcp_server():
    """It must be an actual MCP tool, not just a function in the module.

    Reads fastmcp's real tool list (`FastMCP._list_tools`) so this asserts the
    registration rather than the presence of a function. If a future fastmcp
    version renames that private hook, the test FAILS (not skips) — a skipped
    check here would silently stop verifying the thing that matters.
    """
    import asyncio

    import al_mcp_http as al

    lister = getattr(al.mcp, "_list_tools", None)
    assert lister is not None, (
        "fastmcp exposes no _list_tools on FastMCP — cannot verify that "
        "ledger_start is registered; update this test for the new API instead "
        "of removing the check (D-1375)")
    result = lister()
    tools = asyncio.run(result) if hasattr(result, "__await__") else result
    names = {getattr(t, "name", str(t)) for t in tools}
    assert "ledger_start" in names, (
        f"ledger_start is not a registered MCP tool; registered: {sorted(names)}")


def test_mint_tool_does_not_require_a_workspace_key():
    """Guard against 'fixing' it by making the key a required argument."""
    import al_mcp_http as al

    src = inspect.getsource(al.ledger_start)
    sig = inspect.signature(al.ledger_start)
    for bad in ("agent_secret", "workspace_key", "admin_secret"):
        assert bad not in sig.parameters, (
            f"ledger_start must not require '{bad}' — the whole point is that "
            f"the caller has no credential yet (D-1375)")
    # It must not reject the caller for lacking a key either.
    assert "workspace_key_required" not in src, (
        "ledger_start must not answer a credential-less caller with "
        "workspace_key_required — that is the dead end it exists to remove")


def test_mint_tool_shares_the_human_door_rate_limit():
    """The mint is an unauthenticated write. It MUST be metered.

    POST /start is capped at 3 mints per IP per 24h because "one loop creates
    unlimited workspaces". If the MCP mint bypassed that, this change would
    turn a dead end into an abuse vector — strictly worse.
    """
    import al_mcp_http as al

    src = inspect.getsource(al.ledger_start)
    assert ("_start_mint_allowed" in src or "start_mint_allowed" in src
            or "rate" in src.lower()), (
        "ledger_start does not appear to rate-limit. The human door caps minting "
        "at 3/IP/day; an unmetered MCP mint is worse than the dead end (D-1375)")


# ── 2. No surface may claim GET /start mints ────────────────────────────────

def test_no_rendered_surface_misleads_an_agent_about_GET_start(tmp_path, monkeypatch):
    """Check the text agents actually READ, one claim at a time.

    The source-line version of this check was blindable: a caveat belonging to a
    NEIGHBOURING mention fell inside the search window and vouched for a line
    that had none (caught by injecting a fresh misdirection and watching it pass).
    So this renders the real surfaces and requires that any SENTENCE naming
    GET /start carries its own caveat — a sentence is the natural unit of a
    claim, so an unrelated caveat cannot vouch for it.

    Surfaces: /llms.txt (what discovery agents read) and the OpenAPI document
    (what codegen and registries read). GET /start deliberately does NOT mint.
    """
    import json as J
    import re as _re

    from fastapi.testclient import TestClient

    monkeypatch.setenv("AGENT_LEDGER_DATA", str(tmp_path))
    import api_server

    negatives = ("does not mint", "only renders", "renders the form",
                 "renders the start form", "does not create", "does not issue",
                 "does not get", "no key is issued")

    def claims_from_text(text):
        """Atomic claims from a text surface: one per sentence."""
        flat = _re.sub(r"\s+", " ", text)
        return [c.strip() for c in _re.split(r"(?<=[.!?])\s+", flat) if c.strip()]

    def claims_from_json(text):
        """Atomic claims from a JSON surface: one per string VALUE.

        JSON surfaces here are single-line documents, so a line- or window-based
        scan is worthless: every mention lands in one giant "line", and one
        correct caveat anywhere in it vouches for every other mention. Checking
        each string value separately is the only unit that can't be gamed that
        way (verified by injecting a misdirection into /.well-known/x402 and
        watching the window version pass it).
        """
        out = []

        def walk(node):
            if isinstance(node, str):
                out.append(node)
            elif isinstance(node, dict):
                for k, v in node.items():
                    out.append(str(k))
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        try:
            walk(J.loads(text))
        except Exception:
            out = claims_from_text(text)
        return [c.strip() for c in out if c.strip()]

    with TestClient(api_server.app) as client:
        surfaces = {}
        # Every surface that actually mentions GET /start (probed 2026-09-18):
        # /llms.txt 5, /agents.txt 5, /.well-known/x402 2, /openapi.json 1.
        # A guard reading only one is blind to a misdirection placed in another.
        for path in ("/llms.txt", "/openapi.json", "/agents.txt", "/.well-known/x402"):
            r = client.get(path)
            if r.status_code == 200:
                surfaces[path] = r.text
        assert surfaces, "no readable discovery surface — cannot verify the front door"
        missing = [p for p in ("/llms.txt", "/openapi.json") if p not in surfaces]
        assert not missing, (
            f"core discovery surfaces did not render ({missing}); the front door "
            f"cannot be verified")

        offenders = []
        for name, text in surfaces.items():
            claims = (claims_from_json(text) if name.endswith(".json")
                      else claims_from_text(text))
            for c in claims:
                if "GET /start" not in c and "GET  /start" not in c:
                    continue
                low = c.lower()
                if any(neg.lower() in low for neg in negatives):
                    continue
                offenders.append(f"{name}: {c[:170]}")

    assert offenders == [], (
        "these rendered claims name GET /start without stating that it does NOT "
        "mint — an agent acting on one calls a route that only returns a form "
        "and never gets a credential (D-1375):\n  " + "\n  ".join(offenders))


def test_docs_do_not_tell_agents_to_use_GET_start_for_a_workspace():
    """The second misdirection: 'Use GET /start instead if you have no wallet'.

    Same defect as above in different words — an agent following it calls a
    route that only renders a form, so it never gets a credential. Caught
    separately because the phrasing differs (D-1375).
    """
    text = (REPO / "api_server.py").read_text()
    bad = re.compile(r"use\s+GET\s+/start\s+instead", re.I)
    m = bad.search(text)
    assert m is None, (
        f"api_server.py still says 'Use GET /start instead...' (hit: "
        f"{m.group(0) if m else ''}) — a wallet-less agent following that gets a "
        f"form, not a key. It must point at POST /start (D-1375)")


def test_docs_point_at_post_start_or_the_x402_door():
    """The corrected wording must name a path that actually works."""
    text = (REPO / "api_server.py").read_text()
    assert "POST /start" in text, (
        "api_server.py names no working keyless mint path; POST /start is the "
        "one that mints (D-1375)")


# ── 3. The mint must actually WORK end to end ───────────────────────────────

def test_mint_tool_issues_a_key_that_can_claim_an_agent(tmp_path, monkeypatch):
    """The functional proof, not just the shape.

    A keyless call must produce a real workspace_key, and that key must be able
    to claim a NEW agent_id — otherwise the MCP front door is still a dead end,
    just a more polite one.
    """
    monkeypatch.setenv("AGENT_LEDGER_DATA", str(tmp_path))
    import al_mcp_http as al

    result = al.ledger_start()
    assert "error" not in result, f"ledger_start errored: {result}"
    key = result.get("workspace_key", "")
    assert key.startswith("wk_"), (
        f"ledger_start returned no usable workspace_key (got {key!r}) — an agent "
        f"would still be stuck (D-1375)")
    assert result.get("workspace_id"), "no workspace_id returned"

    # The key must be a working credential, not just a string.
    from ledger_engine import ensure_agent_secret
    secret, created = ensure_agent_secret(
        "d1322-probe-agent", None, workspace_key=key)
    assert created is True, (
        "the minted workspace_key could not claim a new agent — the front door "
        "hands out a key that does not work")
    assert secret, "claiming returned no agent_secret"


# ── 4. End-to-end through the REAL mounted app ──────────────────────────────

def test_live_mcp_surface_serves_the_mint(tmp_path, monkeypatch):
    """The whole path an arriving agent takes, exercised through api_server.app.

    Module-level presence is not enough: the tool must be reachable on the
    mounted /mcp/ surface that the 368 visitors actually hit. Uses the app's
    real combined lifespan (api_server.py:1887) — without it fastmcp's session
    manager is uninitialized and the call fails for reasons unrelated to D-1375.
    """
    import json as J
    import re

    from fastapi.testclient import TestClient

    monkeypatch.setenv("AGENT_LEDGER_DATA", str(tmp_path))
    import api_server

    with TestClient(api_server.app) as client:
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        init = client.post("/mcp/", headers=h, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "d1322-probe", "version": "1"}}})
        assert init.status_code == 200, f"initialize failed: {init.status_code}"
        sid = init.headers.get("mcp-session-id")
        assert sid, "no mcp-session-id returned"

        h2 = dict(h, **{"mcp-session-id": sid})
        listing = client.post("/mcp/", headers=h2,
                              json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = J.loads(re.search(r"data: (\{.*\})", listing.text).group(1))
        names = [t["name"] for t in tools["result"]["tools"]]
        assert "ledger_start" in names, (
            f"the mounted /mcp/ surface does not expose ledger_start; got {names}")

        # And it must work with an empty argument object — no credential, no input.
        call = client.post("/mcp/", headers=h2, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "ledger_start", "arguments": {}}})
        payload = J.loads(re.search(r"data: (\{.*\})", call.text, re.S).group(1))
        text = payload["result"]["content"][0]["text"]
        assert "wk_live_" in text, (
            f"ledger_start via /mcp/ returned no workspace_key: {text[:300]}")


# ── 5. The REST error message is a channel too ──────────────────────────────

def test_rest_401_names_a_mint_path_that_works(tmp_path, monkeypatch):
    """The 401 an agent gets is documentation, and it was wrong.

    Probed live 2026-09-18: POST /v1/track with a new agent_id and no key
    returned 401 workspace_key_required whose message said to get a key
    "self-serve at https://aiagentscity.com/start". That URL is GET /start,
    which returns a FORM. An agent that follows the instruction exactly still
    has no credential — the same lie as the other five sites, in the one place
    an agent is guaranteed to read it (its own error).
    """
    monkeypatch.setenv("AGENT_LEDGER_DATA", str(tmp_path))
    from ledger_engine import ensure_agent_secret, WorkspaceKeyRequiredError

    with pytest.raises(WorkspaceKeyRequiredError) as exc:
        ensure_agent_secret("d1375-rest-probe", None, workspace_key=None)
    msg = str(exc.value)

    assert "POST" in msg, (
        f"the workspace_key_required message does not name the POST method, so "
        f"an agent cannot tell that GET returns a form: {msg}")
    assert "GET on that URL only renders the form" in msg or "does NOT issue" in msg, (
        f"the message points at /start without saying GET does not mint there, "
        f"which is how the original misdirection read: {msg}")


def test_cli_init_mints_via_post_not_get():
    """The CLI is a real channel: `agent-ledger init` is in our own docs.

    It must POST, because GET /start does not mint. A regression here would
    break the one-command onboarding path advertised in llms.txt.
    """
    src = (REPO / "cli.py").read_text()
    # Match the actual call, not the comment above it that also names /start.
    call = re.search(r'_remote_request\(\s*base\s*,\s*[\'"]([A-Z]+)[\'"]\s*,\s*[\'"]/start[\'"]',
                     src)
    assert call is not None, (
        "cli.py no longer calls /start — the mint path changed shape; update "
        "this check rather than deleting it")
    assert call.group(1) == "POST", (
        f"cli.py mints via {call.group(1)} /start, but GET /start returns a form "
        f"and does NOT mint — one-command onboarding would be broken (D-1375)")
