#!/usr/bin/env python3
"""Tests for launch-kit v0.3 item 2 — MCP meta-tools (ledger_api_docs, ledger_examples).

Covers both hosted (al_mcp_http.py) and stdio (mcp_server.py) implementations,
and the shared docs_content.py content module they both call into.

Run with: /opt/miniconda3/bin/python3 -m pytest -q
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import docs_content

TOPICS = ("quickstart", "mcp", "rest", "errors", "idempotency")
PATTERNS = ("python_tracking", "budget_enforcement", "weekly_report", "retry_safe_writes")


@pytest.fixture()
def data_dir(monkeypatch):
    tmp_dir = tempfile.mkdtemp(prefix="agent-ledger-test-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp_dir)
    for mod in ("al_mcp_http", "mcp_server", "metrics"):
        sys.modules.pop(mod, None)
    yield tmp_dir
    shutil.rmtree(tmp_dir, ignore_errors=True)


def _is_markdown(text: str) -> bool:
    return isinstance(text, str) and len(text.strip()) > 0 and "##" in text


# --- docs_content.py directly -------------------------------------------------

@pytest.mark.parametrize("topic", TOPICS)
def test_get_api_docs_each_topic(topic):
    md = docs_content.get_api_docs(topic)
    assert _is_markdown(md)


def test_get_api_docs_all():
    md = docs_content.get_api_docs("all")
    assert _is_markdown(md)
    for topic in TOPICS:
        # every topic's heading should appear somewhere in the full doc
        assert docs_content.DOCS_TOPICS[topic].split("\n", 1)[0] in md


def test_get_api_docs_unknown_topic_falls_back_to_full_docs():
    fallback = docs_content.get_api_docs("nonsense-topic")
    full = docs_content.get_api_docs("all")
    assert fallback == full
    assert _is_markdown(fallback)


def test_get_api_docs_default_is_all():
    assert docs_content.get_api_docs("") == docs_content.get_api_docs("all")


@pytest.mark.parametrize("pattern", PATTERNS)
def test_get_example_each_pattern(pattern):
    code = docs_content.get_example(pattern)
    assert isinstance(code, str) and len(code) > 0
    assert "AgentLedger" in code
    compile(code, f"<recipe:{pattern}>", "exec")  # must be valid, runnable Python


def test_get_example_unknown_pattern():
    result = docs_content.get_example("not-a-real-pattern")
    assert "Unknown pattern" in result
    for pattern in PATTERNS:
        assert pattern in result


# --- al_mcp_http.py meta-tools -------------------------------------------------

@pytest.mark.parametrize("topic", TOPICS + ("all", ""))
def test_al_mcp_http_ledger_api_docs(data_dir, topic):
    import al_mcp_http
    result = al_mcp_http.ledger_api_docs(topic)
    assert _is_markdown(result["markdown"])


def test_al_mcp_http_ledger_api_docs_unknown_topic_falls_back(data_dir):
    import al_mcp_http
    result = al_mcp_http.ledger_api_docs("nonsense-topic")
    assert result["topic"] == "all"
    assert result["markdown"] == docs_content.get_api_docs("all")


@pytest.mark.parametrize("pattern", PATTERNS)
def test_al_mcp_http_ledger_examples(data_dir, pattern):
    import al_mcp_http
    result = al_mcp_http.ledger_examples(pattern)
    assert result["pattern"] == pattern
    compile(result["code"], f"<recipe:{pattern}>", "exec")


def test_al_mcp_http_meta_tools_record_metrics(data_dir):
    import al_mcp_http
    import metrics

    al_mcp_http.ledger_api_docs("quickstart")
    al_mcp_http.ledger_examples("weekly_report")

    snap = metrics.snapshot()
    assert snap["totals"].get("meta_doc_call", 0) >= 2


# --- mcp_server.py meta-tools --------------------------------------------------

@pytest.mark.parametrize("topic", TOPICS)
def test_mcp_server_ledger_api_docs(data_dir, topic):
    import mcp_server
    result = mcp_server.ledger_api_docs(topic)
    assert _is_markdown(result["markdown"])


def test_mcp_server_ledger_api_docs_unknown_topic_falls_back(data_dir):
    import mcp_server
    result = mcp_server.ledger_api_docs("nonsense-topic")
    assert result["topic"] == "all"
    assert result["markdown"] == docs_content.get_api_docs("all")


@pytest.mark.parametrize("pattern", PATTERNS)
def test_mcp_server_ledger_examples(data_dir, pattern):
    import mcp_server
    result = mcp_server.ledger_examples(pattern)
    assert result["pattern"] == pattern
    compile(result["code"], f"<recipe:{pattern}>", "exec")


def test_mcp_server_meta_tools_record_metrics(data_dir):
    import mcp_server
    import metrics

    mcp_server.ledger_api_docs("errors")
    mcp_server.ledger_examples("retry_safe_writes")

    snap = metrics.snapshot()
    assert snap["totals"].get("meta_doc_call", 0) >= 2
