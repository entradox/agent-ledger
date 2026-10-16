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
# README.md was already in this tuple, but the session-cookie read claim it
# carried was not covered by any of the patterns above, so it rotted silently
# through the Google removal. Keep this comment as a reminder that membership
# in DOC_FILES only catches the patterns in STALE_CLAIMS.

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
    # NOT the launch-window grant. This assertion used to REQUIRE the claim,
    # which is how a false promise survived in a machine-read manifest: the
    # human self-serve path (/start) calls create_workspace(grant_scarcity=False),
    # so an agent reading this would tell its user Pro is automatic and be wrong.
    # Retired from every surface 2026-09-13.
    assert "first 50" not in desc


def test_llms_txt_documents_credential_recovery():
    """A lost agent_secret used to be unrecoverable and the docs were silent
    either way. The recovery path is advertised now; if it silently disappears
    from the manifest, agents go back to believing a lost secret is fatal."""
    import api_server
    txt = api_server.LLMS_TXT
    assert "/v1/agents/{agent_id}/rotate-secret" in txt
    assert "ledger_rotate_secret" in txt


def test_agent_json_advertises_credential_recovery():
    import api_server
    ids = {c["id"] for c in api_server.AGENT_JSON["capabilities"]}
    assert {"rotate_agent_secret", "revoke_agent_secret"} <= ids
    assert "rotate-secret" in api_server.AGENT_JSON["auth"]["description"]


def test_error_doc_lists_the_rotation_codes():
    from docs_content import get_api_docs
    errors = get_api_docs("errors")
    assert "agent_not_claimed" in errors
    assert "not_your_agent" in errors


def test_llms_txt_states_the_data_handling_position():
    """The strongest trust statement a spend tracker has, and the one the
    audit found invisible. It must not quietly disappear from the manifest."""
    import api_server
    txt = api_server.LLMS_TXT
    assert "prompt or response content" in txt
    assert "/privacy" in txt and "/terms" in txt


def test_agent_json_legal_names_the_pages():
    import api_server
    legal = api_server.AGENT_JSON["legal"]
    assert "/privacy" in legal and "/terms" in legal
    assert "never stored" in legal


def test_docs_document_auto_pricing_and_its_refusal():
    """The refusal is the load-bearing part: an unpriced model must never be
    recorded as a zero-cost spend."""
    import api_server
    from docs_content import get_api_docs
    assert "model_not_priced" in api_server.LLMS_TXT
    assert "model_not_priced" in get_api_docs("errors")
    assert "OPTIONAL" in api_server.LLMS_TXT


def test_llms_txt_documents_the_shortest_onboarding_path():
    """If the two-line path disappears from the manifest, a stranger has to
    reverse-engineer the proxy to use it — which is how the last integration
    friction survived."""
    import api_server
    txt = api_server.LLMS_TXT
    assert "agentledger.wrap" in txt
    assert "agent-ledger init" in txt
    assert "does not\n" not in txt.split("agentledger.wrap")[0][-200:]  # sanity


def test_docs_say_providers_are_config_not_code():
    """The fix for 'DeepSeek is not counted' is provider extensibility, not a
    DeepSeek special case. If the docs stop saying so, the next vendor goes
    invisible again."""
    import api_server
    from docs_content import get_api_docs
    assert "providers.json" in api_server.LLMS_TXT
    assert "providers.json" in get_api_docs("metering")
    assert "deepseek" in api_server.LLMS_TXT.lower()
    # and the errors topic must be about errors again — my earlier sections had
    # piled up there because of where I anchored them
    assert "providers.json" not in get_api_docs("errors")


def test_llms_txt_documents_the_proxy_and_its_limits():
    """The proxy is the only real enforcement this product has, and the only
    place a caller could be misled about what it guarantees."""
    import api_server
    txt = api_server.LLMS_TXT
    assert "/proxy/{provider}" in txt
    assert "NEVER stored" in txt
    assert "bypass" in txt          # the honest limit, stated not buried
    assert "/v1/pricing" in txt


def test_the_landing_page_states_the_proxy_limit(page_docs=None):
    """Enforcement must never be claimed for traffic that skips the proxy."""
    from pathlib import Path as _P
    html = (_P(__file__).resolve().parent.parent / "status.html").read_text()
    assert "not enforced" in html
    assert "never stored" in html


def test_agent_json_advertises_the_proxy_with_the_bypass_stated():
    import api_server
    caps = {c["id"]: c for c in api_server.AGENT_JSON["capabilities"]}
    assert "route_through_proxy" in caps
    assert "bypass" in caps["route_through_proxy"]["description"]


def test_llms_txt_documents_alert_delivery():
    """The webhook routes are how an alert reaches a human without polling."""
    import api_server
    txt = api_server.LLMS_TXT
    assert "/v1/webhooks" in txt
    assert "budget.warning" in txt


def test_agent_json_advertises_alert_delivery():
    import api_server
    ids = {c["id"] for c in api_server.AGENT_JSON["capabilities"]}
    assert "register_alert_webhook" in ids


def test_error_doc_lists_webhook_codes():
    from docs_content import get_api_docs
    errors = get_api_docs("errors")
    assert "invalid_webhook" in errors
    assert "webhook_not_found" in errors


def test_llms_txt_documents_share_links():
    """The share URL is the only read path a browser can use. If it vanishes
    from the manifest, agents stop telling humans how to read a report."""
    import api_server
    txt = api_server.LLMS_TXT
    assert "/v1/report/{agent_id}/share" in txt
    assert "html?t=" in txt


def test_agent_json_advertises_share_report_link():
    import api_server
    ids = {c["id"] for c in api_server.AGENT_JSON["capabilities"]}
    assert "share_report_link" in ids


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


# ── Served-surface PRICE integrity (D-1319 review; broadened by D-1676) ─────
# The defect this pins: /agents.txt served "pay $0.01 in USDC" as a hardcoded
# literal while every other surface derived the price from
# x402_verify.X402_MINT_PRICE. Reproduced: with X402_MINT_PRICE=$0.05 the SAME
# document rendered the correct derived "$0.05" three lines below the stale
# "$0.01" — so the document visibly contradicted itself. The same defect then
# recurred across city pages, the dashboard, the product pages and the docs,
# because each fix patched one string instead of the seam.
#
# WHY THIS IS A PROPERTY, NOT A PHRASE LIST: this project has shipped five
# stale-claim defects, and each earlier guard tested the phrasing someone
# thought of. A literal price is mode- and config-independent, so the durable
# assertion is "no served surface holds a raw price literal at all" — which
# catches every wording, and cannot be satisfied by rewording.
#
# WHY THE BAN IS A SET, NOT ONE STRING: banning only "$0.01" meant the guard was
# satisfied by shipping "$0.10" as a new literal — the same defect, one price
# later. BOTH shipped defaults stay banned forever.
#
# WHY THE DEFAULT IS BANNED BUT ALSO EXPECTED: the ban is on a shipped literal
# that is NOT the configured price. Under the sentinel below, "$0.10" is
# evidence of a literal (the config says $0.05) while "$0.05" is evidence of
# derivation. The app is booted under a price that is neither shipped default,
# so a surface can only pass by actually reading the config.

_PRICE_SENTINEL = "$0.05"
# Every price this product has SHIPPED as a served literal: "$0.01" until
# D-1676, "$0.10" from it.
_BANNED_LITERALS = ("$0.01", "$0.10")
# A line reading "then $0.01" is a dated record of what the price USED to be (a
# changelog entry, a superseded spec), not a claim about the offer. The
# exemption is line-aware and marker-bound: it never exempts a whole file.
_HISTORY_MARKER = "then "


def _stale_price_lines(text, configured):
    """Lines stating a superseded price as a LIVE price. The detector.

    Factored out so it can be canary-tested: a guard that cannot be shown to
    fail is not a guard, and this project has already shipped one that could not
    (it asserted a string the product always happened to contain).

    Deliberately duplicated in ~/AI-Workbench/projects/agent-ledger/
    probe_price_surface.py rather than shared: the probe must still be able to
    disagree with this file, so that weakening the in-repo guard cannot silently
    weaken the out-of-repo verification of it.
    """
    out = []
    for line in text.splitlines():
        for literal in _BANNED_LITERALS:
            if literal == str(configured):
                continue                      # the configured price is correct
            if literal in line and (_HISTORY_MARKER + literal) not in line:
                out.append((literal, line.strip()))
                break
    return out


# Served text surfaces that state our price: the city landing page, the product
# pages, the workspace/dashboard page, the pricing and upgrade doors, and the
# agent-facing manifests. Every one of them must state the CONFIGURED price and
# no shipped literal.
TEXT_PRICE_SURFACES = (
    "/", "/products", "/manifesto", "/developers", "/changelog", "/compare",
    "/agent-ledger", "/dashboard", "/demo", "/start", "/terms",
    "/pricing", "/upgrade", "/pricing.md",
    "/agents.txt", "/skill.md", "/llms.txt",
)


@pytest.fixture(scope="module")
def sentinel_app():
    """The app booted under _PRICE_SENTINEL, with its served bodies already read.

    The price is read from the environment at IMPORT time, so the sentinel needs
    a reload of x402_verify AND of api_server (which freezes the price into
    AGENT_JSON/LLMS_TXT at import). Both are reloaded back to the real config on
    teardown so the sentinel cannot leak into the rest of the session.
    """
    import importlib
    import os as _os
    from fastapi.testclient import TestClient

    saved = {k: _os.environ.get(k)
             for k in ("X402_MINT_PRICE", "X402_RESOURCE_PRICE")}
    _os.environ["X402_MINT_PRICE"] = _PRICE_SENTINEL
    _os.environ["X402_RESOURCE_PRICE"] = _PRICE_SENTINEL
    import x402_verify
    importlib.reload(x402_verify)
    import api_server
    importlib.reload(api_server)

    bodies = {}
    with TestClient(api_server.app) as client:
        for path in TEXT_PRICE_SURFACES:
            bodies[path] = client.get(path).text
        structured = {
            "/.well-known/x402": client.get("/.well-known/x402").json(),
            "/openapi.json": client.get("/openapi.json").json(),
        }
    try:
        yield {"bodies": bodies, "structured": structured, "api_server": api_server}
    finally:
        for k, v in saved.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v
        importlib.reload(x402_verify)
        importlib.reload(api_server)


def test_the_two_configured_prices_are_one_number():
    """The mint and the per-call resources are the same price by design: a page
    can then never quote one while the till charges the other."""
    import x402_verify
    assert x402_verify.X402_MINT_PRICE == x402_verify.X402_RESOURCE_PRICE, (
        "the workspace mint and the per-call resources disagree — 'our price' "
        "is one number")
    assert (x402_verify.x402_mint_price_atomic()
            == x402_verify.x402_resource_price_atomic())


@pytest.mark.parametrize("surface", TEXT_PRICE_SURFACES)
def test_no_served_surface_hardcodes_the_price(surface, sentinel_app):
    """Both directions, per surface: the configured price appears, the shipped
    literals do not.

    Fails if a surface carries "$0.01" or "$0.10" while the configured price is
    "$0.05" — a derived surface tracks the config; a literal cannot.
    """
    body = sentinel_app["bodies"][surface]
    stale = _stale_price_lines(body, _PRICE_SENTINEL)
    assert stale == [], (
        f"{surface} states a shipped price literal (line-aware history "
        f"exemption is only 'then $0.01') while the configured price is "
        f"{_PRICE_SENTINEL!r}: {stale[:3]} — derive it from "
        f"x402_verify.X402_MINT_PRICE instead")
    assert _PRICE_SENTINEL in body, (
        f"{surface} never mentions the configured price {_PRICE_SENTINEL!r} — "
        f"either it is not deriving the price, or it drops the price entirely")
    assert "{X402_" not in body and "__X402_" not in body, (
        f"{surface} still carries an unsubstituted price token — the render "
        f"path did not run (the D-1270 bug class)")


def test_no_served_surface_hardcodes_the_price__canary_literal(sentinel_app):
    """Canary: the detector above must FAIL when a literal is injected.

    Without this, every assertion in this file could pass while the detector
    quietly matched nothing. Both banned literals are injected, and the
    line-aware history exemption is shown not to swallow a bare literal.
    """
    clean = sentinel_app["bodies"]["/"]
    assert _stale_price_lines(clean, _PRICE_SENTINEL) == [], (
        "precondition: the real served body must be clean")

    for literal in _BANNED_LITERALS:
        poisoned = clean + f"\n<p>Pro is {literal} USDC, paid by the agent.</p>\n"
        found = _stale_price_lines(poisoned, _PRICE_SENTINEL)
        assert found, (
            f"canary_literal: the detector did NOT notice an injected "
            f"{literal!r} — it cannot fail, so it verifies nothing")
        assert found[0][0] == literal

    # The configured price is not a literal — the detector must stay silent.
    assert _stale_price_lines(
        clean + f"\n<p>Pro is {_PRICE_SENTINEL} USDC.</p>\n", _PRICE_SENTINEL) == []

    # History is exempt, line-aware, and only on the marked line.
    history = "2026-09-16: x402 live on Base mainnet (then $0.01 USDC = 24h Pro)"
    assert _stale_price_lines(history, _PRICE_SENTINEL) == []
    assert _stale_price_lines(history + "\n<p>Pro is $0.01 USDC.</p>",
                              _PRICE_SENTINEL), (
        "a bare literal on a different line must not be exempted by a marked "
        "historical one")


def test_structured_surfaces_derive_the_price(sentinel_app):
    """The machine-readable surfaces, where a stale number is worse than in
    prose: a caller acting on it sends a payment the till rejects."""
    wellknown = sentinel_app["structured"]["/.well-known/x402"]
    assert wellknown["priceUsd"] == 0.05, (
        f"the till publishes {wellknown['priceUsd']!r} while the configured "
        f"price is {_PRICE_SENTINEL!r}")
    percall = {p["route"]: p for p in wellknown["perCall"]}
    assert percall, "the per-call resources vanished from the till"
    for route, spec in percall.items():
        assert spec["priceUsd"] == 0.05, (
            f"{route} publishes {spec['priceUsd']!r} while the configured "
            f"resource price is {_PRICE_SENTINEL!r}")

    openapi = sentinel_app["structured"]["/openapi.json"]
    priceable = [p for p, methods in openapi["paths"].items()
                 for m, op in methods.items()
                 if isinstance(op, dict) and "x-payment-info" in op]
    assert priceable, "no path advertises x-payment-info any more"
    for path in priceable:
        for method, op in openapi["paths"][path].items():
            info = op.get("x-payment-info")
            if not info:
                continue
            assert info["x402"]["priceUsd"] == 0.05, (
                f"{method.upper()} {path} advertises "
                f"{info['x402']['priceUsd']!r}")

    # The structured pricing block in the agent manifest, in-process.
    amount = sentinel_app["api_server"].AGENT_JSON["pricing"]["amount_usd"]
    assert amount == 0.05, (
        f"AGENT_JSON.pricing.amount_usd = {amount!r} while the configured price "
        f"is {_PRICE_SENTINEL!r}")

    # And the buyer skill's atomic amount, which a paying agent signs against.
    # Asserted as the full atomic string, never as a substring: at $0.10 the
    # atomic amount is 100000, which CONTAINS "10000" — the old assertion passed
    # by luck at the new price and would have kept passing if the skill dropped
    # a zero.
    import x402_verify
    skill = sentinel_app["bodies"]["/skill.md"]
    atomic = str(x402_verify.x402_mint_price_atomic())
    assert atomic in skill, (
        f"the served skill must carry the atomic amount {atomic} that a payer "
        f"signs for")
