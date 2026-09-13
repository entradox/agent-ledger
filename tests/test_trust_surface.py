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


def test_every_public_surface_advertises_the_products_own_domain(page):
    """An infrastructure subdomain in the copy is the trust gap the audit opened
    with: nobody points production agents at a random *.up.railway.app address.
    The host still serves (health checks use it), but nothing the product shows a
    human or an agent should mention it."""
    for path in ("/", "/llms.txt", "/.well-known/agent.json", "/server.json"):
        body = page.get(path).text
        assert "aiagentscity.com" in body, f"{path} does not name the real domain"
        assert "up.railway.app" not in body, f"{path} still advertises the infrastructure host"


def test_enforcement_claims_carry_the_proxy_qualification(page):
    """This test used to forbid any 'stops the spend' phrasing outright, because
    before D-1222 every such claim was false: a cap rejected the ledger write
    after the provider had charged.

    The proxy makes the claim true — for proxied traffic. So the rule becomes
    conditional rather than absolute: if the page says spend is blocked, it must
    also name the proxy and state that bypassing it is not enforced. A claim
    that travels without its limit is the thing that was wrong before.
    """
    body = page.get("/").text
    lowered = body.lower()
    assert "real enforcement" not in lowered
    for claim in ("blocks the spend", "stops the spend", "stops runaway spend"):
        if claim in lowered:
            assert "proxy" in lowered, f"{claim!r} is claimed without naming the proxy"
            assert "not enforced" in lowered, (
                f"{claim!r} is claimed without stating that traffic bypassing the "
                f"proxy is not enforced")


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
