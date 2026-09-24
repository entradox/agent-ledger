"""Post-ship verification for AgentLedger 0.4.2 — the checks that need a credential.

The ship script proved the credential-free checks (unsigned webhook 400, machine
/start 409). It could NOT prove the billing-route refusals: without a workspace key,
`/v1/billing/checkout` answers 401 from its own auth gate BEFORE reaching the new
409. Muse's verification list asks for Team 409, so this closes that gap with a real
demo workspace.

Read-only against the live service, except minting ONE demo-tagged workspace, which
the onboarding funnel excludes from demand counts (?demo=1).
"""
import json
import urllib.error
import urllib.request

BASE = "https://aiagentscity.com"
STARTER_LINK = "https://buy.stripe.com/14AbJ0clUeoE9QN3Nl2400e"


def call(method, path, headers=None, data=None, timeout=30):
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:                       # noqa: BLE001
        return "ERR", repr(e)


def mint():
    """A demo workspace so the funnel excludes it from demand counts."""
    code, body = call("POST", "/start?demo=1", {"Accept": "application/json"})
    if code != 200:
        return None, None, (code, body[:200])
    j = json.loads(body)
    return j["workspace_id"], j["workspace_key"], None


def main():
    print("=== AUTHENTICATED SPOT CHECKS (need a workspace key) ===\n")

    ws, key, err = mint()
    if err:
        print(f"mint blocked: HTTP {err[0]} — {err[1]}")
        print("(per-IP rate limit is 3/day; retry later or reuse a key)\n")
        return
    print(f"[mint] demo workspace {ws} (funnel-excluded)\n")
    auth = {"X-Workspace-Id": ws, "X-Workspace-Key": key,
            "Accept": "application/json"}

    print("--- Team checkout WITHOUT a Team link (the item-2 defect) ---")
    code, body = call("POST", "/v1/billing/checkout?plan=team", auth, data=b"")
    print(f"POST /v1/billing/checkout?plan=team -> HTTP {code}")
    print(f"  {body[:220]}")
    print(f"  refused?          {code == 409}")
    print(f"  leaked $19 link?  {STARTER_LINK in body}  (must be False)")
    print()

    print("--- Starter checkout still returns the real $19 link (happy path) ---")
    code, body = call("POST", "/v1/billing/checkout?plan=starter", auth, data=b"")
    print(f"POST /v1/billing/checkout?plan=starter -> HTTP {code}")
    try:
        j = json.loads(body)
        print(f"  plan={j.get('plan')!r}")
        print(f"  checkout_url={j.get('checkout_url','')[:70]}")
        print(f"  is the live $19 link? {j.get('checkout_url','').startswith(STARTER_LINK)}")
        print(f"  carries client_reference_id? {ws in j.get('checkout_url','')}")
    except Exception:                            # noqa: BLE001
        print(f"  {body[:220]}")
    print()

    print("--- machine /start?plan=starter refused BEFORE minting (item-3 defect) ---")
    code, body = call("POST", "/start?plan=starter", {"Accept": "application/json"})
    print(f"POST /start?plan=starter -> HTTP {code}")
    print(f"  {body[:220]}")
    print(f"  refused?            {code == 409}")
    print(f"  minted a key?       {'wk_live_' in body}  (must be False)")
    print()

    print("--- ordinary FREE machine mint still works (happy path) ---")
    code, body = call("POST", "/start", {"Accept": "application/json"})
    print(f"POST /start -> HTTP {code}")
    try:
        j = json.loads(body)
        print(f"  plan={j.get('plan')!r}  key starts wk_live? "
              f"{str(j.get('workspace_key','')).startswith('wk_live_')}")
    except Exception:                            # noqa: BLE001
        print(f"  {body[:200]}")
    print()

    print("--- llms.txt matches shipped behaviour ---")
    code, body = call("GET", "/llms.txt")
    print(f"GET /llms.txt -> HTTP {code}")
    print(f"  still says 'subscribe at /start'?  {'subscribe at /start' in body}  (must be False)")
    print(f"  names /v1/billing/checkout?         {'/v1/billing/checkout' in body}")
    print(f"  says the monthly plans need a human? {'human' in body.lower()}")


if __name__ == "__main__":
    main()
