# tests/test_spending_spike.py
"""D-1230 — a spending spike must raise an alert, not just appear on a report.

The defect this pins: `report()` computed the `spending_spike` anomaly and
nothing ever called `_log_alert`, so `anomaly.detected` — the event the webhook
docs advertise and `/v1/webhooks` lets a customer subscribe to — could never
fire. The dogfood itself had a live spike (2026-09-13: $16.05 against a $3.43
daily average) that alerted nobody.

Two traps these tests exist to catch, both of which make the obvious
implementation silently wrong:

1. The window mean MOVES as later entries land, so a spike can appear at read
   time that did not exist when the offending entry was written. A check wired
   only into the write path misses exactly the spikes the report shows.
2. `_log_alert_daily()` dedupes on alert TYPE, which would alert the first
   spike and silence every later one forever. Dedupe has to be per spike day.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

AGENT = "spike-agent"


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine
    importlib.reload(ledger_engine)
    yield ledger_engine
    shutil.rmtree(tmp, ignore_errors=True)


def _alerts(engine):
    p = engine.DATA_DIR / "agents" / AGENT / "alerts.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _write_days(engine, days_cents):
    """One entry per day, dated in the past so each lands on its own UTC day.

    SpendEntry takes `timestamp` at construction, so the fixture writes entries
    through it rather than calling track() with a timestamp kwarg (that kwarg
    would land in `meta`, not in the record's timestamp).
    """
    import time
    from datetime import datetime, timezone
    for i, cents in enumerate(days_cents):
        ts = time.time() - (len(days_cents) - i) * 86400
        entry = engine.SpendEntry(agent_id=AGENT, rail="manual", amount_cents=cents,
                                  service="seed",
                                  timestamp=datetime.fromtimestamp(ts, timezone.utc).isoformat())
        p = engine._ledger_path(AGENT)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")
    # one real write so the write-path scan runs, as it would in production
    engine.track(AGENT, "manual", 1, "trigger")


def test_spike_raises_an_alert(env):
    engine = env
    _write_days(engine, [100, 100, 100, 100, 100, 5000])
    spikes = [a for a in _alerts(engine) if a["type"] == "spending_spike"]
    assert spikes, "a 50x day against a flat baseline must raise an alert"
    assert spikes[0]["date"].endswith(("01", "02", "03")) or spikes[0]["date"]
    assert "Spending spike" in spikes[0]["message"]


def test_report_and_alert_agree(env):
    """The anomaly a customer sees on the report must be the one that alerted.
    Two surfaces disagreeing about what a spike IS is worse than no alert."""
    engine = env
    _write_days(engine, [100, 100, 100, 100, 100, 5000])
    reported = {a["date"] for a in engine.report(AGENT, days=30).anomalies}
    alerted = {a["date"] for a in _alerts(engine) if a["type"] == "spending_spike"}
    assert reported, "sanity: the report must see a spike in this fixture"
    assert reported == alerted, f"report saw {reported}, alert feed saw {alerted}"


def test_no_spike_no_alert(env):
    engine = env
    _write_days(engine, [100, 100, 100, 100])
    assert [a for a in _alerts(engine) if a["type"] == "spending_spike"] == []


def test_single_day_is_not_a_spike(env):
    """One day of data has no mean to be anomalous against."""
    engine = env
    engine.track(AGENT, "manual", 99999, "seed")
    assert engine.scan_spending_spikes(AGENT) == []


def test_second_spike_still_alerts(env):
    """The per-TYPE dedupe trap: the first spike must not silence the second."""
    engine = env
    _write_days(engine, [100, 100, 100, 100, 100, 5000])
    first = {a["date"] for a in _alerts(engine) if a["type"] == "spending_spike"}
    assert first

    import time
    from datetime import datetime, timezone
    # a much later day, well clear of the first spike
    ts = time.time() + 3 * 86400
    entry = engine.SpendEntry(agent_id=AGENT, rail="manual", amount_cents=90000,
                              service="second",
                              timestamp=datetime.fromtimestamp(ts, timezone.utc).isoformat())
    with open(engine._ledger_path(AGENT), "a") as f:
        f.write(json.dumps(entry.to_dict()) + "\n")
    engine.scan_spending_spikes(AGENT)
    all_spikes = {a["date"] for a in _alerts(engine) if a["type"] == "spending_spike"}
    assert len(all_spikes) > len(first), (
        "a second spike on a different day must alert too — dedupe is per day, "
        f"not per type (got {all_spikes})")


def test_same_day_is_not_re_alerted(env):
    """Idempotent: re-scanning must not spam the feed."""
    engine = env
    _write_days(engine, [100, 100, 100, 100, 100, 5000])
    before = len([a for a in _alerts(engine) if a["type"] == "spending_spike"])
    engine.scan_spending_spikes(AGENT)
    engine.scan_spending_spikes(AGENT)
    after = len([a for a in _alerts(engine) if a["type"] == "spending_spike"])
    assert before == after


def test_alert_carries_the_event_the_webhook_subscribes_to(env):
    """The whole point of D-1218: a subscriber to anomaly.detected gets it."""
    engine = env
    import alert_delivery
    assert alert_delivery.event_for("spending_spike") == "anomaly.detected"


def test_scan_never_breaks_a_write(env, monkeypatch):
    """An alert failure must never fail the write that caused it."""
    engine = env
    def boom(*a, **k):
        raise RuntimeError("alert subsystem down")
    monkeypatch.setattr(engine, "_log_alert", boom)
    _write_days(engine, [100, 100, 100, 100, 100, 5000])  # must not raise
