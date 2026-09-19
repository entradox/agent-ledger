"""Tests for routes_satellite.py — the public human-facing JSON bridges.

Covers the pure logic (levenshtein, typosquat screen, URL safety gate, npm
name validation). The upstream HTTP calls are not exercised here — they are
probed live before each deploy instead, because their contracts live on
someone else's infrastructure.
"""
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import routes_satellite as rs


def test_lev_exact_and_near():
    assert rs._lev("lodash", "lodash") == 0
    assert rs._lev("lodahs", "lodash") == 2
    assert rs._lev("express", "express") == 0
    assert rs._lev("abc", "xyz") > 2  # capped


def test_typosquat_flags_close_names():
    hits = rs._typosquat_hits("lodahs")
    assert "lodash" in hits


def test_typosquat_ignores_exact_and_distant():
    assert rs._typosquat_hits("express") == []
    assert rs._typosquat_hits("my-totally-unique-widget-123") == []


def test_typosquat_scoped_name():
    hits = rs._typosquat_hits("@foo/lodahs")
    assert "lodash" in hits


def test_npm_name_re():
    assert rs.NPM_NAME_RE.match("express")
    assert rs.NPM_NAME_RE.match("@modelcontextprotocol/sdk")
    assert rs.NPM_NAME_RE.match("my-pkg_2")
    assert not rs.NPM_NAME_RE.match("EXPRESS")
    assert not rs.NPM_NAME_RE.match("")
    assert not rs.NPM_NAME_RE.match("../evil")


def _fake_dns(monkeypatch, ip):
    def fake(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)


def test_url_gate_rejects_non_http(monkeypatch):
    assert rs._url_is_public("ftp://example.com/") is not None


def test_url_gate_rejects_private_ip(monkeypatch):
    _fake_dns(monkeypatch, "192.168.1.10")
    assert rs._url_is_public("https://example.com/") is not None


def test_url_gate_rejects_loopback(monkeypatch):
    _fake_dns(monkeypatch, "127.0.0.1")
    assert rs._url_is_public("http://localhost:3000/") is not None


def test_url_gate_accepts_public_ip(monkeypatch):
    _fake_dns(monkeypatch, "93.184.216.34")  # example.com, documentation range
    assert rs._url_is_public("https://example.com/mcp/") is None


def test_url_gate_rejects_credentials(monkeypatch):
    _fake_dns(monkeypatch, "93.184.216.34")
    assert rs._url_is_public("https://user:pass@example.com/") is not None


def test_url_gate_rejects_unresolvable(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("nope")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert rs._url_is_public("https://does-not-exist-xyz.invalid/") is not None
