# tests/test_claude_code_reporter.py
"""BUILD-6 — reporting a Claude Code session's spend.

The reporter's two hard requirements were both discovered by looking at real
transcripts, not by reasoning about them:

* the same message is written more than once (317 records, 181 unique ids in
  one session) so records must be collapsed by message id or spend inflates;
* Anthropic's `input_tokens` is the uncached remainder, so the total has to be
  reassembled from three buckets before it is priced.
"""
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

REPORTER = Path(__file__).resolve().parent.parent / "integrations" / "claude-code" / "report-session.py"
spec = importlib.util.spec_from_file_location("report_session", REPORTER)
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


def _assistant(msg_id, model, *, fresh=0, w5=0, w1=0, hit=0, out=0):
    """One transcript record, shaped exactly like the real ones."""
    usage = {"input_tokens": fresh, "output_tokens": out,
             "cache_read_input_tokens": hit,
             "cache_creation_input_tokens": w5 + w1}
    if w5 or w1:
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": w5,
                                   "ephemeral_1h_input_tokens": w1}
    return {"type": "assistant", "uuid": f"u-{msg_id}", "message": {"id": msg_id,
            "model": model, "usage": usage}}


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(reporter, "STATE_DIR", tmp_path / "state")
    path = tmp_path / "session.jsonl"
    lines = [
        # a message duplicated three times, byte-identical — as streaming writes it
        _assistant("msg_A", "claude-sonnet-5", fresh=2, w1=115_652, hit=40_869, out=567),
        _assistant("msg_A", "claude-sonnet-5", fresh=2, w1=115_652, hit=40_869, out=567),
        _assistant("msg_A", "claude-sonnet-5", fresh=2, w1=115_652, hit=40_869, out=567),
        _assistant("msg_B", "claude-sonnet-5", fresh=5, w5=4_115, hit=156_521, out=1_152),
        # a second model in the same session
        _assistant("msg_C", "claude-haiku-4-5", fresh=10, out=20),
        # noise that must be ignored
        {"type": "user", "message": {"role": "user", "content": "hello"}},
        {"type": "summary", "summary": "no usage here"},
    ]
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return path


# ── deduplication: the defect that would have inflated real spend ─────────

def test_duplicate_records_are_counted_once(transcript):
    per_model, seen = reporter.parse_transcript(transcript)
    assert len(seen) == 3, "three unique messages, despite seven records"
    assert per_model["claude-sonnet-5"]["messages"] == 2


def test_summing_records_instead_of_messages_would_have_overstated_spend(transcript):
    per_model, _ = reporter.parse_transcript(transcript)
    honest = per_model["claude-sonnet-5"]["tokens_out"]
    naive = 567 * 3 + 1152          # counting all seven records
    assert honest == 567 + 1152
    assert (naive - honest) / honest > 0.5


def test_synthetic_zero_token_records_are_ignored(tmp_path):
    """Claude Code emits `<synthetic>` records with every count at zero. They
    are not spend, and reporting one would be rejected — failing the run and
    holding back the message log with it. Found by parsing a real transcript."""
    path = tmp_path / "synthetic.jsonl"
    zero = _assistant("msg_Z", "<synthetic>")
    zero["message"]["stop_reason"] = "stop_sequence"
    path.write_text(json.dumps(zero) + "\n"
                    + json.dumps(_assistant("msg_R", "claude-sonnet-5", fresh=3, out=4)) + "\n")
    per_model, seen = reporter.parse_transcript(path)
    assert "<synthetic>" not in per_model
    assert set(per_model) == {"claude-sonnet-5"}
    assert len(seen) == 2, "the zero record is still marked seen, so it is not retried"


# ── the three input buckets ───────────────────────────────────────────────

def test_all_three_input_buckets_are_aggregated_separately(transcript):
    per_model, _ = reporter.parse_transcript(transcript)
    agg = per_model["claude-sonnet-5"]
    assert agg["fresh_in"] == 2 + 5
    assert agg["cache_write_1h_in"] == 115_652
    assert agg["cache_write_5m_in"] == 4_115
    assert agg["cache_read_in"] == 40_869 + 156_521


def test_tokens_in_is_the_sum_of_the_buckets_not_the_fresh_count(transcript):
    per_model, _ = reporter.parse_transcript(transcript)
    body = reporter.payload_for("claude-sonnet-5", per_model["claude-sonnet-5"], "a", "s")
    agg = per_model["claude-sonnet-5"]
    assert body["tokens_in"] == (agg["fresh_in"] + agg["cache_read_in"]
                                 + agg["cache_write_5m_in"] + agg["cache_write_1h_in"])
    assert body["tokens_in"] > agg["fresh_in"] * 1000, (
        "reading input_tokens as the total would drop nearly every token billed")
    assert body["cache_hit_in"] == agg["cache_read_in"]
    assert body["cache_write_1h_in"] == 115_652


def test_an_unsplit_cache_write_falls_back_to_the_5m_tier(tmp_path):
    path = tmp_path / "unsplit.jsonl"
    record = _assistant("msg_X", "claude-sonnet-5", fresh=1, out=1)
    record["message"]["usage"]["cache_creation_input_tokens"] = 5_000   # no split
    path.write_text(json.dumps(record) + "\n")
    per_model, _ = reporter.parse_transcript(path)
    assert per_model["claude-sonnet-5"]["cache_write_5m_in"] == 5_000
    assert per_model["claude-sonnet-5"]["cache_write_1h_in"] == 0


def test_each_model_is_reported_separately(transcript):
    per_model, _ = reporter.parse_transcript(transcript)
    assert set(per_model) == {"claude-sonnet-5", "claude-haiku-4-5"}


# ── idempotency: a hook that runs twice must not bill twice ───────────────

def test_already_reported_messages_are_skipped(transcript):
    per_model, seen = reporter.parse_transcript(transcript)
    again, seen_again = reporter.parse_transcript(transcript, already_reported=seen)
    assert again == {}, "a second run re-reported the same session"
    assert seen_again == set()


def test_a_session_that_continues_only_reports_the_new_messages(transcript):
    _, seen = reporter.parse_transcript(transcript)
    with transcript.open("a") as f:
        f.write(json.dumps(_assistant("msg_D", "claude-sonnet-5", fresh=7, out=9)) + "\n")
    per_model, _ = reporter.parse_transcript(transcript, already_reported=seen)
    assert per_model["claude-sonnet-5"]["messages"] == 1
    assert per_model["claude-sonnet-5"]["tokens_out"] == 9


def test_the_reported_log_round_trips(transcript, tmp_path):
    _, seen = reporter.parse_transcript(transcript)
    reporter.save_state(transcript, seen)
    assert reporter.load_state(transcript) == seen


# ── dry run ───────────────────────────────────────────────────────────────

def test_dry_run_prints_the_payloads_and_posts_nothing(transcript, capsys):
    code = reporter.main(["--transcript", str(transcript), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert '"cache_write_1h_in": 115652' in out
    assert '"tokens_in"' in out
    assert not reporter.load_state(transcript), "a dry run advanced the reported log"


def test_reporting_without_an_identity_is_refused(transcript, capsys):
    code = reporter.main(["--transcript", str(transcript)])
    assert code == 1
    assert "agent identity is required" in capsys.readouterr().err


# ── a real transcript, if one is on this machine ──────────────────────────

def test_against_a_real_transcript_if_present():
    """Grounding: the synthetic fixtures above are only as good as their
    resemblance to the real thing, so parse the real thing when it exists."""
    projects = Path.home() / ".claude" / "projects"
    candidates = sorted(projects.glob("*/*.jsonl"), key=lambda p: p.stat().st_size
                        if p.exists() else 0, reverse=True)
    if not candidates:
        pytest.skip("no real transcripts on this machine")
    per_model, seen = reporter.parse_transcript(candidates[0])
    assert seen, "a real transcript parsed to nothing"
    for model, agg in per_model.items():
        total = (agg["fresh_in"] + agg["cache_read_in"]
                 + agg["cache_write_5m_in"] + agg["cache_write_1h_in"])
        assert total > 0
        assert agg["messages"] >= 1
        # dedupe must hold on real data, where the duplicates actually occur
        assert agg["messages"] <= len(seen)


# ── what the server does with it ──────────────────────────────────────────

class _Fake(BaseHTTPRequestHandler):
    replies: dict = {}
    seen: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        _Fake.seen.append(body)
        status, payload = _Fake.replies.get(body["model"], (200, {"amount_cents": 42, "priced": "auto"}))
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_server():
    _Fake.seen = []
    _Fake.replies = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_an_unpriced_model_still_records_its_tokens(transcript, fake_server, monkeypatch, capsys):
    monkeypatch.setattr(reporter, "STATE_DIR", transcript.parent / "state")
    _Fake.replies["claude-sonnet-5"] = (422, {"error": {"code": "model_not_priced"}})
    code = reporter.main(["--transcript", str(transcript), "--api-base", fake_server,
                          "--agent-id", "a", "--agent-secret", "s"])
    err = capsys.readouterr().err
    assert "no price entry" in err
    fallback = [b for b in _Fake.seen if b["rail"] == "tokens"]
    assert fallback, "the token burn was dropped instead of recorded at $0"
    assert code == 1, "an unpriced model must not pass silently"


def test_a_budget_block_is_surfaced_loudly(transcript, fake_server, monkeypatch, capsys):
    monkeypatch.setattr(reporter, "STATE_DIR", transcript.parent / "state")
    _Fake.replies["claude-sonnet-5"] = (402, {"error": {"code": "budget_exceeded",
                                                        "message": "over its monthly cap"}})
    code = reporter.main(["--transcript", str(transcript), "--api-base", fake_server,
                          "--agent-id", "a", "--agent-secret", "s"])
    err = capsys.readouterr().err
    assert "BLOCKED" in err and "over its monthly cap" in err
    assert code == 1


def test_a_failed_run_does_not_advance_the_reported_log(transcript, fake_server, monkeypatch):
    monkeypatch.setattr(reporter, "STATE_DIR", transcript.parent / "state")
    _Fake.replies["claude-sonnet-5"] = (500, {"error": {"code": "server_error"}})
    _Fake.replies["claude-haiku-4-5"] = (200, {"amount_cents": 1, "priced": "auto"})
    reporter.main(["--transcript", str(transcript), "--api-base", fake_server,
                   "--agent-id", "a", "--agent-secret", "s"])
    assert not reporter.load_state(transcript), (
        "the window was advanced despite a failure — those tokens are now lost")


def test_a_clean_run_advances_the_log(transcript, fake_server, monkeypatch):
    monkeypatch.setattr(reporter, "STATE_DIR", transcript.parent / "state")
    code = reporter.main(["--transcript", str(transcript), "--api-base", fake_server,
                          "--agent-id", "a", "--agent-secret", "s"])
    assert code == 0
    assert reporter.load_state(transcript), "a clean run left the log unadvanced"
    assert len([b for b in _Fake.seen if b["rail"] == "api_key"]) == 2
