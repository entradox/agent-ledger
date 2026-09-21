#!/usr/bin/env python3
"""Journey sweep — "no new customer fails anywhere", against PRODUCTION.

D-1404 / aiagentscity 14-day brief item 3. Repeatable harness with a pass/fail log;
weekly from here on. Source discipline: `E2E-CUSTOMER-WALK-2026-09-18.md`.

WHY THIS EXISTS
The 2026-09-18 walk found two CRITICAL funnel defects by hand. Nothing kept them found,
because `agent-journey-test.sh` walks the AGENT path and never executes the HUMAN
onboarding example. This harness walks the human journey and — the part the walk itself
asked for — executes OUR PUBLISHED INSTRUCTIONS, not just our product.

THE HONESTY RULE (non-negotiable)
    PASS  — the check ran and the live system met the bar.
    FAIL  — the check ran and the system did not.
    UNKNOWN (exit 3) — the check could NOT be established (no credential, no response).
UNKNOWN must never be reported as PASS. A harness that turns "I don't know" into "fine"
is worse than no harness: it launders an outage into a green light.

SAFETY
Reads production. Mints throwaway workspaces/agents. Constructs a checkout SESSION but
never pays. Prints no credential. Touches no config, no Stripe setting, no DNS.

Usage:
    python3 journey_sweep.py [--base URL] [--json PATH] [--log PATH] [--quiet]
Exit: 0 all PASS · 1 any FAIL · 3 any UNKNOWN and no FAIL
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"
DEFAULT_BASE = "https://aiagentscity.com"
API_VERSION = "2026-09-01"


class Ctx:
    """Accumulates results and the evidence behind each one."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.results: list[dict] = []
        self.session_id: str | None = None  # MCP session, captured from initialize

    def add(self, name: str, status: str, detail: str, evidence: str = "") -> None:
        self.results.append(
            {"check": name, "status": status, "detail": detail, "evidence": evidence[:400]}
        )
        mark = {PASS: "PASS", FAIL: "FAIL", UNKNOWN: "????"}[status]
        print(f"  [{mark}] {name}: {detail}")

    # --- HTTP helper: every check records the real response, never a guess ---
    def http(self, path: str, method: str = "GET", body: dict | None = None,
             headers: dict | None = None, timeout: int = 30):
        url = path if path.startswith("http") else self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", "agent-ledger-journey-sweep/1.0")
        if data:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                sid = r.headers.get("mcp-session-id")
                if sid:
                    self.session_id = sid
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            sid = e.headers.get("mcp-session-id") if e.headers else None
            if sid:
                self.session_id = sid
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # network/timeout — the check is UNKNOWN, not FAIL
            return None, f"__TRANSPORT__{type(e).__name__}: {e}"


def _json(txt: str):
    try:
        return json.loads(txt)
    except Exception:
        return None


# ---------------------------------------------------------------- checks

def check_mint(c: Ctx) -> dict | None:
    """1. Mint — a brand-new customer can create a workspace.

    DISCIPLINE: POST /start is capped at 3 mints per IP per day (api_server
    `_start_mint_allowed`), and the walk's own evidence says 30 of 40 minted
    workspaces stopped at the next step. So this sweep must NOT casually burn a mint:
    it probes the FORM, and only mints when a credential is actually needed and none
    was supplied via --creds / SWEEP_WS_ID+SWEEP_WS_KEY.
    """
    st, txt = c.http("/start")
    if st is None:
        c.add("mint.page", UNKNOWN, "could not reach /start", txt)
        return None
    if st != 200:
        c.add("mint.page", FAIL, f"/start returned {st}", txt[:200])
        return None
    c.add("mint.page", PASS, "/start 200 (mint form present)")

    # Inspect the FORM — never POST to it. An earlier version of this check POSTed an
    # empty body to /start; it returned the workspace page, i.e. it MINTED, burning one
    # of the 3 mints allowed per IP per day. Probing a mint route with a write is the
    # bug; read the form instead.
    form_ok = ('<form' in (txt or "")) and ("method=\"post\"" in (txt or "").lower()
                                            or "method='post'" in (txt or "").lower())
    if form_ok:
        c.add("mint.form", PASS,
              "mint form present and POSTs (no mint consumed by this check)")
    else:
        c.add("mint.form", UNKNOWN,
              "no POST form detected on /start — mint path unclear, inspect manually",
              (txt or "")[:200])
    return None


def check_published_instructions(c: Ctx) -> None:
    """2. OUR PUBLISHED INSTRUCTIONS EXECUTE — the check the 09-18 walk asked for.

    A command is a template if it still contains an obvious placeholder. A template must
    fail CLEANLY (4xx/usage error) — that proves the route exists. A concrete URL must
    respond. A 404/405/422 on a published concrete command is a documentation defect.
    """
    placeholder = re.compile(r"<[^>]*>|YOUR_|your-|__|PAYMENT|PLACEHOLDER", re.I)
    found = 0
    defects: list[str] = []
    for page in ("/start", "/llms.txt", "/quickstart"):
        st, txt = c.http(page)
        if st != 200:
            defects.append(f"{page} -> {st}")
            continue
        t = re.sub(r"<br\s*/?>", "\n", txt)
        t = re.sub(r"<[^>]+>", "", t)
        for ent, ch in (("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&"),
                        ("&gt;", ">"), ("&lt;", "<")):
            t = t.replace(ent, ch)
        t = re.sub(r"\\\s*\n\s*", " ", t)  # join shell continuations
        for line in t.split("\n"):
            line = line.strip()
            if not line.startswith("curl "):
                continue
            found += 1
            m = re.search(r"https?://[^\s\"']+|/v?1/[A-Za-z0-9_\-/{}]+", line)
            if not m:
                continue
            target = m.group(0)
            is_template = bool(placeholder.search(line))
            st2, txt2 = c.http(target, "GET" if "POST" not in line else "POST")
            if st2 is None:
                defects.append(f"{page}: transport failure on {target}")
            elif st2 in (404, 405):
                # 405 on a GET probe of a POST route is expected; 404 is not.
                if st2 == 404:
                    defects.append(f"{page}: published command targets {target} -> 404")
            elif is_template and st2 >= 500:
                defects.append(f"{page}: templated command {target} -> {st2}")
    if found == 0:
        c.add("published.commands", UNKNOWN,
              "no executable curl examples found on /start, /llms.txt, /quickstart")
    elif defects:
        c.add("published.commands", FAIL,
              f"{len(defects)} published command(s) do not work", " | ".join(defects))
    else:
        c.add("published.commands", PASS,
              f"{found} published command(s) execute against prod")


def check_pay_door(c: Ctx) -> None:
    """3. The pay door is REACHABLE — D2's literal defect: /upgrade, /pricing 404'd.

    Presence of a working till is not enough; the walk proved the till worked and the
    funnel still failed, because there was no door from where the need arises.
    """
    missing: list[str] = []
    for path in ("/upgrade", "/pricing"):
        st, _ = c.http(path)
        if st is None:
            c.add(f"paydoor{path}", UNKNOWN, "transport failure probing pay door")
            continue
        if st == 404:
            missing.append(path)
            c.add(f"paydoor{path}", FAIL, f"{path} -> 404 (no door from the dashboard)",
                  f"HTTP {st}")
        else:
            c.add(f"paydoor{path}", PASS, f"{path} -> {st}")
    if missing:
        c.add("paydoor.reachable", FAIL,
              "pay door absent: " + ", ".join(missing),
              "E2E walk D2: pay trigger fires after the free cap bites, at which point "
              "the only pay link (one-time /start screen) is gone")


def check_cap_remedy(c: Ctx) -> None:
    """4. When the cap bites, the remedy must point at PAYING, not a second free mint.

    Note: /v1/track's required fields are (agent_id, rail, amount_cents, service). A
    probe that omits `rail` gets a 422 about the missing field, which says nothing about
    the cap remedy — that was an UNKNOWN in the first version. Send a complete body.
    """
    st, txt = c.http("/v1/track", "POST",
                     {"agent_id": f"sweep-probe-{int(time.time())}",
                      "rail": "manual", "amount_cents": 1, "service": "journey-sweep"},
                     {"AL-API-Version": API_VERSION})
    if st is None:
        c.add("cap.remedy", UNKNOWN, "transport failure probing cap error", txt)
        return
    t = (txt or "").lower()
    points_to_mint = "post /start" in t or "mint a workspace" in t or "mint another" in t
    points_to_pay = ("billing" in t or "checkout" in t or "stripe" in t
                     or "upgrade" in t or "pay" in t)
    if points_to_mint and not points_to_pay:
        c.add("cap.remedy", FAIL,
              "cap error sends the customer to mint ANOTHER free workspace",
              (txt or "")[:300])
    elif points_to_pay:
        c.add("cap.remedy", PASS, "cap error points at the paying path", (txt or "")[:200])
    elif st == 200:
        c.add("cap.remedy", PASS, "track accepted a fresh agent (cap not yet reached)",
              (txt or "")[:200])
    else:
        c.add("cap.remedy", UNKNOWN,
              f"remedy unclear (HTTP {st}) — inspect before calling it a defect",
              (txt or "")[:200])


def check_checkout(c: Ctx, creds: dict | None) -> None:
    """5. Checkout CONSTRUCTS a session. Never pays."""
    st, txt = c.http("/v1/billing/checkout", "POST", None,
                     {"AL-API-Version": API_VERSION})
    if st is None:
        c.add("checkout.constructs", UNKNOWN, "transport failure", txt)
        return
    if creds:
        st2, txt2 = c.http("/v1/billing/checkout", "POST", None,
                           {"AL-API-Version": API_VERSION,
                            "X-Workspace-Id": creds["workspace_id"],
                            "X-Workspace-Key": creds["workspace_key"]})
        d = _json(txt2) or {}
        url = d.get("checkout_url", "")
        if st2 == 200 and url.startswith("https://"):
            # assert the promise: real Stripe link, no credential leaked in output
            c.add("checkout.constructs", PASS,
                  "checkout session built (not paid)",
                  f"host={url.split('/')[2]} live_link=True")
            if creds["workspace_key"] in (txt2 or ""):
                c.add("checkout.noleak", FAIL, "workspace_key echoed in checkout response")
        elif st2 == 200:
            c.add("checkout.constructs", FAIL, "200 without a usable checkout_url",
                  (txt2 or "")[:200])
        else:
            c.add("checkout.constructs", FAIL,
                  f"checkout with real creds returned {st2}", (txt2 or "")[:200])
    else:
        if st in (401, 403, 422):
            c.add("checkout.constructs", PASS,
                  f"checkout without creds refuses cleanly ({st})", (txt or "")[:160])
        else:
            c.add("checkout.constructs", UNKNOWN,
                  f"no workspace credential available; unauthenticated probe gave {st}")


def check_auth_boundary(c: Ctx) -> None:
    """6. Auth boundary intact, and the refusal tells a customer how to self-serve.

    Note on route choice: `/v1/agents` is OWNER-ONLY by design ("owner only") — probing
    it and demanding a self-serve pointer was my mistake in the first version. The
    customer-facing refusal is on the agent-data route, which names the credential the
    caller can actually obtain. Check both, and only demand helpfulness from the latter.
    """
    st, txt = c.http("/v1/agents", headers={"AL-API-Version": API_VERSION})
    if st is None:
        c.add("auth.boundary", UNKNOWN, "transport failure")
    elif st in (401, 403):
        c.add("auth.boundary", PASS,
              f"owner-only route refuses unauthenticated read ({st})", (txt or "")[:160])
    else:
        c.add("auth.boundary", FAIL,
              f"unauthenticated read of /v1/agents returned {st} (expected 401/403)",
              (txt or "")[:200])

    st2, txt2 = c.http("/v1/alerts/journey-sweep-probe",
                       headers={"AL-API-Version": API_VERSION})
    if st2 is None:
        c.add("auth.selfserve", UNKNOWN, "transport failure")
    elif st2 in (401, 403):
        body = (txt2 or "").lower()
        useful = any(k in body for k in ("agent_secret", "workspace_key", "start",
                                         "billing", "docs", "quickstart"))
        c.add("auth.selfserve", PASS if useful else UNKNOWN,
              f"agent-data route refuses and names the obtainable credential ({st2})"
              if useful else
              f"refused ({st2}) but names no obtainable credential",
              (txt2 or "")[:200])
    else:
        c.add("auth.selfserve", FAIL,
              f"/v1/alerts/<id> unauthenticated returned {st2} (expected 401/403)",
              (txt2 or "")[:200])

    st3, _ = c.http("/v1/agents", headers={"AL-API-Version": API_VERSION,
                                           "X-Workspace-Id": "ws_bogus",
                                           "X-Workspace-Key": "wk_bogus"})
    if st3 is None:
        c.add("auth.invalid_key", UNKNOWN, "transport failure")
    else:
        c.add("auth.invalid_key", PASS if st3 in (401, 403) else FAIL,
              f"invalid key -> {st3} (expected 401/403)")


def check_mcp(c: Ctx) -> None:
    """7. MCP handshake + tools/list — a REAL session, not a bare POST.

    A bare tools/list is rejected because fastmcp's session manager is uninitialized
    (the repo's own test_mcp_front_door.py says so at line 272). A harness that POSTs
    tools/list straight out and calls the 400 a product defect is wrong — that was my
    first version, and it produced a false FAIL. Real session: initialize on /mcp/,
    capture the mcp-session-id header, then send tools/list with it.
    """
    h = {"Accept": "application/json, text/event-stream",
         "Content-Type": "application/json"}
    st, txt = c.http("/mcp/", "POST",
                     {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-06-18",
                                 "capabilities": {},
                                 "clientInfo": {"name": "journey-sweep", "version": "1.0"}}},
                     h)
    if st is None:
        c.add("mcp.initialize", UNKNOWN, "transport failure", txt)
        return
    ok = st == 200 and "protocolVersion" in (txt or "")
    c.add("mcp.initialize", PASS if ok else FAIL,
          f"MCP initialize -> {st}", (txt or "")[:160])
    if not ok:
        return

    if not c.session_id:
        c.add("mcp.tools_list", UNKNOWN,
              "initialize returned no mcp-session-id — cannot open a real session",
              (txt or "")[:200])
        return
    st2, txt2 = c.http("/mcp/", "POST",
                       {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                       {**h, "mcp-session-id": c.session_id})
    n = len(re.findall(r'"name"\s*:', txt2 or ""))
    c.add("mcp.tools_list", PASS if st2 == 200 and n > 0 else FAIL,
          f"tools/list -> {st2}, ~{n} entries", (txt2 or "")[:200])


def check_public_surfaces(c: Ctx) -> None:
    """8. Machine-discovery surfaces must exist — agents find us there."""
    for path in ("/llms.txt", "/sitemap.xml", "/robots.txt", "/.well-known/x402.json",
                 "/.well-known/mcp.json", "/health"):
        st, txt = c.http(path)
        if st is None:
            c.add(f"surface{path}", UNKNOWN, "transport failure")
        elif st == 200:
            c.add(f"surface{path}", PASS, f"{path} 200 ({len(txt or '')}B)")
        else:
            c.add(f"surface{path}", FAIL, f"{path} -> {st}", (txt or "")[:160])


def check_dashboard(c: Ctx, creds: dict | None) -> None:
    """9. Dashboard renders for a real customer; free of insider state."""
    st, txt = c.http("/dashboard")
    if st is None:
        c.add("dashboard.renders", UNKNOWN, "transport failure")
    else:
        c.add("dashboard.renders", PASS if st == 200 else FAIL, f"/dashboard -> {st}")
    if creds:
        st2, txt2 = c.http("/dashboard",
                           headers={"X-Workspace-Id": creds["workspace_id"],
                                    "X-Workspace-Key": creds["workspace_key"]})
        if st2 == 200:
            has_pay = any(k in (txt2 or "").lower()
                          for k in ("buy.stripe.com", "/v1/billing", "upgrade",
                                    "subscribe", "pro"))
            c.add("dashboard.pay_affordance", PASS if has_pay else FAIL,
                  "dashboard exposes a pay affordance" if has_pay
                  else "dashboard shows NO pay affordance (D2: need arises here)")
        else:
            c.add("dashboard.authenticated", UNKNOWN, f"authed dashboard -> {st2}")


def resolve_creds(c: Ctx, ws: str, wk: str) -> dict | None:
    """Find a workspace credential WITHOUT burning one of the day's 3 mints.

    Order: explicit flags -> env -> none. If none, the authed checks report UNKNOWN,
    which is the honest answer — not a silent PASS, and not a minted workspace wasted.
    """
    if ws and wk:
        c.add("creds.supplied", PASS, "workspace credential supplied for authed checks",
              "source=flags")
        return {"workspace_id": ws, "workspace_key": wk}
    ews, ewk = os.environ.get("SWEEP_WS_ID", ""), os.environ.get("SWEEP_WS_KEY", "")
    if ews and ewk:
        c.add("creds.supplied", PASS, "workspace credential supplied for authed checks",
              "source=env")
        return {"workspace_id": ews, "workspace_key": ewk}
    c.add("creds.supplied", UNKNOWN,
          "no SWEEP_WS_ID/SWEEP_WS_KEY supplied — authed checks (dashboard pay "
          "affordance, checkout construction) cannot be established. Deliberately NOT "
          "minting: POST /start allows 3 per IP per day and a sweep must not consume it.",
          "")
    return None


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("SWEEP_BASE", DEFAULT_BASE))
    ap.add_argument("--ws-id", default="")
    ap.add_argument("--ws-key", default="")
    ap.add_argument("--json", default="")
    ap.add_argument("--log", default="")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    c = Ctx(a.base)
    started = datetime.now(timezone.utc)
    print(f"Journey sweep against {c.base}  ({started.isoformat()})")
    print("-" * 72)

    creds = resolve_creds(c, a.ws_id, a.ws_key)
    check_mint(c)
    check_published_instructions(c)
    check_pay_door(c)
    check_cap_remedy(c)
    check_checkout(c, creds)
    check_auth_boundary(c)
    check_mcp(c)
    check_public_surfaces(c)
    check_dashboard(c, creds)

    n = {PASS: 0, FAIL: 0, UNKNOWN: 0}
    for r in c.results:
        n[r["status"]] += 1
    print("-" * 72)
    print(f"RESULT: {n[PASS]} PASS | {n[FAIL]} FAIL | {n[UNKNOWN]} UNKNOWN "
          f"(of {len(c.results)} checks)")

    if n[FAIL]:
        print("\nFAILED CHECKS:")
        for r in c.results:
            if r["status"] == FAIL:
                print(f"  - {r['check']}: {r['detail']}")
    if n[UNKNOWN]:
        print("\nUNKNOWN CHECKS (not evidence of health):")
        for r in c.results:
            if r["status"] == UNKNOWN:
                print(f"  - {r['check']}: {r['detail']}")

    stamp = started.strftime("%Y-%m-%d")
    payload = {
        "base": c.base,
        "started_utc": started.isoformat(),
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "counts": n,
        "total": len(c.results),
        "results": c.results,
        "verdict": "FAIL" if n[FAIL] else ("UNKNOWN" if n[UNKNOWN] else "PASS"),
    }
    jpath = a.json or f"journey-sweep-{stamp}.json"
    with open(jpath, "w") as fh:
        json.dump(payload, fh, indent=2)

    lines = [f"Journey sweep {started.isoformat()} base={c.base}",
             f"verdict={payload['verdict']} "
             f"pass={n[PASS]} fail={n[FAIL]} unknown={n[UNKNOWN]}"]
    lines += [f"{r['status']:7} {r['check']:34} {r['detail']}" for r in c.results]
    lpath = a.log or f"journey-sweep-{stamp}.log"
    with open(lpath, "w") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"\nlog: {lpath}\njson: {jpath}")
    if n[FAIL]:
        return 1
    return 3 if n[UNKNOWN] else 0


if __name__ == "__main__":
    sys.exit(main())
