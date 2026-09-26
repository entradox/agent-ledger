#!/usr/bin/env python3
"""Spend policy: the ONE place that decides whether a spend may happen.

Two kinds of caller, one answer:

- `routes_proxy._pre_call_check` ENFORCES it: a denied call is refused with a
  402 and the provider is never contacted.
- `POST /v1/check` and the `ledger_check_spend` MCP tool REPORT it, so an agent
  can ask before it spends, on any rail, including spend the proxy never sees
  (an x402 purchase, a paid API that is not an LLM).

If those two ever disagreed, an agent told "allowed" by the check would be
refused by the proxy a moment later, and the check would be worthless. Keeping
both on `decide()` is what makes the answer mean something.

`decide()` is read-only. It records nothing and reserves nothing: two agents
sharing one budget can both be told yes for the same remaining headroom. The
proxy still enforces on every call it forwards; the check is advice everywhere
else.
"""
from typing import Optional

import ledger_engine as le

# Reason codes. Stable strings: agents branch on them.
WITHIN_BUDGET = "within_budget"
NO_BUDGET_SET = "no_budget_set"
UNPRICED_MODEL = "unpriced_model"
OVER_MONTHLY_CAP = "over_monthly_cap"
OVER_DAILY_CAP = "over_daily_cap"
OVER_MONTHLY_TOKEN_CAP = "over_monthly_token_cap"
OVER_DAILY_TOKEN_CAP = "over_daily_token_cap"

_DENY_REASON = {
    ("cents", "monthly"): OVER_MONTHLY_CAP,
    ("cents", "daily"): OVER_DAILY_CAP,
    ("tokens", "monthly"): OVER_MONTHLY_TOKEN_CAP,
    ("tokens", "daily"): OVER_DAILY_TOKEN_CAP,
}


def _window(label: str, unit: str, cap: int, spent: int, add: Optional[int]) -> dict:
    return {"window": label, "unit": unit, "cap": cap, "spent": spent,
            "remaining": max(0, cap - spent),
            "after": None if add is None else spent + add}


def decide(agent_id: str, estimate_cents: Optional[int], tokens: int = 0) -> dict:
    """Would a spend of `estimate_cents` (and `tokens` tokens) fit the agent's caps?

    estimate_cents=None means the cost is unknown (an unpriced model). That is
    allowed, the same as the proxy: an unlisted model passes through and raises
    an alert. The reason code says so, so the caller is never told "within
    budget" about a number nobody computed.

    Returns {"allowed", "reason", "windows", "binding"}; `binding` is the first
    window the spend would overflow, or None.
    """
    budget = le.get_budget(agent_id)
    if not budget:
        return {"allowed": True, "reason": NO_BUDGET_SET, "windows": [], "binding": None}

    windows = []
    if budget.monthly_cap_cents > 0:
        windows.append(_window("monthly", "cents", budget.monthly_cap_cents,
                               le._month_spend(agent_id), estimate_cents))
    if getattr(budget, "daily_cap_cents", 0) > 0:
        windows.append(_window("daily", "cents", budget.daily_cap_cents,
                               le._today_spend(agent_id), estimate_cents))
    if tokens > 0 and budget.monthly_token_cap > 0:
        windows.append(_window("monthly", "tokens", budget.monthly_token_cap,
                               le._month_tokens(agent_id), tokens))
    if tokens > 0 and budget.daily_token_cap > 0:
        windows.append(_window("daily", "tokens", budget.daily_token_cap,
                               le._today_tokens(agent_id), tokens))

    # Same comparison the proxy has always made: spent + estimate > cap.
    for w in windows:
        if w["after"] is not None and w["after"] > w["cap"]:
            return {"allowed": False, "reason": _DENY_REASON[(w["unit"], w["window"])],
                    "windows": windows, "binding": w}
    reason = UNPRICED_MODEL if estimate_cents is None else WITHIN_BUDGET
    return {"allowed": True, "reason": reason, "windows": windows, "binding": None}
