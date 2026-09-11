#!/usr/bin/env python3
"""Docs must not misinform callers (I4).

Post-workspace-identity, the manifests and marketing copy still claimed
"no signup — the first write claims it" (new claims now require a
workspace_key) and that the MCP reads were open (they are now
credential-gated, same as REST). These tests pin the corrected state so it
can't silently rot back.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

REPO = Path(__file__).resolve().parent.parent

# Surfaces a caller actually reads before integrating. The two .py files are
# here because their docstrings ARE the integration docs for the MCP surface
# and the claim gate — an agent reads the tool docstring, not README.md, and
# both had drifted into claiming reads need no credential / claims need no
# signup after the workspace-identity phase changed both.
DOC_FILES = ("README.md", "status.html", "recipes.md",
             "al_mcp_http.py", "routes_agents.py")

# Claims that are no longer true. "no login" is deliberately absent: the x402
# path genuinely requires none. "no signup" was on this list until D-1162 and
# has been taken off deliberately, not quietly: it went stale when the only
# self-serve source of a workspace_key was a paid x402 mint, so telling a human
# "no signup" pointed them at a wall. GET+POST /start now mints a workspace with
# no email, no password and no card, so the claim is exact again — and
# tests/test_start_flow.py pins the mechanism, so it cannot rot into a lie twice.
STALE_CLAIMS = ("first write claims it",
                "which needs no secret", "needs no secret",
                "no secret needed")


@pytest.fixture(autouse=True)
def data_dir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="agent-ledger-docs-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    yield
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.parametrize("filename", DOC_FILES)
def test_no_stale_no_signup_claims_in_docs(filename):
    text = (REPO / filename).read_text()
    for claim in STALE_CLAIMS:
        assert claim not in text, f"{filename} still claims: {claim!r}"


def test_llms_txt_has_no_stale_claims():
    import api_server
    for claim in STALE_CLAIMS:
        assert claim not in api_server.LLMS_TXT


def test_llms_txt_documents_the_workspace_key_claim_gate():
    import api_server
    txt = api_server.LLMS_TXT
    assert "workspace_key" in txt
    assert "/start" in txt
    assert "/v1/billing/x402" in txt


def test_llms_txt_does_not_call_mcp_reads_open():
    """ledger_report/ledger_alerts are credential-gated now — saying
    otherwise tells an agent it can skip auth."""
    import api_server
    txt = api_server.LLMS_TXT
    assert "ledger_report         — get a spend report (open read)" not in txt
    assert "remain open reads" not in txt


def test_agent_json_auth_describes_workspace_key():
    import api_server
    auth = api_server.AGENT_JSON["auth"]
    assert auth["type"] == "workspace_key"
    assert "No signup" not in auth["description"]
    assert "/start" in auth["description"]


def test_agent_json_advertises_the_x402_self_serve_path():
    import api_server
    ids = {c["id"] for c in api_server.AGENT_JSON["capabilities"]}
    assert "mint_workspace_x402" in ids


def test_agent_json_pricing_is_per_workspace():
    import api_server
    desc = api_server.AGENT_JSON["pricing"]["description"]
    assert "per workspace" in desc
    assert "first 50 workspaces" in desc


def test_api_docs_quickstart_documents_workspace_key():
    from docs_content import get_api_docs
    quickstart = get_api_docs("quickstart")
    assert "workspace_key" in quickstart
    assert "workspace_key_required" in quickstart


def test_error_codes_doc_lists_workspace_key_required():
    from docs_content import get_api_docs
    errors = get_api_docs("errors")
    assert "workspace_key_required" in errors


def test_recipes_md_mirrors_docs_content_exactly():
    """recipes.md is the static mirror of the RECIPES dict — the header of
    that file promises it never drifts, so enforce it."""
    from docs_content import RECIPES
    md = (REPO / "recipes.md").read_text()
    drifted = [name for name, code in RECIPES.items() if code.strip() not in md]
    assert drifted == [], f"recipes.md out of sync with docs_content.py: {drifted}"


@pytest.mark.parametrize("pattern", ("python_tracking", "budget_enforcement",
                                     "retry_safe_writes"))
def test_claiming_recipes_send_a_workspace_key(pattern):
    from docs_content import get_example
    assert "workspace_key" in get_example(pattern)
