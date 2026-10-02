#!/usr/bin/env python3
"""The ONE place that resolves a request's client IP.

Why this exists (red-team review): the deployment runs uvicorn with
`--proxy-headers --forwarded-allow-ips="*"` (railpack.json). With every hop
trusted, uvicorn's ProxyHeadersMiddleware walks X-Forwarded-For to the
LEFTMOST entry — so `request.client.host` is whatever the client wrote in its
own XFF header. Every rate limiter keyed on it (`/start` mints, satellite
bridges) was one `X-Forwarded-For: <random>` header away from a full bypass.

Railway's edge APPENDS the real client IP as the rightmost XFF entry. That
entry cannot be forged — a client can prepend anything to XFF but cannot
change what the edge added — so we take the LAST entry, never the first.

When no XFF is present (direct connection, tests, non-proxied deploys), fall
back to the TCP peer. Set AL_CLIENT_IP_FROM_XFF=0 to ignore XFF entirely
(a deployment with no trusted edge must NOT read it: with no proxy adding a
hop, the rightmost entry is client-chosen).
"""
import os


def client_ip(request) -> str:
    """Best-available real client IP for rate limiting and telemetry keys."""
    if os.environ.get("AL_CLIENT_IP_FROM_XFF", "1") != "0":
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            # Rightmost = appended by our edge; leftmost is attacker-controlled.
            return fwd.split(",")[-1].strip()[:45] or _peer(request)
    return _peer(request)


def _peer(request) -> str:
    return (request.client.host if request.client else None) or "unknown"
