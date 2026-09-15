#!/usr/bin/env python3
"""Verify the DEPLOYED subscription-lifecycle code, inside the container.

Runs the real deployed modules with AGENT_LEDGER_DATA pointed at a THROWAWAY
directory, so it proves production behaviour without writing a single row to
prod (customers.jsonl, metrics.jsonl, workspaces/ are untouched).

Uses the container's own STRIPE_WEBHOOK_SECRET_AL to sign, exactly as Stripe
would, so the HMAC path under test is the real one.
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="al-verify-")
os.environ["AGENT_LEDGER_DATA"] = TMP
sys.path.insert(0, '/app')

import workspace_engine as ws          # noqa: E402
import routes_billing as rb            # noqa: E402
import api_server                      # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET_AL", "")
if not SECRET:
    print("FAIL: no webhook secret in this container")
    sys.exit(1)

c = TestClient(api_server.app)
results = []


def post(payload):
    body = json.dumps(payload).encode()
    t = str(int(time.time()))
    sig = "t=%s,v1=" % t + hmac.new(
        SECRET.encode(), ("%s." % t).encode() + body, hashlib.sha256).hexdigest()
    r = c.post("/stripe/webhook", content=body, headers={"stripe-signature": sig})
    return r.status_code, r.json()


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))


# 1. a real payer gets Pro WITH a clock
wid, _ = ws.create_workspace(owner_email="verify@example.com", grant_scarcity=False)
code, body = post({"type": "checkout.session.completed", "data": {"object": {
    "id": "cs_verify", "customer": "cus_verify", "amount_total": 1900,
    "payment_status": "paid", "client_reference_id": wid,
    "subscription": "sub_verify",
    "customer_details": {"email": "verify@example.com"}}}})
check("$19 payer granted Pro", code == 200 and body.get("granted") is True, str(body))
rec = ws.get_workspace(wid)
check("Pro has a real expiry (not None)", rec.get("pro_until") is not None,
      "pro_until=%s" % rec.get("pro_until"))
check("subscription id recorded", bool(rec.get("stripe_subscription_id")))

# 2. period-end cancellation
period_end = int(time.time() + 20 * 24 * 3600)
code, body = post({"type": "customer.subscription.deleted", "data": {"object": {
    "id": "sub_verify", "customer": "cus_verify", "status": "canceled",
    "current_period_end": period_end}}})
check("cancel resolves workspace (no client_reference_id)",
      body.get("workspace_id") == wid, str(body)[:120])
check("cancel keeps Pro to period end", ws.is_workspace_pro(wid) is True)
check("pro_until == the paid period end",
      abs((ws.get_workspace(wid).get("pro_until") or 0) - period_end) < 1)

# 3. grace on failed renewal
wid2, _ = ws.create_workspace(owner_email="verify2@example.com", grant_scarcity=False)
post({"type": "checkout.session.completed", "data": {"object": {
    "id": "cs_v2", "customer": "cus_v2", "amount_total": 1900,
    "payment_status": "paid", "client_reference_id": wid2,
    "subscription": "sub_v2", "customer_details": {"email": "verify2@example.com"}}}})
code, body = post({"type": "invoice.payment_failed", "data": {"object": {
    "id": "in_v", "customer": "cus_v2", "amount_due": 1900}}})
check("failed renewal gets grace", body.get("grace") is True, "grace_days=%s" % body.get("grace_days"))
check("Pro retained during grace", ws.is_workspace_pro(wid2) is True)

# 4. renewal revenue recorded; initial invoice NOT double-counted
code, body = post({"type": "invoice.paid", "data": {"object": {
    "id": "in_v2", "customer": "cus_v2", "amount_paid": 1900,
    "billing_reason": "subscription_cycle",
    "current_period_end": int(time.time() + 30 * 24 * 3600)}}})
check("renewal recognised", body.get("renewal") is True, str(body)[:100])
code, body = post({"type": "invoice.paid", "data": {"object": {
    "id": "in_v3", "customer": "cus_verify", "amount_paid": 1900,
    "billing_reason": "subscription_create",
    "current_period_end": int(time.time() + 30 * 24 * 3600)}}})
check("initial invoice NOT counted as renewal", body.get("renewal") is False)

# 5. items.data[] period location (Stripe's newer field position)
wid3, _ = ws.create_workspace(owner_email="verify3@example.com", grant_scarcity=False)
post({"type": "checkout.session.completed", "data": {"object": {
    "id": "cs_v3", "customer": "cus_v3", "amount_total": 1900,
    "payment_status": "paid", "client_reference_id": wid3,
    "subscription": "sub_v3", "customer_details": {"email": "verify3@example.com"}}}})
pe = int(time.time() + 25 * 24 * 3600)
code, body = post({"type": "customer.subscription.updated", "data": {"object": {
    "id": "sub_v3", "customer": "cus_v3", "status": "active",
    "items": {"data": [{"current_period_end": pe}]}}}})
check("period read from items.data[]",
      abs((ws.get_workspace(wid3).get("pro_until") or 0) - pe) < 1)

# 6. fail-open: no period end must not expire a payer
wid4, _ = ws.create_workspace(owner_email="verify4@example.com", grant_scarcity=False)
post({"type": "checkout.session.completed", "data": {"object": {
    "id": "cs_v4", "customer": "cus_v4", "amount_total": 1900,
    "payment_status": "paid", "client_reference_id": wid4,
    "subscription": "sub_v4", "customer_details": {"email": "verify4@example.com"}}}})
post({"type": "customer.subscription.deleted", "data": {"object": {
    "id": "sub_v4", "customer": "cus_v4", "status": "canceled"}}})
check("no period end -> stays Pro (fail open)", ws.is_workspace_pro(wid4) is True)

# 7. unknown event does not error (or Stripe retries forever)
code, body = post({"type": "payment_intent.created", "data": {"object": {"id": "pi_1"}}})
check("unknown event 200s and is ignored", code == 200 and body.get("ignored"),
      str(body)[:80])

# 8. the gate still refuses a bad signature
r = c.post("/stripe/webhook", content=b'{"type":"invoice.paid"}',
           headers={"stripe-signature": "t=1,v1=deadbeef"})
check("forged signature still 400", r.status_code == 400, str(r.status_code))

passed = sum(1 for _, ok, _ in results if ok)
print("\nRESULT: %d/%d passed" % (passed, len(results)))
print("prod data dir used: %s (throwaway)" % TMP)
sys.exit(0 if passed == len(results) else 1)
