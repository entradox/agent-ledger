"""The unpriced-model safety contract: unknown cost must never block a call.

This is the contract that decided item 6 of VALUE-BUILD-3. The brief asked for
Ollama (kimi/glm/qwen) rows to be added to model_prices.json marked
`verified: false`, on the theory that this would make the owner's own spend
visible. Reading the code, that would have been unsafe, and the reasoning is
worth keeping because it will come up again:

  1. `verified` is REPORTING-ONLY. It is surfaced through /v1/pricing and the
     price block, but the enforcement path never consults it.
  2. The proxy prices a call PRE-FLIGHT from this table (proxy.call_estimate_cents
     -> cost_cents_exact). An entry that exists is an entry the cap will enforce
     against, verified or not.
  3. Therefore adding `verified: false` rows would make the proxy enforce real
     dollar caps using numbers derived from a subscription fee. A subscription
     is not a per-token price. The caps would refuse real calls on a fiction,
     and they would be WRONG IN BOTH DIRECTIONS depending on usage.
  4. Ollama is additionally not a proxy provider at all (providers.json:
     anthropic, deepseek, moonshot, openai), so those rows could never be
     exercised through the enforcement path anyway.

The honest behaviour for a model with no real per-token price is the one already
implemented: pass the call through, record the tokens, and raise an alert. These
tests pin that, because it is load-bearing and nothing was defending it.
"""
import tempfile

import pytest


@pytest.fixture()
def isolated(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, workspace_engine, identity, spend_policy
    for m in (ledger_engine, workspace_engine, identity, spend_policy):
        importlib.reload(m)
    import shutil
    yield ledger_engine, workspace_engine, spend_policy
    shutil.rmtree(tmp, ignore_errors=True)


def test_an_unpriced_model_is_allowed_not_blocked(isolated):
    """The whole contract: we do not refuse a call because we cannot price it."""
    le, we, sp = isolated
    _, key = we.create_workspace(owner_email="p@example.com")
    le.track("guard-unpriced-1", "api_key", 0, "seed", workspace_key=key)
    # A real, enforced cap.
    le.set_budget("guard-unpriced-1", monthly_cents=100, daily_cents=100)

    d = sp.decide("guard-unpriced-1", None)          # None == cost unknown
    assert d["allowed"] is True, d
    assert d["reason"] == sp.UNPRICED_MODEL
    assert d["binding"] is None


def test_an_unknown_cost_is_never_reported_as_within_budget(isolated):
    """It must not claim a number nobody computed. That is the honest-reporting
    half of the same contract, and the reason code carries it."""
    le, we, sp = isolated
    _, key = we.create_workspace(owner_email="p@example.com")
    le.track("guard-unpriced-2", "api_key", 0, "seed", workspace_key=key)
    le.set_budget("guard-unpriced-2", monthly_cents=100, daily_cents=100)

    d = sp.decide("guard-unpriced-2", None)
    assert d["reason"] != sp.WITHIN_BUDGET


def test_a_known_cost_over_the_cap_is_still_denied(isolated):
    """Guard the guard: the unpriced path must not have softened real enforcement."""
    le, we, sp = isolated
    _, key = we.create_workspace(owner_email="p@example.com")
    le.track("guard-priced-1", "api_key", 0, "seed", workspace_key=key)
    le.set_budget("guard-priced-1", monthly_cents=100, daily_cents=100)

    ok = sp.decide("guard-priced-1", 90)
    assert ok["allowed"] is True
    over = sp.decide("guard-priced-1", 101)
    assert over["allowed"] is False
    assert over["binding"] is not None


def test_the_price_table_has_no_subscription_derived_rows(isolated):
    """Regression guard for the rejected approach: a model with no real
    per-token price must not be given one. If someone later adds Ollama rows
    derived from a plan fee, this fails and they must read the docstring."""
    import proxy
    table = proxy.prices()
    for model in table:
        low = model.lower()
        assert not any(k in low for k in ("kimi", "glm-", "qwen")), (
            f"'{model}' is priced from a subscription, not a per-token rate. "
            f"An entry here means the proxy will enforce dollar caps against a "
            f"fiction — see this file's docstring before adding it.")


def test_ollama_is_not_a_proxy_provider(isolated):
    """Item 6 assumed these models were proxiable. They are not."""
    import proxy
    assert "ollama" not in proxy.provider_ids()
