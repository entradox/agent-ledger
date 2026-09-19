"""Public human-facing JSON bridges to the satellite products.

These power the interactive forms on /agent-watch and /trust-scan. Every
route here is read-only against the satellites (probe / registry lookup) and
runs the same validation a careful human would: SSRF guards on the probe
URL, strict npm-name validation, short timeouts, and a small per-IP rate
limit so the forms can't be used to hammer the backends.

Nothing here mints, writes, or spends. No auth, no keys.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
import urllib.parse
import urllib.request
from collections import deque
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter()

AW_PROBE_URL = "https://agent-watch-api-production.up.railway.app/v1/probe"
AW_WATCH_URL = "https://agent-watch-api-production.up.railway.app/v1/watch"
AW_CENSUS_URL = "https://agent-watch-api-production.up.railway.app/v1/census"
NPM_REGISTRY = "https://registry.npmjs.org"
NPM_DOWNLOADS = "https://api.npmjs.org/downloads/point/last-week"

_UA = {"User-Agent": "aiagentscity.com/satellite-bridge (public form)"}

# ── tiny per-IP rate limiter ────────────────────────────────────────────────
# {endpoint_key: {ip: deque[timestamps]}} — 12 requests / 60s per IP.
_RATE: dict[str, dict[str, deque]] = {}


def _limited(key: str, ip: str, n: int = 12, window: int = 60) -> bool:
    now = time.time()
    bucket = _RATE.setdefault(key, {}).setdefault(ip, deque())
    while bucket and now - bucket[0] > window:
        bucket.popleft()
    if len(bucket) >= n:
        return True
    bucket.append(now)
    return False


def _client_ip(request: Request) -> str:
    # Railway sits behind a proxy; X-Forwarded-For carries the real client.
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _get_json(url: str, timeout: int = 15) -> tuple[Optional[dict], Optional[str]]:
    """GET url, parse JSON. Returns (data, error)."""
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace")), None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, "not_found"
        return None, f"upstream HTTP {e.code}"
    except Exception as e:  # timeout, DNS, reset — never leak internals
        return None, "upstream unreachable"


def _post_json(url: str, payload: dict, timeout: int = 50) -> tuple[Optional[dict], Optional[str]]:
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={**_UA, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return (json.loads(body) if body.strip() else {}), None
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        return None, f"upstream HTTP {e.code} {detail}"
    except Exception:
        return None, "upstream unreachable"


# ── Agent Watch: probe ──────────────────────────────────────────────────────

class ProbeRequest(BaseModel):
    url: str = Field(max_length=2048)


def _url_is_public(raw: str) -> Optional[str]:
    """Return an error string, or None if the URL is safe to forward."""
    raw = (raw or "").strip()
    try:
        p = urllib.parse.urlsplit(raw)
    except Exception:
        return "that URL doesn't parse"
    if p.scheme not in ("http", "https"):
        return "only http:// and https:// URLs can be probed"
    if p.username or p.password:
        return "URLs with credentials can't be probed"
    host = (p.hostname or "").strip().lower()
    if not host:
        return "missing hostname"
    if len(raw) > 2048:
        return "URL too long"
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return "hostname doesn't resolve"
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return "hostname resolves oddly"
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_multicast or addr.is_reserved or addr.is_unspecified):
            return "that host isn't publicly reachable"
    return None


@router.post("/api/agent-watch/probe")
def aw_probe(req: ProbeRequest, request: Request):
    ip = _client_ip(request)
    if _limited("aw_probe", ip):
        return JSONResponse({"ok": False, "error": "rate limited — try again in a minute"},
                            status_code=429)
    err = _url_is_public(req.url)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=422)
    data, uerr = _post_json(AW_PROBE_URL, {"url": req.url.strip(), "timeout": 25},
                            timeout=55)
    if uerr:
        return JSONResponse({"ok": False, "error": "probe backend unreachable right now"},
                            status_code=502)
    try:
        import metrics
        metrics.record_event("aw_probe")
    except Exception:
        pass
    # Pass through only the fields the page renders — nothing else.
    data = data or {}
    return {"ok": True, "result": {
        "endpoint_url": data.get("endpoint_url") or req.url.strip(),
        "alive": data.get("alive"),
        "latency_p50_ms": data.get("latency_p50_ms"),
        "latency_p95_ms": data.get("latency_p95_ms"),
        "protocol": data.get("protocol"),
        "tool_count": data.get("tool_count"),
        "price_challenge_found": data.get("price_challenge_found"),
        "auth_metadata_present": data.get("auth_metadata_present"),
        "error": data.get("error"),
        "probed_at": data.get("probed_at"),
    }}


@router.get("/api/agent-watch/census")
def aw_census(request: Request):
    ip = _client_ip(request)
    if _limited("aw_census", ip):
        return JSONResponse({"ok": False, "error": "rate limited"}, status_code=429)
    data, uerr = _get_json(AW_CENSUS_URL, timeout=20)
    if uerr or not data:
        return JSONResponse({"ok": False, "error": "census unreachable right now"},
                            status_code=502)
    return {"ok": True, "census": {
        "total": data.get("total"), "alive": data.get("alive"),
        "dead": data.get("dead"), "unknown": data.get("unknown"),
        "pct_alive": data.get("pct_alive"),
    }}


@router.get("/api/agent-watch/watch")
def aw_watch_status(request: Request, email: str = "", token: str = ""):
    """Passthrough for customers who already hold a watch token."""
    ip = _client_ip(request)
    if _limited("aw_watch", ip):
        return JSONResponse({"ok": False, "error": "rate limited"}, status_code=429)
    email = (email or "").strip()
    token = (token or "").strip()
    if not email or not token or len(email) > 320 or len(token) > 200:
        return JSONResponse({"ok": False, "error": "email and token are required"},
                            status_code=422)
    q = urllib.parse.urlencode({"email": email, "token": token})
    data, uerr = _get_json(f"{AW_WATCH_URL}?{q}", timeout=20)
    if uerr:
        # The satellite answers 401/403 for bad tokens — surface honestly.
        return JSONResponse({"ok": False, "error": "invalid email/token or unreachable"},
                            status_code=502)
    return {"ok": True, "watch": data}


# ── TrustScan: npm package metadata check ───────────────────────────────────
# This is the package-metadata half of "scan before you trust": registry
# facts + a typosquat screen. The deep code scan (invisible-Unicode prompt
# injection, MCP001–MCP006, hardcoded secrets) runs over MCP — the page says
# so plainly instead of faking a score.

NPM_NAME_RE = re.compile(
    r"^(?:@[a-z0-9-~][a-z0-9-._~]*/)?[a-z0-9-~][a-z0-9-._~]*$")

# Widely-used package names the typosquat screen compares against. Curated,
# not exhaustive — the page says exactly that.
TOP_PACKAGES = [
    # runtimes / frameworks
    "express", "koa", "fastify", "hapi", "next", "nuxt", "gatsby", "remix",
    "react", "react-dom", "vue", "angular", "svelte", "solid-js", "preact",
    # language / build
    "typescript", "webpack", "vite", "esbuild", "rollup", "parcel", "babel",
    "eslint", "prettier", "jest", "vitest", "mocha", "chai", "sinon",
    # utilities
    "lodash", "underscore", "ramda", "axios", "node-fetch", "got",
    "commander", "yargs", "chalk", "inquirer", "ora", "dotenv", "cors",
    "moment", "dayjs", "date-fns", "uuid", "nanoid", "validator",
    # backend
    "mongoose", "sequelize", "typeorm", "prisma", "redis", "ioredis",
    "jsonwebtoken", "bcrypt", "bcryptjs", "passport", "multer", "sharp",
    "ws", "socket.io", "nodemon", "pm2", "helmet", "express-rate-limit",
    # data / files
    "cheerio", "puppeteer", "playwright", "csv-parse", "xml2js", "pdfkit",
    "exceljs", "archiver", "adm-zip", "glob", "fs-extra", "rimraf", "mkdirp",
    "semver", "debug", "ms", "qs", "body-parser", "cookie-parser",
    # mcp / ai-adjacent
    "@modelcontextprotocol/sdk", "@modelcontextprotocol/server-filesystem",
    "@modelcontextprotocol/server-github", "openai", "anthropic",
    "@anthropic-ai/sdk", "langchain", "@langchain/core", "zod", "winston",
    "pino", "async", "bluebird", "rxjs", "eventemitter3",
]


def _lev(a: str, b: str, cap: int = 2) -> int:
    """Levenshtein with early exit past cap."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        row_min = i
        for j, cb in enumerate(b, 1):
            c = prev[j - 1] if ca == cb else 1 + min(prev[j - 1], prev[j], cur[j - 1])
            cur.append(c)
            if c < row_min:
                row_min = c
        if row_min > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _typosquat_hits(name: str) -> list[str]:
    """Names in TOP_PACKAGES within edit distance 1–2 of the queried name."""
    short = name.split("/")[-1].lower()
    hits = []
    for cand in TOP_PACKAGES:
        c = cand.split("/")[-1].lower()
        if c == short:
            continue
        if _lev(short, c) <= 2:
            hits.append(cand)
    return hits[:5]


class NpmCheckRequest(BaseModel):
    package: str = Field(max_length=214)


@router.post("/api/trustscan/npm")
def trustscan_npm(req: NpmCheckRequest, request: Request):
    ip = _client_ip(request)
    if _limited("ts_npm", ip):
        return JSONResponse({"ok": False, "error": "rate limited — try again in a minute"},
                            status_code=429)
    name = (req.package or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "enter a package name"}, status_code=422)
    if name != name.lower():
        return JSONResponse({"ok": False, "error": "npm names are lowercase",
                             "suggestion": name.lower()}, status_code=422)
    if not NPM_NAME_RE.match(name):
        return JSONResponse({"ok": False, "error": "that isn't a valid npm package name"},
                            status_code=422)

    meta, merr = _get_json(f"{NPM_REGISTRY}/{urllib.parse.quote(name, safe='@/')}",
                           timeout=15)
    if merr == "not_found":
        return {"ok": True, "found": False, "package": name,
                "note": "not published on the public npm registry"}
    if merr or not meta:
        return JSONResponse({"ok": False, "error": "npm registry unreachable right now"},
                            status_code=502)
    dl, _ = _get_json(f"{NPM_DOWNLOADS}/{urllib.parse.quote(name, safe='@/')}",
                      timeout=15)

    times = meta.get("time", {}) or {}
    created = times.get("created", "")
    modified = times.get("modified", "")
    age_days = None
    if created:
        try:
            age_days = max(0, int((time.time() - time.mktime(
                time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400))
        except Exception:
            age_days = None
    maintainers = meta.get("maintainers") or []
    repo = (meta.get("repository") or {})
    repo_url = repo.get("url", "") if isinstance(repo, dict) else ""
    dist_tags = meta.get("dist-tags") or {}
    latest = dist_tags.get("latest", "")
    versions = meta.get("versions") or {}
    latest_meta = versions.get(latest, {}) if isinstance(versions, dict) else {}
    deprecated_msg = (latest_meta.get("deprecated") or "") if isinstance(latest_meta, dict) else ""

    checks = []
    warnings = []

    checks.append({"label": "Published on npm", "state": "pass",
                   "detail": f"latest {latest or 'unknown'}"})
    if deprecated_msg:
        checks.append({"label": "Deprecation flag", "state": "warn",
                       "detail": str(deprecated_msg)[:160]})
        warnings.append("the latest version is marked deprecated by its maintainer")
    else:
        checks.append({"label": "Deprecation flag", "state": "pass", "detail": "none"})

    if age_days is None:
        checks.append({"label": "Package age", "state": "info", "detail": "unknown"})
    elif age_days < 30:
        checks.append({"label": "Package age", "state": "warn",
                       "detail": f"first published {age_days}d ago — new and unproven"})
        warnings.append("published very recently — new packages deserve extra scrutiny")
    elif age_days < 365:
        checks.append({"label": "Package age", "state": "info",
                       "detail": f"first published {age_days}d ago"})
    else:
        checks.append({"label": "Package age", "state": "pass",
                       "detail": f"on npm since {created[:10]}"})

    n_maint = len(maintainers)
    if n_maint == 0:
        checks.append({"label": "Maintainers", "state": "info", "detail": "none listed"})
    elif n_maint == 1:
        checks.append({"label": "Maintainers", "state": "info",
                       "detail": "1 — single maintainer (bus-factor risk)"})
    else:
        checks.append({"label": "Maintainers", "state": "pass", "detail": str(n_maint)})

    weekly = (dl or {}).get("downloads")
    if weekly is None:
        checks.append({"label": "Weekly downloads", "state": "info", "detail": "unknown"})
    elif weekly < 100:
        checks.append({"label": "Weekly downloads", "state": "warn",
                       "detail": f"{weekly:,} — barely used"})
        warnings.append("almost nobody downloads this — treat claims about it skeptically")
    else:
        state = "pass" if weekly >= 10000 else "info"
        checks.append({"label": "Weekly downloads", "state": state,
                       "detail": f"{weekly:,}"})

    if repo_url:
        checks.append({"label": "Source repository", "state": "pass",
                       "detail": repo_url[:80]})
    else:
        checks.append({"label": "Source repository", "state": "warn",
                       "detail": "no repository link — source can't be inspected"})
        warnings.append("no linked source repository")

    hits = _typosquat_hits(name)
    if hits:
        checks.append({"label": "Typosquat screen", "state": "warn",
                       "detail": "close to widely-used " + ", ".join(hits)})
        warnings.append("name is one or two keystrokes from a widely-used package — "
                        "a classic typosquat signal")
    else:
        checks.append({"label": "Typosquat screen", "state": "pass",
                       "detail": "no close match among widely-used packages"})

    desc = meta.get("description", "") or ""
    summary = ("established package" if not warnings
               else "worth a closer look — " + "; ".join(warnings[:2]))
    try:
        import metrics
        metrics.record_event("ts_npm_check")
    except Exception:
        pass
    return {"ok": True, "found": True, "package": name,
            "description": desc[:200], "latest": latest,
            "checks": checks, "warnings": warnings, "summary": summary,
            "scope_note": ("package-metadata check only — the deep code scan "
                           "(invisible-Unicode prompt injection, MCP001–MCP006, "
                           "hardcoded secrets) runs over MCP")}
