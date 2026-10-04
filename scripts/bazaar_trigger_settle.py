#!/usr/bin/env /opt/miniconda3/bin/python3
"""Trigger one real x402 settlement so the CDP Bazaar catalogs the endpoint.

The Bazaar indexes a resource only when a settled PaymentPayload carries the
bazaar extension (deployed bb292f6/f2bf665 today). This makes one $0.01
self-payment through the live route — same as the 4 prior self-tests — so the
facilitator sees the extension and catalogs us. Costs 0.01 USDC + gas from
X402_PRIVATE_KEY, already earmarked for x402 self-tests.
"""
import base64, json, os, sys, urllib.request

sys.path.insert(0, os.path.expanduser("~/AI-Workbench/projects/agent-ledger/repo"))

URL = "https://aiagentscity.com/v1/billing/x402"
NETWORK = "eip155:8453"

from eth_account import Account
from x402 import x402ClientSync
from x402.http import (decode_payment_required_header,
                       encode_payment_signature_header,
                       PAYMENT_SIGNATURE_HEADER)
from x402.mechanisms.evm.exact import ExactEvmClientScheme
from x402.mechanisms.evm.signers import EthAccountSigner

key = os.environ["X402_PRIVATE_KEY"]
acct = Account.from_key(key)
print("payer:", acct.address)

# 1. fetch the 402 — must carry the bazaar extension now
req = urllib.request.Request(URL, method="POST", data=b"{}",
                             headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(req)
    sys.exit("expected 402")
except urllib.error.HTTPError as e:
    assert e.code == 402, e.code
    pr = decode_payment_required_header(e.headers["PAYMENT-REQUIRED"])

exts = getattr(pr, "extensions", None) or {}
print("extensions advertised:", list(exts.keys()))
if "bazaar" not in exts:
    sys.exit("bazaar extension NOT in the 402 — deploy hasn't rolled out yet")

# 2. build the signed payload — echoes the extensions back to us/facilitator
client = x402ClientSync()
client.register(NETWORK, ExactEvmClientScheme(EthAccountSigner(acct)))
payload = client.create_payment_payload(pr)
pext = getattr(payload, "extensions", None) or {}
print("extensions in payload:", list(pext.keys() if isinstance(pext, dict) else pext))

# 3. settle
hdr = encode_payment_signature_header(payload)
req2 = urllib.request.Request(URL, method="POST", data=b"",
                              headers={PAYMENT_SIGNATURE_HEADER: hdr})
try:
    r = urllib.request.urlopen(req2)
    body = json.loads(r.read())
    print("status:", r.status)
    print(json.dumps(body, indent=1)[:1200])
except urllib.error.HTTPError as e:
    print("settle failed:", e.code, e.read().decode()[:600])
