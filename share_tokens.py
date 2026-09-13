#!/usr/bin/env python3
"""Signed, expiring, read-only links to an agent's report page (D-1217).

Why this module exists: the report page is the product's only human-facing
surface, and until now it could not be opened by a human. Reads require an
`X-Agent-Secret` or `X-Workspace-Key` HEADER, and a browser cannot send one —
so the "shareable link" the API docs advertised returned raw JSON to anyone who
clicked it. A link you cannot open is not a feature.

The token is deliberately weak on purpose and narrow in scope:

  * it can only READ, and only the ONE agent_id it was minted for;
  * it cannot write, rotate, revoke, or read any other agent;
  * it expires (default 7 days, hard max 90);
  * it can be killed in bulk by bumping the agent's share epoch.

Signing key lives on the data volume, created once. If the volume is ever lost
the key is regenerated and every outstanding link dies — which is the safe
direction to fail for a credential, and is why the key is not derived from
anything the operator has to remember.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Tuple

MAX_TTL_DAYS = 90
DEFAULT_TTL_DAYS = 7
_PARAM = "t"


def _data_dir() -> Path:
    return Path(os.environ.get("AGENT_LEDGER_DATA",
                               os.path.expanduser("~/.agent-ledger")))


def _signing_key() -> bytes:
    """Stable across restarts and deploys: it lives on the data volume."""
    path = _data_dir() / "share_secret.key"
    if not path.exists():
        _data_dir().mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(48))
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
    return path.read_text().strip().encode()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ── per-agent epoch = bulk revocation without storing issued tokens ─────────

def _epoch_path(agent_id: str) -> Path:
    return _data_dir() / "agents" / agent_id / "share_epoch.txt"


def current_epoch(agent_id: str) -> int:
    path = _epoch_path(agent_id)
    if not path.exists():
        return 0
    try:
        return int(path.read_text().strip())
    except Exception:
        return 0


def bump_epoch(agent_id: str) -> int:
    """Invalidate every outstanding link for this agent. Idempotent in effect:
    the tokens issued before the bump stop verifying, the ones after do not
    exist yet."""
    epoch = current_epoch(agent_id) + 1
    path = _epoch_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(epoch))
    return epoch


# ── mint / verify ──────────────────────────────────────────────────────────

def mint(agent_id: str, ttl_days: int = DEFAULT_TTL_DAYS) -> Tuple[str, float]:
    """Returns (token, expires_at_unix)."""
    ttl_days = max(1, min(int(ttl_days), MAX_TTL_DAYS))
    expires_at = time.time() + ttl_days * 86400
    payload = {"a": agent_id, "e": expires_at,
               "n": secrets.token_urlsafe(8), "v": current_epoch(agent_id)}
    raw = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(_signing_key(), raw.encode(), hashlib.sha256).digest())
    return f"{raw}.{sig}", expires_at


def verify(token: str, agent_id: str) -> Tuple[bool, str]:
    """True only if the token is intact, unexpired, unrevoked, and was minted
    for exactly this agent_id. Returns (ok, reason); reason feeds the error
    page, and is deliberately vague to the caller-facing page while precise
    here."""
    if not token or "." not in token:
        return False, "malformed"
    raw, _, sig = token.partition(".")
    expected = _b64e(hmac.new(_signing_key(), raw.encode(), hashlib.sha256).digest())
    # Constant-time: a timing side-channel here would let someone forge a
    # signature byte by byte.
    if not hmac.compare_digest(sig, expected):
        return False, "bad_signature"
    try:
        payload = json.loads(_b64d(raw))
    except Exception:
        return False, "malformed"
    if payload.get("a") != agent_id:
        return False, "wrong_agent"
    if float(payload.get("e", 0)) < time.time():
        return False, "expired"
    if int(payload.get("v", -1)) != current_epoch(agent_id):
        return False, "revoked"
    return True, "ok"


# ── the human-facing error page (never raw JSON) ───────────────────────────

_REASONS = {
    "expired": ("This link has expired.",
                "Ask the agent's owner for a fresh one — they take seconds to mint."),
    "revoked": ("This link was revoked.",
                "The owner turned off sharing for this agent."),
    "wrong_agent": ("This link is not valid for that agent.",
                    "A share link works for exactly one agent."),
    "bad_signature": ("This link is not valid.", "It may have been copied incompletely."),
    "malformed": ("This link is not valid.", "It may have been copied incompletely."),
}


def error_page(reason: str) -> str:
    title, hint = _REASONS.get(reason, ("This link is not valid.", ""))
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>AgentLedger — link unavailable</title>
<meta name="robots" content="noindex">
<style>
body{{background:#0d1117;color:#e6edf3;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;
padding:48px 20px;line-height:1.6}}
.w{{max-width:520px;margin:0 auto}}
.brand{{font-size:12px;color:#8b949e;letter-spacing:.06em;text-transform:uppercase}}
h1{{font-size:22px;margin:10px 0 6px}}
p{{color:#8b949e;font-size:14px;margin:6px 0}}
a{{color:#79c0ff}}
</style></head><body><div class="w">
<div class="brand">AgentLedger</div>
<h1>{title}</h1>
<p>{hint}</p>
<p style="margin-top:20px"><a href="/">What is AgentLedger?</a></p>
</div></body></html>"""
