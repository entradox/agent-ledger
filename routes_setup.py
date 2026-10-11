#!/usr/bin/env python3
"""POST /v1/setup-event — the funnel sink for `npx @aiagentscity/setup`.

The CLI reports one event per harness outcome (plus a 'started' per run) so
connect-page → command-run → MCP-registered is measurable for the first time.
This endpoint is deliberately unauthenticated-ish: the installer runs before
the caller has any credential, so auth would drop exactly the events the
funnel exists to see. What keeps that honest:

- every field is an enum or a bounded format — the schema is the sanitizer,
  and extra fields are a 422, not a shrug;
- the rate guard runs inside the route handler BEFORE FastAPI's body
  decode, so malformed traffic hits the same cap as valid traffic;
- the per-IP cap is backed by a global hourly cap, so a forged-rotation
  X-Forwarded-For cannot turn the limit into an unbounded write;
- nothing but the enumerated fields is persisted (no config content, no
  detail text, no headers — a credential-shaped `x-al-key-lookup` is
  accepted-and-dropped: nothing reads it, so nothing stores it);
- attempt ids are only trusted when the site actually minted them: the
  connect pages record `setup_attempt_minted` at render, so a fabricated
  uuid is counted as `unmatched`, never as a conversion.
"""
import os
import re
import time
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from client_ip import client_ip
import metrics

# These enums are the contract the CLI's lib/telemetry.js mirrors. Adding a
# value is a deliberate version bump on BOTH sides, not an accident of drift.
SETUP_HARNESSES = (
    "claude-code", "codex", "cursor", "opencode", "hermes", "openclaw",
)
SETUP_PRODUCTS = ("agent-ledger",)
SETUP_OUTCOMES = (
    "started", "registered", "already_registered", "manual",
    "failed", "dry_run", "skill_installed",
)
SETUP_FAILURE_REASONS = (
    "harness_not_supported", "command_not_found", "spawn_failed",
    "config_corrupt", "write_failed", "declined",
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._+-]{1,32}$")
_PLATFORM_RE = re.compile(r"^[a-z0-9]{1,16}$")


class SetupEventRequest(BaseModel):
    """Closed schema — extra fields are a 422 so a drifting client is told,
    not silently absorbed."""
    model_config = ConfigDict(extra="forbid")

    product: Literal["agent-ledger"]
    outcome: Literal["started", "registered", "already_registered", "manual",
                     "failed", "dry_run", "skill_installed"]
    harness: Optional[Literal["claude-code", "codex", "cursor", "opencode",
                              "hermes", "openclaw"]] = None
    is_human_initiated: bool
    connect_attempt_id: Optional[str] = Field(default=None, pattern=_UUID_RE.pattern)
    failure_reason: Optional[Literal["harness_not_supported", "command_not_found",
                                     "spawn_failed", "config_corrupt",
                                     "write_failed", "declined"]] = None
    cli_version: Optional[str] = Field(default=None, max_length=32,
                                       pattern=_SAFE_TOKEN_RE.pattern)
    runtime_platform: Optional[str] = Field(default=None, max_length=16,
                                            pattern=_PLATFORM_RE.pattern)


# Rate limit: real installs produce a handful of events per machine; 60/hour
# per IP is generous for a fleet-wide setup run and useless for flooding the
# funnel. Two buckets per request:
#   - per-IP (client_ip's rightmost-XFF — forgeable only when no trusted edge
#     sits in front, so it cannot be the whole defense), and
#   - a global backstop, so rotating a forged XFF still hits a wall.
# Same bounded-table shape as _CITY_RATE — an unauthenticated per-IP dict is
# itself a memory vector, so it is capped and evicted.
SETUP_EVENT_RATE_LIMIT = int(os.environ.get("SETUP_EVENT_RATE_LIMIT", "60"))
SETUP_EVENT_GLOBAL_LIMIT = int(os.environ.get("SETUP_EVENT_GLOBAL_LIMIT", "1200"))
SETUP_EVENT_RATE_WINDOW = 3600
SETUP_RATE_MAX_KEYS = 5000
_SETUP_RATE: dict[str, list[float]] = {}


def _setup_event_guard(request: Request) -> None:
    now = time.time()
    for key, limit in ((client_ip(request), SETUP_EVENT_RATE_LIMIT),
                       ("*global*", SETUP_EVENT_GLOBAL_LIMIT)):
        hits = [t for t in _SETUP_RATE.get(key, []) if now - t < SETUP_EVENT_RATE_WINDOW]
        if len(hits) >= limit:
            raise HTTPException(429, "Setup-event rate limit reached; try again later.")
        hits.append(now)
        _SETUP_RATE[key] = hits
    if len(_SETUP_RATE) > SETUP_RATE_MAX_KEYS:
        for k in [k for k, v in _SETUP_RATE.items()
                  if not v or now - v[-1] >= SETUP_EVENT_RATE_WINDOW]:
            del _SETUP_RATE[k]
        while len(_SETUP_RATE) > SETUP_RATE_MAX_KEYS:
            oldest = min(_SETUP_RATE, key=lambda k: _SETUP_RATE[k][-1] if _SETUP_RATE[k] else float("-inf"))
            del _SETUP_RATE[oldest]


class _SetupEventRoute(APIRoute):
    """Run the rate guard before FastAPI touches the body at all. A Depends()
    still sits downstream of the JSON decode — a malformed-body flood raised
    RequestValidationError before the dependency ever ran (review round 2).
    Wrapping the route handler is the earliest seam this endpoint controls."""
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            _setup_event_guard(request)
            return await original(request)

        return handler


router = APIRouter(route_class=_SetupEventRoute)


@router.post("/v1/setup-event")
def setup_event(body: SetupEventRequest):
    """Record one connect-funnel event. Returns {"ok": true} — the CLI treats
    reporting as best-effort, and a 200 here is an ack of receipt, not a claim
    about the install.

    Header note: the CLI may send `x-al-key-lookup` (a SHA-256) for products
    that carry a key. Nothing on this service reads it, so it is deliberately
    NOT persisted — storing an identifier no one consumes is data collection
    without a purpose.
    """
    fields = {
        "product": body.product,
        "outcome": body.outcome,
        "is_human_initiated": body.is_human_initiated,
    }
    if body.harness is not None:
        fields["harness"] = body.harness
    if body.connect_attempt_id:
        fields["connect_attempt_id"] = body.connect_attempt_id.lower()
    if body.failure_reason:
        fields["failure_reason"] = body.failure_reason
    if body.cli_version:
        fields["cli_version"] = body.cli_version
    if body.runtime_platform:
        fields["runtime_platform"] = body.runtime_platform

    metrics.record_event("setup_event", **fields)
    return {"ok": True}
