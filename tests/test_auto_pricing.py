# tests/test_auto_pricing.py
"""BUILD-4 — token→cost auto-pricing on POST /v1/track.

Before this, `amount_cents` and `service` were REQUIRED even when the caller
sent exact token counts and a model. So every agent integrator had to duplicate
the provider's price table to report a spend, which is both friction and a
place to be quietly wrong.

The rule the tests enforce: a tokens-only write is priced from the shared
table, and an UNPRICED model is REFUSED rather than recorded as zero — a silent
zero is a lie about money that was really spent.
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

AL_VERSION = "2026-09-01"
AGENT = "autopriced-agent"


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, workspace_engine, identity, proxy, routes_agents, api_server
    for module in (proxy, ledger_engine, workspace_engine, identity, routes_agents, api_server):
        importlib.reload(module)
    from fastapi.testclient import TestClient
    tc = TestClient(api_server.app)
    _, key = workspace_engine.create_workspace(owner_email="a@example.com")
    r = tc.post("/v1/track", headers={"AL-API-Version": AL_VERSION},
                json={"agent_id": AGENT, "rail": "api_key", "amount_cents": 0,
                      "service": "seed", "workspace_key": key})
    assert r.status_code == 200, r.text
    yield tc, key, r.json()["agent_secret"]
    shutil.rmtree(tmp, ignore_errors=True)


def _track(tc, secret, **overrides):
    body: dict = {"agent_id": AGENT, "rail": "api_key", "service": "openai"}
    body.update(overrides)
    if "amount_cents" not in body and "tokens_in" not in body:
        body["amount_cents"] = 0
    if secret:
        body["agent_secret"] = secret
    return tc.post("/v1/track", headers={"AL-API-Version": AL_VERSION}, json=body)


def _report(tc, secret):
    return tc.get(f"/v1/report/{AGENT}", headers={"X-Agent-Secret": secret}).json()


def test_a_real_provider_model_id_auto_prices(env):
    """muse-ai functional test, 2026-09-15.

    The wrapper and the proxy forward the caller's REAL model string, so a
    tokens-only write sent `claude-sonnet-4-20250514` and was refused with
    `model_not_priced` while that same model sat in the table as
    `claude-sonnet-4-5`. Every tokens-only write from a real SDK therefore died
    on the primary enforcement path.
    """
    tc, key, secret = env
    r = _track(tc, secret, rail="api_key", model="claude-sonnet-4-20250514",
               tokens_in=1000, tokens_out=100, service="")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["priced"] == "auto"
    assert body["priced_model"] == "claude-sonnet-4-5"
    # 1000 in @ $3/MTok + 100 out @ $15/MTok = $0.0045 -> 0.45 cents
    assert body["amount_cents"] == 0
    assert "alias of" in body["_note"]


def test_a_dated_openai_snapshot_prices_at_its_base_rate(env):
    tc, key, secret = env
    r = _track(tc, secret, rail="api_key", model="gpt-4o-2024-08-06",
               tokens_in=1_000_000, tokens_out=0, service="")
    assert r.status_code == 200, r.text
    assert r.json()["priced_model"] == "gpt-4o"


def test_a_dearer_dated_snapshot_is_still_refused(env):
    """The guard against a naive 'strip the date suffix' alias rule.

    OpenAI prices gpt-4o-2024-05-13 at $5/$15 against base gpt-4o at $2.50/$10
    (platform.openai.com/docs/pricing). Stripping the date would auto-price it
    at HALF its real cost — under-metering a spend cap, the exact failure the
    cap exists to prevent. It must stay unpriced and be refused, not guessed.
    """
    tc, key, secret = env
    r = _track(tc, secret, rail="api_key", model="gpt-4o-2024-05-13",
               tokens_in=1000, tokens_out=100, service="")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "model_not_priced"


def test_a_genuinely_unknown_model_is_still_refused(env):
    tc, key, secret = env
    r = _track(tc, secret, rail="api_key", model="not-a-real-model-9",
               tokens_in=1000, tokens_out=100, service="")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "model_not_priced"


def test_an_exact_table_hit_is_not_reported_as_alias_resolved(env):
    """A direct hit must not claim it was 'resolved' — nothing was substituted."""
    tc, key, secret = env
    r = _track(tc, secret, rail="api_key", model="claude-sonnet-4-5",
               tokens_in=1000, tokens_out=100, service="")
    assert r.status_code == 200, r.text
    assert "priced_model" not in r.json()


# ── the feature ────────────────────────────────────────────────────────────

def test_a_tokens_only_write_is_priced_for_you(env):
    """gpt-4o-mini: 0.15 in / 0.60 out per 1M → 1M + 1M = 75 cents."""
    tc, key, secret = env
    r = _track(tc, secret, tokens_in=1_000_000, tokens_out=1_000_000,
               model="gpt-4o-mini")
    assert r.status_code == 200, r.text
    assert r.json()["amount_cents"] == 75
    assert r.json()["priced"] == "auto"
    assert _report(tc, secret)["total_spend_cents"] == 75


def test_the_computed_amount_lands_in_the_report(env):
    tc, key, secret = env
    _track(tc, secret, tokens_in=1_000_000, tokens_out=0, model="gpt-4o-mini")
    rep = _report(tc, secret)
    assert rep["total_spend_cents"] == 15          # 0.15 per 1M in
    assert rep["by_service"].get("openai") == 15


def test_service_defaults_to_the_models_provider(env):
    tc, key, secret = env
    body = {"agent_id": AGENT, "rail": "api_key", "agent_secret": secret,
            "tokens_in": 1_000_000, "tokens_out": 0, "model": "claude-sonnet-4-5"}
    r = tc.post("/v1/track", headers={"AL-API-Version": AL_VERSION}, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["service"] == "anthropic"


# ── what it must refuse ────────────────────────────────────────────────────

def test_an_unpriced_model_is_refused_not_recorded_as_zero(env):
    """The whole reason to refuse: a 0-cent entry would read as 'this agent
    spends nothing' while real money left the account."""
    tc, key, secret = env
    r = _track(tc, secret, tokens_in=1000, tokens_out=1000, model="mystery-model-2099")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "model_not_priced"
    assert _report(tc, secret)["total_spend_cents"] == 0


def test_a_tokens_write_with_no_model_is_refused(env):
    tc, key, secret = env
    r = _track(tc, secret, tokens_in=1000, tokens_out=1000)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "amount_required"


def test_an_amount_write_with_no_tokens_still_requires_an_amount(env):
    tc, key, secret = env
    r = tc.post("/v1/track", headers={"AL-API-Version": AL_VERSION},
                json={"agent_id": AGENT, "rail": "api_key", "agent_secret": secret})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "amount_required"


# ── nothing that already worked may change ─────────────────────────────────

def test_an_explicit_amount_is_still_honoured_verbatim(env):
    tc, key, secret = env
    r = _track(tc, secret, amount_cents=999, tokens_in=1_000_000,
               tokens_out=1_000_000, model="gpt-4o-mini")
    assert r.status_code == 200
    assert r.json()["amount_cents"] == 999        # the caller's number wins
    assert "priced" not in r.json()


def test_token_bookkeeping_rows_are_still_zero_cent(env):
    tc, key, secret = env
    r = tc.post("/v1/track", headers={"AL-API-Version": AL_VERSION},
                json={"agent_id": AGENT, "rail": "tokens", "agent_secret": secret,
                      "tokens_in": 500, "tokens_out": 500, "model": "gpt-4o-mini"})
    assert r.status_code == 200, r.text
    assert r.json()["amount_cents"] == 0


def test_the_amount_ceiling_still_applies_to_a_supplied_amount(env):
    tc, key, secret = env
    r = _track(tc, secret, amount_cents=99_999_999)
    assert r.status_code == 422
