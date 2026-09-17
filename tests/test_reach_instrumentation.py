#!/usr/bin/env python3
"""The reach instrument must cover every HTML product page (D-1321).

WHY THIS EXISTS (2026-09-17)
----------------------------
A funnel report claimed "607 hit `/`, only 48 reach `/start` — 92% never reach the mint."
That number was an artifact of OUR OWN instrument, not user behaviour:

  * `/` is NOT AgentLedger's landing page. D-1239 turned the domain root into the AI Agent
    City umbrella index — a 2.7 KB, 455-word hub listing five products, with no price and
    no `/start` link. It cannot convert.
  * AgentLedger's real landing page is `/agent-ledger` (21 KB, carries the pitch and the
    pricing, links to `/start`).
  * `REACH_PATHS` was a hand-maintained 9-path list that omitted `/agent-ledger` and
    `/quickstart`, so the converting page's traffic was never recorded.

The "drop" was therefore arithmetic over two different populations. Same defect class as the
0.5-midpoint verifier fallback and the memory monitor's free-RAM alarm: a confident verdict
computed from a number that does not mean what it is being read as.

This test is the guard. A hand-maintained allowlist rots silently because nothing notices
what is missing; this makes the omission a RED test instead of a quiet zero.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import api_server  # noqa: E402


def _html_get_routes() -> set:
    """Every registered GET route that returns an HTMLResponse."""
    out = set()
    for route in api_server.app.routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        methods = getattr(route, "methods", None) or set()
        if "GET" not in methods:
            continue
        rcls = getattr(route, "response_class", None)
        if rcls is not None and "HTML" in getattr(rcls, "__name__", ""):
            out.add(path)
    return out


def test_every_html_page_is_instrumented_or_explicitly_exempt():
    """THE GUARD. A new HTML page that is neither tracked nor exempted fails here.

    Without this, the next product page added is invisible in metrics and nobody finds out
    until someone reads a funnel number that is quietly wrong — which is exactly what
    happened to `/agent-ledger`.
    """
    routes = _html_get_routes()
    assert routes, "no HTML GET routes discovered — the probe is broken, not the app"

    untracked, unexempted = [], []
    for path in sorted(routes):
        if path in api_server.REACH_PATHS:
            continue
        if path in api_server.REACH_EXEMPT:
            continue
        untracked.append(path)

    assert not untracked, (
        "HTML GET route(s) neither tracked in REACH_PATHS nor exempted with a reason in "
        f"REACH_EXEMPT: {untracked}. Add to REACH_PATHS, or add to REACH_EXEMPT with a "
        "reason explaining why this page is not a funnel entry."
    )


def test_product_landing_pages_are_tracked():
    """The specific regression: the page that carries the CTA must be counted."""
    for path in ("/agent-ledger", "/quickstart", "/start"):
        assert path in api_server.REACH_PATHS, (
            f"{path} is a conversion surface but is not tracked — the funnel denominator "
            f"would again omit the page that actually converts"
        )


def test_reach_exempt_entries_all_carry_a_reason():
    """An exemption with no reason is just an omission. Every entry must explain itself."""
    for path, reason in api_server.REACH_EXEMPT.items():
        assert isinstance(reason, str) and len(reason.strip()) >= 12, (
            f"REACH_EXEMPT['{path}'] needs a real reason, not a placeholder"
        )


def test_reach_exempt_does_not_shadow_a_tracked_path():
    """A path cannot be both tracked and exempt — that would make the guard meaningless."""
    both = set(api_server.REACH_PATHS) & set(api_server.REACH_EXEMPT)
    assert not both, f"path(s) in BOTH REACH_PATHS and REACH_EXEMPT: {sorted(both)}"


def test_guard_can_actually_go_red():
    """Prove the guard BITES: a fake untracked HTML route must be caught.

    Without this, the test could pass for the wrong reason (e.g. discovering no routes at
    all) and would be decoration. Mirrors the fleet's canary discipline.
    """
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse

    probe = FastAPI()

    @probe.get("/a-brand-new-product-page", response_class=HTMLResponse)
    def _new_page():
        return "<h1>new</h1>"

    original_app = api_server.app
    try:
        api_server.app = probe
        assert "/a-brand-new-product-page" in _html_get_routes(), "probe route not found"
        with pytest.raises(AssertionError):
            test_every_html_page_is_instrumented_or_explicitly_exempt()
    finally:
        api_server.app = original_app


def test_umbrella_index_links_to_the_mint():
    """The domain root must not be a dead end (D-1321).

    `/` is the umbrella hub and cannot convert on its own, but it should still offer a path
    into the funnel. Before this it carried no price and no link to `/start`.
    """
    import site_pages

    assert '/start' in site_pages.UMBRELLA_INDEX, (
        "the umbrella index has no link to /start — it is a dead end for a visitor who "
        "arrives at the domain root"
    )
    assert '/quickstart' in site_pages.UMBRELLA_INDEX
