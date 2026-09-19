"""Satellite MCP tool-call translation for the AI Agent City gateway.

Background (verified live 2026-09-19): the three v1.30.0 satellite backends
(agent-watch, perimeter-watch, cited) have a server-side fault — every MCP
tools/call returns
  {"isError": true, "text": "Error executing tool <name>: The read operation timed out"}
after ~15s, while their plain documented REST APIs answer fine. The fault is
identical on all three (shared dispatch layer); the satellite API repos are
private, so the fault cannot be fixed from outside.

This module translates an MCP tools/call into the equivalent documented REST
call and returns a proper MCP result envelope, so agents connecting through
https://aiagentscity.com/mcp/<product> get working tools today. Native MCP
(initialize, tools/list, notifications) and trustscan (whose backend works)
pass through the byte-proxy untouched.

Only tools whose REST equivalent is documented in the satellite's own
llms.txt / skill files are translated. Anything else falls through to the
native path (which returns the upstream isError honestly).
"""

import json as _json
import re as _re
import urllib.parse as _up

# ---------------------------------------------------------------------------
# Validators: return the cleaned value, or None when invalid.
# ---------------------------------------------------------------------------

def _v_id(v):
    s = str(v)
    return s if _re.fullmatch(r"[A-Za-z0-9_-]{1,64}", s) else None


def _v_domain(v):
    s = str(v).lower().strip()
    if len(s) > 253:
        return None
    return s if _re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", s) else None


def _v_business(v):
    s = str(v).strip()
    return s if 1 <= len(s) <= 200 else None


def _v_url(v):
    s = str(v).strip()
    if len(s) > 2048:
        return None
    return s if s.startswith("https://") or s.startswith("http://") else None


def _v_email(v):
    s = str(v).strip()
    if len(s) > 320:
        return None
    return s if _re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s) else None


def _v_timeout(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if 5 <= n <= 120 else None


def _v_text(v, limit=500):
    s = str(v)
    return s if 1 <= len(s) <= limit else None


def _v_authority(v):
    return True if v is True else None


def _v_urls(v):
    if not isinstance(v, list) or not (1 <= len(v) <= 500):
        return None
    out = [_v_url(u) for u in v]
    return out if all(out) else None


_VALIDATORS = {
    "id": _v_id,
    "domain": _v_domain,
    "business": _v_business,
    "url": _v_url,
    "email": _v_email,
    "timeout": _v_timeout,
    "text": _v_text,
    "authority": _v_authority,
    "urls": _v_urls,
}

# ---------------------------------------------------------------------------
# Translation table.
#
# prefix -> {"base": rest_base_url,
#            "tools": {tool_name: spec}}
# A REST spec: {"rest": (method, path_template, timeout_secs),
#               "path": {arg: validator},   # interpolated into path
#               "query": {arg: (query_key, validator)},
#               "body": {arg: validator},    # JSON body fields
#               "fixed": {field: value}}      # fixed JSON body fields
# A branch spec: {"branch": (arg, spec_if_present, spec_if_absent)}
# A static spec: {"static": key}
# ---------------------------------------------------------------------------

_TRANSLATIONS = {
    "/mcp/cited": {
        "base": "https://cited-api-production.up.railway.app",
        "tools": {
            "cited_health": {"rest": ("GET", "/health", 25)},
            "cited_stats": {"rest": ("GET", "/stats", 25)},
            "cited_report": {"rest": ("GET", "/report/{scan_id}", 25),
                             "path": {"scan_id": "id"}},
            "cited_watch_status": {"rest": ("GET", "/watch/{business}", 25),
                                   "path": {"business": "business"}},
            "cited_scan": {"rest": ("POST", "/scan", 150),
                           "body": {"business": "business", "city": "business",
                                    "category": "text"}},
            # cited_api_docs / cited_examples / skills_list_tool are NOT
            # translated: they execute natively and fast (0.5-0.9s, verified
            # 2026-09-19) — only the live-read tools hit the broken path.
            "read_skill": {"static": "cited_read_skill"},
        },
    },
    "/mcp/perimeter-watch": {
        "base": "https://perimeter-watch-api-production.up.railway.app",
        "tools": {
            "pw_health": {"rest": ("GET", "/health", 25)},
            "pw_stats": {"rest": ("GET", "/stats", 25)},
            "pw_snapshot": {"rest": ("POST", "/snapshot", 150),
                            "body": {"domain": "domain",
                                     "authority": "authority"}},
            "pw_watch_status": {"rest": ("GET", "/watch/{domain}", 25),
                                "path": {"domain": "domain"}},
        },
    },
    "/mcp/agent-watch": {
        "base": "https://agent-watch-api-production.up.railway.app",
        "tools": {
            "aw_health": {"rest": ("GET", "/health", 25)},
            "aw_census": {"rest": ("GET", "/v1/census", 40)},
            "aw_list_monitored": {"rest": ("GET", "/v1/endpoints", 40)},
            "aw_alerts": {"rest": ("GET", "/v1/alerts", 40)},
            "aw_check_endpoint": {"rest": ("POST", "/v1/probe", 150),
                                  "body": {"url": "url", "timeout": "timeout"},
                                  "fixed": {}},
            "aw_watch": {"branch": ("watch_token",
                                    {"rest": ("GET", "/v1/watch", 25),
                                     "query": {"email": ("email", "email"),
                                               "watch_token": ("token", "text")}},
                                    {"rest": ("POST", "/v1/watch", 40),
                                     "body": {"email": "email",
                                              "urls": "urls"}})},
        },
    },
}

# ---------------------------------------------------------------------------
# Static content: doc/skill tools served from the satellites' own published
# docs (llms.txt / SKILL.md), bundled here because the native MCP doc tools
# hit the same broken dispatch path.
# ---------------------------------------------------------------------------

_CITED_SKILLS = [
    {"name": "cited-watch",
     "description": "Check whether AI engines (Gemini, ChatGPT, Perplexity, AI "
                    "Overviews) recommend a specific local business vs competitors, "
                    "with verbatim evidence. Use when a user asks about their "
                    "business's AI search visibility, or before/after local SEO work.",
     "uri": "https://raw.githubusercontent.com/entradox/cited-site/main/skill/cited-watch/SKILL.md"},
]

_CITED_SKILL_MD = """---
name: cited-watch
description: Check whether AI engines (Gemini, ChatGPT, Perplexity, AI Overviews) recommend a specific local business vs competitors, with verbatim evidence. Use when a user asks about their business's AI search visibility, or before/after local SEO work.
license: MIT
metadata:
  product: Cited
  version: 1.0
  surfaces: mcp, rest, cli
---

# Cited — AI-visibility check for a business

Cited asks public AI engines a public question — "best dentist in {city}" —
and reports whether they recommend the target business, **with the AI's own
words as verbatim evidence**. No auth, no signup for scans.

## How to check (pick one)

**MCP (preferred for agents):**

```
claude mcp add --transport http cited https://aiagentscity.com/mcp/cited/
```

Then call `cited_scan(business="Gentry Dentistry of Suwanee", city="Suwanee, GA")`.
Self-serve docs inside the server: `cited_api_docs(topic)`, `cited_examples(pattern)`.

**REST:**

```bash
curl -X POST https://cited-api-production.up.railway.app/scan \\
  -H "Content-Type: application/json" \\
  -d '{"business": "Gentry Dentistry of Suwanee", "city": "Suwanee, GA"}'
```

**CLI (scriptable):**

```bash
pip install cited-cli
cited scan "Gentry Dentistry of Suwanee" "Suwanee, GA"
```

Exit codes: 0=GREEN (recommended) · 1=AMBER (mentioned, not top) · 2=RED (absent) · 3=error.

## Reading the result

- `summary.overall`: GREEN | AMBER | RED | INCOMPLETE.
  **INCOMPLETE means checks failed (engine outage/rate limit) — it is never a
  visibility verdict.** Never report INCOMPLETE as "AI doesn't recommend you."
- `results[].verdict`: recommended | mentioned_lower | not_mentioned | error.
- `results[].quote`: the AI engine's own words naming (or ranking) businesses —
  verbatim, verified against source text server-side. Quote it to the user.
- `results[].cta_locked` / `cta_teaser`: the specific fix is paid-tier; the
  evidence is never withheld on the free tier.

## Honest-reporting rules (binding on the agent)

1. Report INCOMPLETE/error states as inconclusive, not as a visibility verdict.
2. Cite the verbatim `quote` when telling a user they were mentioned — never
   paraphrase evidence into a stronger claim than the engine made.
3. A free scan is a single run per engine (methodology field says so); present
   it as an instant check, not a statistically-voted verdict.
4. One business + city per scan; scans are rate-limited 3/IP/24h — don't loop.
"""


def _static_cited_read_skill(args):
    uri = str((args or {}).get("uri", "")).strip()
    known = _CITED_SKILLS[0]["uri"]
    if uri == known or uri.endswith("skill/cited-watch/SKILL.md"):
        return _CITED_SKILL_MD
    return None  # unknown skill -> caller returns honest error


_STATIC_HANDLERS = {
    "cited_read_skill": _static_cited_read_skill,
}

# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


def _mcp_result(rpc_id, payload, is_error=False):
    result = {"content": [{"type": "text",
                           "text": payload if isinstance(payload, str)
                           else _json.dumps(payload)}]}
    if is_error:
        result["isError"] = True
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _mcp_error(rpc_id, message):
    return _mcp_result(rpc_id, {"error": message}, is_error=True)


def _clean_args(spec_args, args):
    """Validate and clean args per {arg: validator_name}. Returns (cleaned, error)."""
    cleaned = {}
    for arg, vname in (spec_args or {}).items():
        if arg not in (args or {}):
            return None, "missing required argument: %s" % arg
        val = _VALIDATORS[vname]((args or {})[arg])
        if val is None:
            return None, "invalid argument: %s" % arg
        cleaned[arg] = val
    return cleaned, None


async def _exec_rest(base, spec, args, client, client_ip):
    method, path_t, timeout = spec["rest"]
    cleaned, err = _clean_args(spec.get("path"), args)
    if err:
        return None, err
    path = path_t
    for k, v in (cleaned or {}).items():
        path = path.replace("{%s}" % k, _up.quote(str(v), safe=""))
    q = {}
    for arg, (qkey, vname) in (spec.get("query") or {}).items():
        if arg not in (args or {}):
            return None, "missing required argument: %s" % arg
        val = _VALIDATORS[vname](args[arg])
        if val is None:
            return None, "invalid argument: %s" % arg
        q[qkey] = val
    body = dict(spec.get("fixed") or {})
    for arg, vname in (spec.get("body") or {}).items():
        if arg not in (args or {}):
            if vname == "timeout":
                continue  # timeout is optional; server default applies
            return None, "missing required argument: %s" % arg
        val = _VALIDATORS[vname](args[arg])
        if val is None:
            return None, "invalid argument: %s" % arg
        body[arg] = val
    url = base + path
    headers = {}
    if client_ip:
        headers["x-forwarded-for"] = client_ip
    try:
        if method == "GET":
            resp = await client.get(url, params=q or None, headers=headers,
                                    timeout=timeout)
        else:
            resp = await client.post(url, json=body, headers=headers,
                                     timeout=timeout)
    except Exception as exc:
        return None, "upstream unreachable: %s" % type(exc).__name__
    ctype = resp.headers.get("content-type", "")
    if resp.status_code >= 400:
        detail = resp.text[:500]
        try:
            detail = _json.dumps(resp.json())[:500]
        except Exception:
            pass
        return None, "upstream HTTP %d: %s" % (resp.status_code, detail)
    if "json" in ctype:
        try:
            return resp.json(), None
        except Exception:
            return {"raw": resp.text[:2000]}, None
    return {"raw": resp.text[:2000]}, None


async def _exec_spec(base, spec, args, client, client_ip):
    if "branch" in spec:
        arg, if_present, if_absent = spec["branch"]
        use = if_present if (args or {}).get(arg) else if_absent
        return await _exec_spec(base, use, args, client, client_ip)
    if "static" in spec:
        handler = _STATIC_HANDLERS[spec["static"]]
        out = handler(args or {})
        if out is None:
            return None, "unknown skill uri (only the bundled cited-watch skill is served)"
        return out, None
    return await _exec_rest(base, spec, args, client, client_ip)


async def translate(prefix, rpc, client, client_ip):
    """Translate one JSON-RPC tools/call.

    Returns the MCP response dict when the tool is translatable, else None
    (caller must pass the request through to the native upstream).
    """
    entry = _TRANSLATIONS.get(prefix)
    if not entry or not isinstance(rpc, dict):
        return None
    if rpc.get("method") != "tools/call":
        return None
    params = rpc.get("params") or {}
    if not isinstance(params, dict):
        return None
    tool = params.get("name")
    spec = entry["tools"].get(tool)
    if not spec:
        return None
    rpc_id = rpc.get("id")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return _mcp_error(rpc_id, "arguments must be an object")
    payload, err = await _exec_spec(entry["base"], spec, args, client,
                                    client_ip)
    if err:
        return _mcp_error(rpc_id, err)
    return _mcp_result(rpc_id, payload)


def translatable(prefix):
    """City path prefixes that have a translation table (trustscan excluded)."""
    return prefix in _TRANSLATIONS
