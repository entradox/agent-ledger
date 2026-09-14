#!/usr/bin/env python3
"""D-1230 / GAP-3: the funnel must survive a process restart.

The defect this pins: `metrics._totals` is process-lifetime. In production the
admin funnel read `track_ok: 3` while 88 track_ok events sat in metrics.jsonl
going back six days. An operator opening /v1/metrics after a deploy saw a
number that was confidently wrong, and "nobody is using this" is
indistinguishable from "the counter reset".

Same class as reach_from_file(), which was fixed earlier for exactly this
reason and never applied to the funnel.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import metrics  # noqa: E402


def _fresh(tmp_path, monkeypatch, records):
    """Point metrics at an isolated file and drop the in-memory state, the way
    a redeploy does."""
    monkeypatch.setattr(metrics, "METRICS_FILE", tmp_path / "metrics.jsonl")
    monkeypatch.setattr(metrics, "_totals", metrics.defaultdict(int))
    monkeypatch.setattr(metrics, "_recent", metrics.deque())
    with open(tmp_path / "metrics.jsonl", "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_funnel_from_file_counts_history(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch, [
        {"kind": "track_ok", "ts": "2026-09-09T03:53:34Z"},
        {"kind": "track_ok", "ts": "2026-09-14T10:15:50Z"},
        {"kind": "track_ok", "ts": "2026-09-14T10:16:00Z"},
        {"kind": "mcp_call", "ts": "2026-09-14T05:14:55Z"},
    ])
    counts = metrics.funnel_from_file()
    assert counts["track_ok"] == 3
    assert counts["mcp_call"] == 1


def test_restart_does_not_zero_the_funnel(tmp_path, monkeypatch):
    """The real regression: history on disk, empty counters in memory."""
    _fresh(tmp_path, monkeypatch, [{"kind": "track_ok", "ts": "2026-09-14T10:15:50Z"}] * 88)

    # What the old code read: in-memory only -> 0.
    assert metrics.snapshot()["totals"].get("track_ok", 0) == 0

    # What the fix reads.
    assert metrics.funnel_from_file()["track_ok"] == 88


def test_missing_file_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "METRICS_FILE", tmp_path / "nope.jsonl")
    assert dict(metrics.funnel_from_file()) == {}


def test_corrupt_lines_are_skipped_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "METRICS_FILE", tmp_path / "metrics.jsonl")
    with open(tmp_path / "metrics.jsonl", "w") as f:
        f.write('{"kind": "track_ok"}\n')
        f.write("this is not json\n")
        f.write('{"kind": "track_ok"}\n')
    assert metrics.funnel_from_file()["track_ok"] == 2
