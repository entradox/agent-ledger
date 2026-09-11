#!/usr/bin/env python3
"""Signed session cookies — HMAC-SHA256, stdlib only (no itsdangerous
dependency). Format: "<workspace_id>.<hex signature>"."""
import hmac
import hashlib
import os


def _secret() -> bytes:
    s = os.environ.get("AL_SESSION_SECRET", "")
    if not s:
        raise RuntimeError("AL_SESSION_SECRET not configured — refusing to sign sessions")
    return s.encode()


def sign_session(workspace_id: str) -> str:
    sig = hmac.new(_secret(), workspace_id.encode(), hashlib.sha256).hexdigest()
    return f"{workspace_id}.{sig}"


def verify_session(cookie_value: str) -> str | None:
    if not cookie_value or "." not in cookie_value:
        return None
    workspace_id, _, sig = cookie_value.rpartition(".")
    expected = hmac.new(_secret(), workspace_id.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(sig, expected):
        return workspace_id
    return None
