#!/usr/bin/env python3
"""AgentLedger — canonical docs content (launch-kit v0.3).

Single source of truth for the agent-facing documentation surfaces:
  - GET /llms.txt (api_server.py renders selected sections)
  - MCP tool `ledger_api_docs` (al_mcp_http.py, mcp_server.py)
  - MCP tool `ledger_examples` (al_mcp_http.py, mcp_server.py)
  - recipes.md (static render of the RECIPES dict)

Keep this the only place these strings live — README.md/status.html carry
their own hand-written Connect sections, but the docs/recipe *content* comes
from here so REST, MCP, and llms.txt never drift out of sync.
"""
from ledger_engine import AL_API_VERSION

BASE_URL = "https://agent-ledger-production-0ff8.up.railway.app"

QUICKSTART_MD = f"""## Quickstart (curl)

Every REST request under `/v1/*` and every MCP HTTP request under `/mcp/`
must send `AL-API-Version: {AL_API_VERSION}`. Missing or wrong values are
rejected with 400 `version_header`.

```bash
curl -X POST {BASE_URL}/v1/track \\
  -H "Content-Type: application/json" \\
  -H "AL-API-Version: {AL_API_VERSION}" \\
  -H "Idempotency-Key: my-agent-track-2026-09-09-001" \\
  -d '{{"agent_id":"my-agent","rail":"x402","amount_cents":100,"service":"search_query"}}'
```

Claiming a NEW `agent_id` requires a `workspace_key` in that first write's
body (add `"workspace_key":"wk_live_..."` to the example above). Get one
with no human at all via `POST /v1/billing/x402` — the paying wallet
becomes the workspace identity. A human owner can instead sign in at
`{BASE_URL}/login`, which requires Google OAuth to be configured for this
deployment; if it isn't, `/login` returns 503 `login_not_configured`.
Without a workspace_key, a new claim is rejected with 401
`workspace_key_required`.

That first write mints an `agent_secret` in the response — save it, every
later write to that `agent_id` must include it as `"agent_secret"` and needs
no `workspace_key` again. Reads: `GET /v1/report`, `GET /v1/tokens`, and
`GET /v1/alerts` all require an `X-Agent-Secret` or `X-Workspace-Key` header
(either credential proving access to that `agent_id`). The MCP read tools
take the same two credentials as parameters — there is no unauthenticated
read path on either surface.

Retries: send the same `Idempotency-Key` on a retried write and you get back
the exact cached response from the first attempt instead of a second write.
Keys are scoped per `agent_id` + operation, expire after 24h, and are
capped at 255 chars (longer → 400 `idempotency_key_too_long`)."""

MCP_TOOLS_MD = f"""## MCP Tools

| Tool | Read/Write | Description |
|---|---|---|
| `ledger_track` | write | record a spend entry (`workspace_key` to claim a new `agent_id`, `agent_secret` thereafter) |
| `ledger_set_budget` | write | set monthly/daily budget caps (same credentials as `ledger_track`) |
| `ledger_report` | read | spend report: totals, by-rail, by-service, anomalies (`agent_secret` or `workspace_key`) |
| `ledger_alerts` | read | budget warning/exceeded + spending-spike alerts (`agent_secret` or `workspace_key`) |
| `ledger_list_agents` | read, owner-only | full cross-tenant listing, needs `admin_secret` |
| `ledger_api_docs` | read | this documentation, filtered by topic |
| `ledger_examples` | read | complete runnable recipe snippets |

Remote endpoint: `{BASE_URL}/mcp/` — every request also needs
`AL-API-Version: {AL_API_VERSION}`."""

REST_ENDPOINTS_MD = f"""## REST Endpoints

```
GET  /health                       — liveness
POST /v1/billing/x402              — self-serve workspace_key for an agent with a wallet
                                      (X-PAYMENT header; paying wallet = workspace identity)
GET  /login                        — Google sign-in; issues a workspace_key on first login
                                      (503 login_not_configured if Google OAuth isn't set
                                      up for this deployment)
POST /v1/track                     — record a spend entry (workspace_key claims, agent_secret writes)
POST /v1/budget                    — set budget caps (workspace_key claims, agent_secret writes)
GET  /v1/report/{{agent_id}}         — spend report (query: days=30) — requires X-Agent-Secret or X-Workspace-Key
GET  /v1/tokens/{{agent_id}}         — token burn report — requires X-Agent-Secret or X-Workspace-Key
GET  /v1/alerts/{{agent_id}}         — alerts for agent — requires X-Agent-Secret or X-Workspace-Key
GET  /v1/agents                    — owner-only: full cross-tenant listing (X-Al-Admin)
GET  /v1/metrics                   — owner-only: funnel + revenue + reach telemetry
GET  /stats                        — usage counters
```

`POST /v1/track` and `POST /v1/budget` accept an optional `Idempotency-Key`
header, and both require `AL-API-Version: {AL_API_VERSION}` — see the
quickstart and idempotency topics."""

ERROR_CODES_MD = """## Error Codes

Every error response — REST and MCP alike — uses the same typed envelope:

```json
{"error": {"type": "<error_type>", "message": "<human message>", "code": "<optional>", "param": "<optional>"}}
```

| HTTP status | type | seen on |
|---|---|---|
| 400 | invalid_request_error | missing/invalid `AL-API-Version`, oversized `Idempotency-Key` |
| 401 | authentication_error | missing/wrong `agent_secret`; missing/invalid `workspace_key` on a NEW claim; no credential on a read |
| 402 | budget_error | budget cap exceeded, workspace agent-slot cap exceeded |
| 403 | permission_error | scope/ownership denied |
| 404 | not_found_error | unknown agent_id or resource |
| 409 | conflict_error | `Idempotency-Key` already in flight |
| 422 | validation_error | malformed `agent_id`, bad rail, out-of-range amount |

Known `code` values: `invalid_agent_id`, `rail_not_allowed`,
`agent_secret_mismatch`, `workspace_key_required`, `beta_cap_exceeded`,
`budget_exceeded`, `invalid_amount`, `version_header`,
`idempotency_key_too_long`, `idempotency_conflict`, `x402_no_tx_hash`.

MCP tools cannot raise HTTP status codes, so they return the same
information in the payload as `{"error": "<message>", "error_code": "<code>"}`
using these same `code` values."""

IDEMPOTENCY_MD = """## Idempotency

Send `Idempotency-Key: <opaque string, <=255 chars>` on `POST /v1/track` or
`POST /v1/budget`. A repeat of the exact same key, for the same `agent_id`
and operation, within 24h returns the cached response from the first
attempt — the write is never executed twice. A second request using the
same key while the first is still being processed gets 409
`idempotency_conflict`. The header is optional — omit it for fire-and-forget
writes where at-most-once isn't required."""

BUDGET_MD = """## Budget Caps

`POST /v1/budget` takes two independent cap dimensions — set either, both, or
neither:

- `monthly_cents` / `daily_cents` — dollar caps. Enforced against every
  non-`tokens` rail (`mpp`, `x402`, `api_key`, `manual`): a `POST /v1/track`
  write that would cross the cap is blocked with 402 `budget_exceeded`
  **before** it's recorded.
- `monthly_tokens` / `daily_tokens` — token-volume caps. Enforced only
  against `rail="tokens"` bookkeeping rows (the `tokens_in`/`tokens_out`
  counters attached to a `track` call, or a call to `/v1/track` with
  `rail: "tokens"` directly). A token-metered agent with no dollar amount
  per call — the common shape for a subscription/flat-rate LLM account —
  gets no protection from the dollar caps at all; set a token cap if that's
  how the agent is billed.

These are separate budgets, not one converted into the other: an agent that
mixes dollar-rail spend and token-burn bookkeeping needs both caps set to be
covered on both dimensions. `GET /v1/report/{agent_id}` returns both under
`budget_status` (`monthly_token_cap`, `monthly_tokens_used`,
`token_pct_used`, `token_exceeded` — present only once a token cap is set)."""

_TOPIC_ORDER = ("quickstart", "mcp", "rest", "budget", "errors", "idempotency")

DOCS_TOPICS = {
    "quickstart": QUICKSTART_MD,
    "mcp": MCP_TOOLS_MD,
    "rest": REST_ENDPOINTS_MD,
    "budget": BUDGET_MD,
    "errors": ERROR_CODES_MD,
    "idempotency": IDEMPOTENCY_MD,
}


def get_api_docs(topic: str = "") -> str:
    """Return markdown docs for one topic, or all topics concatenated.

    topic: "quickstart" | "mcp" | "rest" | "errors" | "idempotency" | "all" | ""
    An unknown topic falls back to the full docs (concatenation of every
    topic) rather than erroring — this is a self-serve doc tool, not a
    strict API, so a typo'd topic should still hand the caller something
    useful.
    """
    topic = (topic or "all").strip().lower()
    if topic not in DOCS_TOPICS:
        return "\n\n".join(DOCS_TOPICS[k] for k in _TOPIC_ORDER)
    return DOCS_TOPICS[topic]


RECIPES = {
    "python_tracking": f'''"""AgentLedger — track spend from an x402/API-key/token-burn loop."""
import requests

BASE = "{BASE_URL}"
HEADERS = {{"Content-Type": "application/json", "AL-API-Version": "{AL_API_VERSION}"}}

# Claiming a new agent_id needs your workspace_key: POST /v1/billing/x402
# if your agent has a wallet (no human, no login), or sign in at
# {BASE_URL}/login (requires Google OAuth configured on the deployment;
# returns 503 login_not_configured otherwise). After the first call the
# minted agent_secret is what authenticates every later write.
WORKSPACE_KEY = "wk_live_..."
agent_secret = None  # fill in after the first successful call


def track_spend(agent_id, rail, amount_cents, service, **extra):
    body = {{"agent_id": agent_id, "rail": rail, "amount_cents": amount_cents,
            "service": service, **extra}}
    if agent_secret:
        body["agent_secret"] = agent_secret
    else:
        body["workspace_key"] = WORKSPACE_KEY
    r = requests.post(f"{{BASE}}/v1/track", json=body, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    result = track_spend("research-agent-v2", "x402", 250, "search_query",
                          tokens_in=1200, tokens_out=340, model="gpt-4o")
    agent_secret = result.get("agent_secret", agent_secret)
    print(result)
''',
    "budget_enforcement": f'''"""AgentLedger — set a monthly cap and handle the 402 block when crossed."""
import requests

BASE = "{BASE_URL}"
HEADERS = {{"Content-Type": "application/json", "AL-API-Version": "{AL_API_VERSION}"}}


# A new agent_id is claimed with your workspace_key; afterwards the minted
# agent_secret authenticates writes. Get one via POST /v1/billing/x402 (no
# human, no login) or by signing in at {BASE_URL}/login (requires Google
# OAuth configured on the deployment; 503 login_not_configured otherwise).
WORKSPACE_KEY = "wk_live_..."


def set_budget(agent_id, monthly_cents, agent_secret=None, daily_cents=0):
    body = {{"agent_id": agent_id, "monthly_cents": monthly_cents, "daily_cents": daily_cents}}
    if agent_secret:
        body["agent_secret"] = agent_secret
    else:
        body["workspace_key"] = WORKSPACE_KEY
    r = requests.post(f"{{BASE}}/v1/budget", json=body, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def spend_or_block(agent_id, agent_secret, amount_cents, service):
    body = {{"agent_id": agent_id, "rail": "manual", "amount_cents": amount_cents,
            "service": service, "agent_secret": agent_secret}}
    r = requests.post(f"{{BASE}}/v1/track", json=body, headers=HEADERS, timeout=10)
    if r.status_code == 402:
        err = r.json()["error"]
        print(f"blocked: {{err['message']}} (code={{err.get('code')}})")
        return None
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    budget = set_budget("cost-guarded-agent", monthly_cents=5000)
    secret = budget["agent_secret"]
    spend_or_block("cost-guarded-agent", secret, 4900, "api_call")
    spend_or_block("cost-guarded-agent", secret, 500, "api_call")  # likely 402
''',
    "weekly_report": f'''"""AgentLedger — weekly spend P&L across every agent you track.

GET /v1/report now requires a credential: either the agent's own
agent_secret, or (as used here) the workspace_key covering all of your
agent_ids at once."""
import requests

BASE = "{BASE_URL}"


def weekly_pnl(agent_ids, workspace_key):
    rows = []
    headers = {{"X-Workspace-Key": workspace_key}}
    for agent_id in agent_ids:
        r = requests.get(f"{{BASE}}/v1/report/{{agent_id}}", params={{"days": 7}},
                          headers=headers, timeout=10)
        r.raise_for_status()
        rep = r.json()
        rows.append({{"agent_id": agent_id,
                     "spend_usd": rep["total_spend_cents"] / 100,
                     "budget_status": rep["budget_status"],
                     "anomalies": rep["anomalies"]}})
    return rows


if __name__ == "__main__":
    workspace_key = "YOUR_SAVED_WORKSPACE_KEY"
    for row in weekly_pnl(["research-agent-v2", "cost-guarded-agent"], workspace_key):
        print(f"{{row['agent_id']:24s}} ${{row['spend_usd']:.2f}}  anomalies={{len(row['anomalies'])}}")
''',
    "retry_safe_writes": f'''"""AgentLedger — retry-safe writes with Idempotency-Key."""
import uuid
import requests

BASE = "{BASE_URL}"
HEADERS = {{"Content-Type": "application/json", "AL-API-Version": "{AL_API_VERSION}"}}


def track_once(agent_id, rail, amount_cents, service, agent_secret=None, idem_key=None):
    idem_key = idem_key or str(uuid.uuid4())
    headers = {{**HEADERS, "Idempotency-Key": idem_key}}
    body = {{"agent_id": agent_id, "rail": rail, "amount_cents": amount_cents, "service": service}}
    if agent_secret:
        body["agent_secret"] = agent_secret
    else:
        # claiming a new agent_id — needs a workspace_key from
        # POST /v1/billing/x402 (no login) or from /login (Google OAuth
        # must be configured on the deployment)
        body["workspace_key"] = "wk_live_..."
    for attempt in range(3):
        try:
            r = requests.post(f"{{BASE}}/v1/track", json=body, headers=headers, timeout=5)
            r.raise_for_status()
            return r.json()  # safe to retry: same idem_key never double-charges
        except requests.exceptions.RequestException:
            if attempt == 2:
                raise
    return None


if __name__ == "__main__":
    print(track_once("flaky-network-agent", "manual", 199, "retry_demo"))
''',
}

_RECIPE_ORDER = ("python_tracking", "budget_enforcement", "weekly_report", "retry_safe_writes")


def get_example(pattern: str) -> str:
    """Return a complete runnable Python recipe for the given pattern name."""
    pattern = (pattern or "").strip().lower()
    if pattern not in RECIPES:
        return (f"Unknown pattern '{pattern}'. Valid patterns: "
                f"{', '.join(_RECIPE_ORDER)}")
    return RECIPES[pattern]
