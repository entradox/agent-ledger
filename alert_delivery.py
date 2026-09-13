#!/usr/bin/env python3
"""AgentLedger — alert delivery (D-1218).

The problem this solves, in the audit's words: "A spike alert you must
remember to check is not an alert." Budget warnings and anomalies existed only
inside `GET /v1/alerts/{agent_id}` — a feed the user had to poll. Nothing was
ever pushed. This module pushes.

Design notes that matter:

* `dispatch()` is called from `ledger_engine._log_alert`, the single sink every
  alert already flows through. Hooking there means a NEW alert type cannot be
  silently undelivered — the failure mode of hooking at each call site.
* Delivery is fire-and-forget on a daemon thread. A slow or dead endpoint must
  never slow down, or break, a write. Same posture as the onboarding mailer:
  transport failure never fails the operation.
* Every attempt is recorded, success or failure. "Delivered" and "gave up" look
  different in `GET /v1/webhooks/deliveries`, because a silent drop is the
  exact thing this feature exists to prevent.
* Payloads carry cost metadata only — event, agent_id, message, timestamp.
  Never a secret, never a prompt, never a response. Asserted in tests.
* Webhook URLs are chosen by the caller and fetched by our server, so they are
  an SSRF surface. Private/loopback/link-local targets are refused unless
  AL_ALLOW_PRIVATE_WEBHOOKS=1 (set only in tests).

**Email delivery was removed 2026-09-13 (principal directive).** Registration
takes an http(s) URL only. The product does not send mail on a user's behalf:
the operator's SMTP rail exists to mail the operator about checkout, not to
become a relaying surface for arbitrary addresses. Anything that needs a human
notification goes to a webhook the user owns — Slack, Discord, Zapier, their
own endpoint.
"""
import ipaddress
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

EVENTS = ("alert.raised", "budget.warning", "budget.exceeded", "anomaly.detected")
MAX_WEBHOOKS_PER_WORKSPACE = 10
DELIVERY_TIMEOUT_SECONDS = 5
# First attempt, then the retries. Overridable so tests don't sleep for 30s.
RETRY_DELAYS = (1.0, 5.0, 25.0)
DELIVERY_LOG_KEEP = 200


def _data_dir() -> Path:
    return Path(os.environ.get("AGENT_LEDGER_DATA",
                               os.path.expanduser("~/.agent-ledger")))


def _registry_path(workspace_id: str) -> Path:
    return _data_dir() / "webhooks" / f"{workspace_id}.json"


def _delivery_log_path(workspace_id: str) -> Path:
    return _data_dir() / "webhooks" / f"{workspace_id}.deliveries.jsonl"


# ── event mapping: one place, so nothing falls through ─────────────────────

def event_for(alert_type: str) -> str:
    """Map an internal alert type onto a public event name.

    Deliberately total: an unrecognised type becomes `alert.raised` rather
    than being dropped, so subscribing to the generic event can never silently
    miss something.
    """
    if alert_type in ("budget_warning", "token_budget_warning"):
        return "budget.warning"
    if alert_type in ("budget_exceeded", "token_budget_exceeded", "budget_blocked"):
        return "budget.exceeded"
    if "anomal" in alert_type or "spike" in alert_type:
        return "anomaly.detected"
    return "alert.raised"


# ── registry ───────────────────────────────────────────────────────────────

class WebhookError(Exception):
    """Invalid registration the caller must fix (bad URL, too many, unknown id)."""


def _is_public_target(url: str) -> bool:
    host = urlparse(url).hostname or ""
    if host in ("localhost", ""):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast):
            return False
    return True


def validate_url(url: str) -> str:
    """http(s) only. An email-shaped string is rejected here — the product has
    no mail rail by design (see the module docstring)."""
    url = (url or "").strip()
    if not url:
        raise WebhookError("url is required")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise WebhookError("url must be http:// or https:// — email delivery "
                           "is not offered; point a webhook at your own service")
    if not parsed.hostname:
        raise WebhookError("url must include a host")
    if os.environ.get("AL_ALLOW_PRIVATE_WEBHOOKS", "") != "1" and not _is_public_target(url):
        raise WebhookError("url must resolve to a public address")
    return url


def load_registry(workspace_id: str) -> list:
    path = _registry_path(workspace_id)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def _save_registry(workspace_id: str, entries: list) -> None:
    path = _registry_path(workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2))


def register(workspace_id: str, url: str, events: Optional[list] = None,
             label: str = "") -> dict:
    entries = load_registry(workspace_id)
    if len(entries) >= MAX_WEBHOOKS_PER_WORKSPACE:
        raise WebhookError(
            f"at most {MAX_WEBHOOKS_PER_WORKSPACE} destinations per workspace")
    url = validate_url(url)
    selected = [e for e in (events or list(EVENTS)) if e in EVENTS]
    if not selected:
        raise WebhookError(f"events must be a non-empty subset of {list(EVENTS)}")
    entry = {"id": "wh_" + os.urandom(8).hex(), "url": url,
             "events": selected, "label": label, "created_at": time.time()}
    entries.append(entry)
    _save_registry(workspace_id, entries)
    return entry


def unregister(workspace_id: str, webhook_id: str) -> bool:
    entries = load_registry(workspace_id)
    keep = [e for e in entries if e.get("id") != webhook_id]
    if len(keep) == len(entries):
        return False
    _save_registry(workspace_id, keep)
    return True


# ── delivery ───────────────────────────────────────────────────────────────

def _record(workspace_id: str, record: dict) -> None:
    try:
        path = _delivery_log_path(workspace_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def recent_deliveries(workspace_id: str, limit: int = 50) -> list:
    path = _delivery_log_path(workspace_id)
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()[-DELIVERY_LOG_KEEP:]
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
    except Exception:
        return []


def deliver_once(entry: dict, payload: dict) -> None:
    """One attempt. Raises on failure so the caller can retry and record."""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        entry["url"], data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "agent-ledger-webhooks/1",
                 "X-AgentLedger-Event": payload["event"]})
    with urllib.request.urlopen(req, timeout=DELIVERY_TIMEOUT_SECONDS) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"endpoint returned {resp.status}")


def deliver(workspace_id: str, entry: dict, payload: dict) -> dict:
    """Attempt with retries; record the outcome either way. Never raises."""
    attempts = 0
    error = ""
    for delay in (0.0,) + RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        attempts += 1
        try:
            deliver_once(entry, payload)
            record = {"ts": time.time(), "webhook_id": entry["id"],
                      "url": entry["url"], "event": payload["event"],
                      "agent_id": payload["agent_id"], "status": "delivered",
                      "attempts": attempts}
            _record(workspace_id, record)
            return record
        except Exception as exc:  # noqa: BLE001 — transport of every kind
            error = f"{type(exc).__name__}: {exc}"[:300]
    record = {"ts": time.time(), "webhook_id": entry["id"], "url": entry["url"],
              "event": payload["event"], "agent_id": payload["agent_id"],
              "status": "failed", "attempts": attempts, "error": error}
    _record(workspace_id, record)
    return record


def dispatch(event: str, *, agent_id: str, message: str,
             workspace_id: Optional[str] = None) -> int:
    """Fan an event out to every matching destination. Returns how many were
    queued. Never raises — it is called from the write path."""
    try:
        if not workspace_id:
            import identity
            workspace_id = identity.workspace_of_agent(agent_id)
        if not workspace_id:
            return 0
        targets = [e for e in load_registry(workspace_id) if event in e.get("events", [])]
        if not targets:
            return 0
        base = os.environ.get("AL_PUBLIC_BASE_URL",
                              "https://aiagentscity.com")
        payload = {"event": event, "agent_id": agent_id, "message": message,
                   "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "report_url": f"{base}/v1/report/{agent_id}"}
        for entry in targets:
            threading.Thread(target=deliver,
                             args=(workspace_id, entry, payload),
                             daemon=True).start()
        return len(targets)
    except Exception:
        return 0
