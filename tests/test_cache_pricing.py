# tests/test_cache_pricing.py
"""Input is not one price. It is three.

Claude bills input in up to three tiers at very different rates: fresh input
at 1x, a 5-minute cache write at 1.25x, a 1-hour cache write at 2x, and a
cache read at 0.1x. Pricing a call at the flat input rate is therefore not a
rounding error — for a cache-dominated session it is wrong in both directions
at once, and Claude Code sessions are cache-dominated.

These tests exist because the first version of this table had no cache tiers
at all, and would have understated the fleet's own Claude Code spend.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

import proxy


# ── each tier priced at its own rate ──────────────────────────────────────

@pytest.mark.parametrize("kwargs, expected_cents, label", [
    ({"cache_hit_in": 0, "cache_write_5m_in": 0, "cache_write_1h_in": 0}, 200.0, "1M fresh input @ $2"),
    ({"cache_hit_in": 1_000_000},                                        20.0, "1M cache read @ $0.20"),
    ({"cache_write_5m_in": 1_000_000},                                  250.0, "1M 5m cache write @ $2.50"),
    ({"cache_write_1h_in": 1_000_000},                                  400.0, "1M 1h cache write @ $4.00"),
])
def test_each_claude_input_tier_is_priced_at_its_own_rate(kwargs, expected_cents, label):
    cost = proxy.cost_cents_exact("claude-sonnet-5", 1_000_000, 0, **kwargs)
    assert round(cost, 4) == expected_cents, label


def test_claude_output_is_priced_at_the_output_rate():
    assert round(proxy.cost_cents_exact("claude-sonnet-5", 0, 1_000_000), 4) == 1000.0


def test_a_realistic_claude_code_call():
    """The shape of an actual Claude Code turn: almost no fresh input, a large
    1-hour cache write, a partial cache read, and a few hundred output tokens."""
    cost = proxy.cost_cents_exact("claude-sonnet-5", 156_525, 567,
                                  cache_hit_in=40_869,
                                  cache_write_1h_in=115_652)
    expected = (2 / 1e6 * 2.00 + 115_652 / 1e6 * 4.00
                + 40_869 / 1e6 * 0.20 + 567 / 1e6 * 10.00) * 100
    assert abs(cost - expected) < 0.001


def test_the_flat_rate_would_have_understated_this_call_by_half():
    """Not a style point — the reason the tiers exist. Charging all 156,525
    input tokens at the base $2 rate hides the 2x cost of the cache write."""
    tiered = proxy.cost_cents_exact("claude-sonnet-5", 156_525, 567,
                                    cache_hit_in=40_869, cache_write_1h_in=115_652)
    flat = proxy.cost_cents_exact("claude-sonnet-5", 156_525, 567)
    assert flat < tiered, "the flat rate must not be cheaper than the truth here"
    assert (tiered - flat) / tiered > 0.30, (
        "if this stops being a large gap, the tiered model has stopped mattering")


def test_a_cache_read_heavy_call_is_not_overcharged():
    """The same mechanism in the other direction: 1M of cache reads must not
    be billed as 1M of fresh input."""
    cheap = proxy.cost_cents_exact("claude-sonnet-5", 1_000_000, 0,
                                   cache_hit_in=1_000_000)
    fresh = proxy.cost_cents_exact("claude-sonnet-5", 1_000_000, 0)
    assert fresh / cheap == 10.0


def test_buckets_are_clamped_so_fresh_input_cannot_go_negative():
    """A caller reporting overlapping buckets must not be able to produce a
    negative charge (or a discount) by over-reporting cache."""
    cost = proxy.cost_cents_exact("claude-sonnet-5", 1_000_000, 0,
                                  cache_hit_in=2_000_000,
                                  cache_write_1h_in=2_000_000)
    assert cost >= 0
    assert round(cost, 4) == 20.0        # everything landed in the cheapest tier, once


def test_a_model_without_cache_rates_behaves_exactly_as_before(monkeypatch):
    """A model whose price entry carries NO cache rates must charge every input
    token the standard rate.

    NOTE: this used to point at gpt-4o-mini, whose entry genuinely had no cache
    rates then. It does now (OpenAI lists a cached-input price), so the real
    table no longer contains a no-cache entry to exercise this with. The
    behaviour is still load-bearing — a future provider row can omit them — so
    the assertion runs against a synthetic entry instead of being dropped.
    """
    table = dict(proxy.prices())
    table["test-no-cache-model"] = {"in": 15.0, "out": 60.0, "provider": "test"}
    monkeypatch.setattr(proxy, "prices", lambda: table)
    monkeypatch.setattr(proxy, "aliases", lambda: {})

    assert round(proxy.cost_cents_exact("test-no-cache-model", 1_000_000, 0), 4) == 1500.0
    assert proxy.cost_cents_exact("test-no-cache-model", 1_000_000, 0,
                                  cache_hit_in=1_000_000) == 1500.0


# ── provider usage parsing: two different conventions ─────────────────────

def test_anthropic_input_is_the_sum_of_its_three_buckets():
    """`input_tokens` on Anthropic is the UNCACHED REMAINDER, not the total —
    the opposite convention from OpenAI and DeepSeek. Reading it as a total
    would drop every cached token from the bill."""
    usage = {"input_tokens": 2, "output_tokens": 567,
             "cache_creation_input_tokens": 115_652,
             "cache_read_input_tokens": 40_869,
             "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                "ephemeral_1h_input_tokens": 115_652}}
    parsed = proxy.usage_tokens("anthropic", {"usage": usage})
    assert parsed["tokens_in"] == 2 + 115_652 + 40_869
    assert parsed["cache_hit_in"] == 40_869
    assert parsed["cache_write_1h_in"] == 115_652
    assert parsed["cache_write_5m_in"] == 0
    assert parsed["tokens_out"] == 567


def test_an_unsplit_cache_write_is_attributed_to_the_cheaper_tier():
    """When only the combined cache-write figure is reported, assume the 5m
    tier: under-charging beats inventing a higher bill."""
    usage = {"input_tokens": 10, "output_tokens": 5,
             "cache_creation_input_tokens": 1000}
    parsed = proxy.usage_tokens("anthropic", {"usage": usage})
    assert parsed["cache_write_5m_in"] == 1000
    assert parsed["cache_write_1h_in"] == 0


def test_openai_and_deepseek_totals_still_include_their_cache():
    openai_style = {"usage": {"prompt_tokens": 1000, "completion_tokens": 10}}
    assert proxy.usage_tokens("openai", openai_style)["tokens_in"] == 1000

    deepseek_style = {"usage": {"prompt_tokens": 1000, "completion_tokens": 10,
                                "prompt_cache_hit_tokens": 800}}
    parsed = proxy.usage_tokens("deepseek", deepseek_style)
    assert parsed["tokens_in"] == 1000          # already the total
    assert parsed["cache_hit_in"] == 800


# ── the table is honest about its own provenance ──────────────────────────

def test_claude_prices_are_marked_verified_with_a_source():
    for model in ("claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5"):
        entry = proxy.lookup(model)
        assert entry["verified"] is True, model
        assert entry["source"].startswith("https://platform.claude.com"), model


def test_openai_prices_are_verified_and_say_where_they_came_from():
    """These WERE unverified placeholders; they were checked against OpenAI's
    pricing page on 2026-09-15 and now carry a source + as_of date.

    The assertion that previously stood here (`verified is False`) was accurate
    when written and is now a stale claim, not a requirement — the real
    requirement is that the flag tracks reality in BOTH directions. The
    counterpart check (something is still flagged unverified) lives in
    tests/test_providers.py so the flag cannot silently become always-true.
    """
    entry = proxy.lookup("gpt-4o-mini")
    assert entry["verified"] is True
    assert entry["source"] == "https://platform.openai.com/docs/pricing"
    assert entry["as_of"] == "2026-09-15"
    # Cached input is a real, separate rate for this model — not a copy of `in`.
    assert entry["cache_hit"] == 0.075
