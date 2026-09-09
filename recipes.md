# AgentLedger Recipes

Complete, runnable Python snippets against the live API at `https://agent-ledger-production-0ff8.up.railway.app`.
Each recipe is also available live via the MCP tool `ledger_examples(pattern=...)` or the `ledger_api_docs` self-serve docs tool — this file is the static mirror, generated from the same `docs_content.py` source so it never drifts.

## Track spend from a Python loop

Pattern name: `python_tracking`

```python
"""AgentLedger — track spend from an x402/API-key/token-burn loop."""
import requests

BASE = "https://agent-ledger-production-0ff8.up.railway.app"
HEADERS = {"Content-Type": "application/json", "AL-API-Version": "2026-09-01"}

agent_secret = None  # fill in after the first successful call


def track_spend(agent_id, rail, amount_cents, service, **extra):
    body = {"agent_id": agent_id, "rail": rail, "amount_cents": amount_cents,
            "service": service, **extra}
    if agent_secret:
        body["agent_secret"] = agent_secret
    r = requests.post(f"{BASE}/v1/track", json=body, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    result = track_spend("research-agent-v2", "x402", 250, "search_query",
                          tokens_in=1200, tokens_out=340, model="gpt-4o")
    agent_secret = result.get("agent_secret", agent_secret)
    print(result)
```

## Set a budget cap and handle the 402 block

Pattern name: `budget_enforcement`

```python
"""AgentLedger — set a monthly cap and handle the 402 block when crossed."""
import requests

BASE = "https://agent-ledger-production-0ff8.up.railway.app"
HEADERS = {"Content-Type": "application/json", "AL-API-Version": "2026-09-01"}


def set_budget(agent_id, monthly_cents, agent_secret=None, daily_cents=0):
    body = {"agent_id": agent_id, "monthly_cents": monthly_cents, "daily_cents": daily_cents}
    if agent_secret:
        body["agent_secret"] = agent_secret
    r = requests.post(f"{BASE}/v1/budget", json=body, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def spend_or_block(agent_id, agent_secret, amount_cents, service):
    body = {"agent_id": agent_id, "rail": "manual", "amount_cents": amount_cents,
            "service": service, "agent_secret": agent_secret}
    r = requests.post(f"{BASE}/v1/track", json=body, headers=HEADERS, timeout=10)
    if r.status_code == 402:
        err = r.json()["error"]
        print(f"blocked: {err['message']} (code={err.get('code')})")
        return None
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    budget = set_budget("cost-guarded-agent", monthly_cents=5000)
    secret = budget["agent_secret"]
    spend_or_block("cost-guarded-agent", secret, 4900, "api_call")
    spend_or_block("cost-guarded-agent", secret, 500, "api_call")  # likely 402
```

## Weekly spend P&L across every agent

Pattern name: `weekly_report`

```python
"""AgentLedger — weekly spend P&L across every agent you track."""
import requests

BASE = "https://agent-ledger-production-0ff8.up.railway.app"


def weekly_pnl(agent_ids):
    rows = []
    for agent_id in agent_ids:
        r = requests.get(f"{BASE}/v1/report/{agent_id}", params={"days": 7}, timeout=10)
        r.raise_for_status()
        rep = r.json()
        rows.append({"agent_id": agent_id,
                     "spend_usd": rep["total_spend_cents"] / 100,
                     "budget_status": rep["budget_status"],
                     "anomalies": rep["anomalies"]})
    return rows


if __name__ == "__main__":
    for row in weekly_pnl(["research-agent-v2", "cost-guarded-agent"]):
        print(f"{row['agent_id']:24s} ${row['spend_usd']:.2f}  anomalies={len(row['anomalies'])}")
```

## Retry-safe writes with Idempotency-Key

Pattern name: `retry_safe_writes`

```python
"""AgentLedger — retry-safe writes with Idempotency-Key."""
import uuid
import requests

BASE = "https://agent-ledger-production-0ff8.up.railway.app"
HEADERS = {"Content-Type": "application/json", "AL-API-Version": "2026-09-01"}


def track_once(agent_id, rail, amount_cents, service, agent_secret=None, idem_key=None):
    idem_key = idem_key or str(uuid.uuid4())
    headers = {**HEADERS, "Idempotency-Key": idem_key}
    body = {"agent_id": agent_id, "rail": rail, "amount_cents": amount_cents, "service": service}
    if agent_secret:
        body["agent_secret"] = agent_secret
    for attempt in range(3):
        try:
            r = requests.post(f"{BASE}/v1/track", json=body, headers=headers, timeout=5)
            r.raise_for_status()
            return r.json()  # safe to retry: same idem_key never double-charges
        except requests.exceptions.RequestException:
            if attempt == 2:
                raise
    return None


if __name__ == "__main__":
    print(track_once("flaky-network-agent", "manual", 199, "retry_demo"))
```

