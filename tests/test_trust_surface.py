# tests/test_trust_surface.py
"""D-1219 — the trust surface: claims that match the code, and pages that exist.

Two rules are under test here, and they are the ones the audit's BUG-5/BUG-5b
were really about:

1. A claim must be traceable to behaviour. "Budget caps with real enforcement"
   was on the pricing card while caps only blocked the ledger WRITE — the
   marketing had outrun the architecture.
2. A data-handling position must be stated AND true. "We never store prompts"
   is one of the strongest trust sentences a spend tracker can offer, and it
   was invisible — but it is only worth saying if the data model backs it.
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def data_dir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="agent-ledger-trust-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, workspace_engine, identity, api_server
    for m in (ledger_engine, workspace_engine, identity, api_server):
        importlib.reload(m)
    yield
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def page(monkeypatch):
    from fastapi.testclient import TestClient
    import api_server
    return TestClient(api_server.app)


# ── claims match the architecture ──────────────────────────────────────────

def test_the_landing_page_does_not_claim_enforcement_the_code_does_not_do(page):
    """"Real enforcement" is the exact phrase that had to go: until the proxy
    ships, a cap rejects the WRITE, not the spend."""
    body = page.get("/").text
    assert "real enforcement" not in body.lower()
    assert "block the write when crossed" in body


def test_the_landing_page_does_not_promise_stopped_spend(page):
    body = page.get("/").text.lower()
    # phrases that would imply the provider charge itself is prevented
    for overclaim in ("stops runaway spend", "blocks the spend", "stops the spend"):
        assert overclaim not in body, f"landing page still implies: {overclaim!r}"


def test_the_terms_state_what_a_cap_actually_does(page):
    body = page.get("/terms").text
    assert "rejects the ledger write" in body
    assert "not the underlying charge" in body


# ── the data-handling position is stated ───────────────────────────────────

def test_landing_page_states_the_data_handling_position(page):
    body = page.get("/").text
    assert "Cost metadata only" in body
    assert "no field for prompt or response content" in body


def test_the_prompt_storage_claim_matches_the_data_model():
    """The claim is only worth making if the storage layer actually has no
    place to put a prompt. Assert it against the model, not the marketing."""
    src = (REPO / "ledger_engine.py").read_text()
    entry = src[src.index("class SpendEntry"):src.index("class SpendEntry") + 700]
    for banned in ("prompt", "completion", "message", "content", "body"):
        assert banned not in entry.lower(), f"SpendEntry carries a {banned!r} field"
    for present in ("agent_id", "rail", "amount_cents", "service", "timestamp"):
        assert present in entry


def test_privacy_states_the_same_position(page):
    body = page.get("/privacy").text
    assert "Prompts and model responses" in body
    assert "do not sell" in body.lower()


def test_privacy_names_the_processors(page):
    body = page.get("/privacy").text
    for processor in ("Railway", "Stripe"):
        assert processor in body


# ── the pages exist and are reachable ──────────────────────────────────────

def test_privacy_and_terms_are_served_and_linked_from_the_landing_page(page):
    landing = page.get("/").text
    for path in ("/privacy", "/terms"):
        assert path in landing, f"{path} is not linked from the landing page"
        assert page.get(path).status_code == 200


def test_status_is_a_real_health_page_not_the_marketing_page(page):
    """Before D-1219, /status returned byte-identical HTML to /. A status page
    that is the marketing page is not a status page."""
    landing = page.get("/").text
    status = page.get("/status").text
    assert landing != status
    assert "Operational" in status
    assert "uptime" in status
    assert "AgentLedger status" in status
    # and it reports live numbers rather than prose
    assert "claimed agents" in status
    assert "launch-window slots left" in status


def test_status_page_links_the_machine_readable_health_checks(page):
    status = page.get("/status").text
    assert "/health" in status
    assert "/stats" in status


def test_llms_txt_still_describes_status_truthfully():
    """/status is documented to agents as the human/agent status page — that was
    a lie when it served the pricing page. Keep the description and the page in
    agreement."""
    import api_server
    assert "status page: GET /status" in api_server.LLMS_TXT
