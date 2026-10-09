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

# ledger_engine and proxy are imported inside each function, never bound at
# module level: several test modules drop them from sys.modules and re-import
# them against a fresh data dir, and a module-level binding here would keep
# deciding against the STALE copy (found by the full suite: the proxy stopped
# returning 402 once an earlier test had re-imported ledger_engine).

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
    import ledger_engine as le
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


# ── the question an agent asks before it spends ────────────────────────────

class CheckInputError(ValueError):
    """A check request that names no spend, or names it two ways at once."""
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


_NOTE = ("Advisory: nothing was recorded or reserved. Record the spend with "
         "POST /v1/track (or ledger_track) once it happens. Calls routed through "
         "/proxy/{provider} are enforced with this same decision on every request.")


def _price_block(model: str) -> Optional[dict]:
    import proxy as proxy_core
    key, how = proxy_core.resolve_model(model)
    if not key:
        return None
    entry = proxy_core.prices()[key]
    return {"model": key, "resolved": how or "exact",
            "in_per_million_usd": entry.get("in"), "out_per_million_usd": entry.get("out"),
            "verified": bool(entry.get("verified")), "as_of": entry.get("as_of"),
            "source": entry.get("source")}


def _estimate(amount_cents, model, tokens_in, tokens_out, payload):
    """(estimate_cents or None, tokens, basis, price). Exactly one spend shape."""
    import ledger_engine as le
    import proxy as proxy_core
    if amount_cents is not None and model:
        raise CheckInputError("give amount_cents OR model (+ tokens or payload), not both",
                              "ambiguous_spend")
    if amount_cents is not None:
        if amount_cents < 0 or amount_cents > le.MAX_AMOUNT_CENTS:
            raise CheckInputError(
                f"amount_cents must be between 0 and {le.MAX_AMOUNT_CENTS}",
                "invalid_amount")
        return amount_cents, 0, "amount", None
    if not model:
        raise CheckInputError(
            "nothing to check: give amount_cents, or model with tokens_in/tokens_out, "
            "or model with payload (the request body you are about to send)",
            "nothing_to_check")
    price = _price_block(model)
    if payload is not None:
        if not isinstance(payload, dict):
            raise CheckInputError("payload must be the JSON request body object",
                                  "invalid_payload")
        tokens = (proxy_core.estimate_prompt_tokens(payload)
                  + proxy_core.requested_max_tokens(payload))
        # The proxy's own estimator, so this is the number the proxy will use.
        return proxy_core.call_estimate_cents(model, payload), tokens, "payload", price
    if tokens_in < 0 or tokens_out < 0:
        raise CheckInputError("tokens_in and tokens_out must be >= 0", "invalid_tokens")
    if tokens_in + tokens_out == 0:
        raise CheckInputError(
            "model given without tokens_in/tokens_out or payload: nothing to price",
            "nothing_to_check")
    exact = proxy_core.cost_cents_exact(model, tokens_in, tokens_out)
    estimate = None if exact is None else int(-(-exact // 1))  # ceil, like the proxy
    return estimate, tokens_in + tokens_out, "model_tokens", price


def _message(agent_id: str, verdict: dict, estimate) -> str:
    reason = verdict["reason"]
    if reason == NO_BUDGET_SET:
        return (f"allowed: {agent_id} has no budget, so nothing limits it. "
                f"Set one with POST /v1/budget (or ledger_set_budget).")
    if reason == UNPRICED_MODEL:
        return ("allowed, but the cost is unknown: this model is not in the price "
                "table, so no cap can be checked against it. GET /v1/pricing lists "
                "priced models.")
    if verdict["allowed"]:
        left = [w["cap"] - w["after"] for w in verdict["windows"] if w["unit"] == "cents"]
        tail = f"; {min(left)} cents of headroom left after it" if left else ""
        return f"allowed: {estimate} cents fits every cap{tail}."
    w = verdict["binding"]
    unit = "cents" if w["unit"] == "cents" else "tokens"
    return (f"denied: this would put {agent_id} at {w['after']} {unit} against its "
            f"{w['window']} cap of {w['cap']} ({w['spent']} already used). "
            f"Do not spend; ask the owner to raise the cap, or wait for the window to reset.")


def check_spend(agent_id: str, amount_cents: Optional[int] = None, model: str = "",
                tokens_in: int = 0, tokens_out: int = 0,
                payload: Optional[dict] = None) -> dict:
    """The body of POST /v1/check and ledger_check_spend. Read-only.
    Caller authorizes first; raises CheckInputError on a malformed question."""
    import ledger_engine as le
    le.validate_agent_id(agent_id)
    estimate, tokens, basis, price = _estimate(amount_cents, (model or "").strip(),
                                               tokens_in, tokens_out, payload)
    # A payload is a call headed for the proxy, and the proxy does not check
    # token caps (its token rows are force-recorded after the call). Checking
    # them here would deny what the proxy then forwards. Token caps DO bind a
    # model+tokens spend, which the agent will record through track().
    verdict = decide(agent_id, estimate, 0 if basis == "payload" else tokens)
    # Surface the approval lane in every check — a denied answer should carry
    # the escape hatch (request an approval) and an approved permit waiting to
    # be spent should be visible even when the cap would deny.
    approvals = {"pending": 0, "approved_covering": None}
    try:
        permits = le.list_approvals(agent_id)
        approvals["pending"] = sum(1 for p in permits if p["state"] == "pending")
        if estimate is not None:
            covering = [p for p in permits
                        if p["state"] == "approved"
                        and int(p.get("amount_cents", 0)) >= estimate]
            if covering:
                approvals["approved_covering"] = covering[-1]["approval_id"]
                verdict["allowed"] = True
                verdict["reason"] = "approved_permit"
    except Exception:
        pass
    if verdict["reason"] == "approved_permit":
        message = (f"allowed: approved permit {approvals['approved_covering']} "
                   f"covers this spend even though the cap would deny it — "
                   f"it will be consumed by the next ledger_track.")
    else:
        message = _message(agent_id, verdict, estimate)
        if not verdict["allowed"]:
            message += (" You can also request a one-shot exception with "
                        "ledger_request_approval.")
    return {"agent_id": agent_id,
            "allowed": verdict["allowed"],
            "decision": "allow" if verdict["allowed"] else "deny",
            "reason": verdict["reason"],
            "message": message,
            "approvals": approvals,
            "estimate_cents": estimate,
            "estimate_tokens": tokens or None,
            "basis": basis,
            "price": price,
            "windows": verdict["windows"],
            "recorded": False,
            "note": _NOTE}
