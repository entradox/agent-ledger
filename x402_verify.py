#!/usr/bin/env python3
"""x402 payment verification against a settlement facilitator. Schema
below is a placeholder shape to be confirmed against Coinbase's current
x402 facilitator docs before this ships — see task note. Kept isolated
in its own module specifically so that confirmation doesn't touch the
API route or its tests."""
import json
import os
import urllib.request

FACILITATOR_URL = os.environ.get("X402_FACILITATOR_URL", "")


def verify_payment(payment_header: str) -> dict:
    if not FACILITATOR_URL:
        raise RuntimeError("X402_FACILITATOR_URL not configured")
    req = urllib.request.Request(
        FACILITATOR_URL, method="POST",
        data=json.dumps({"payment": payment_header}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    return {
        "verified": bool(result.get("verified")),
        "payer_wallet": result.get("payer"),
        "tx_hash": result.get("transaction_hash"),
        # Settlement detail, surfaced so the route can sanity-check what was
        # actually paid and to whom before minting anything. Field names on
        # the facilitator side are part of the same unconfirmed placeholder
        # schema as the ones above — .get() throughout, so an absent field is
        # None and the caller decides whether that is fatal (the route
        # rejects a missing recipient when a receiving address is configured).
        "recipient": result.get("recipient") or result.get("pay_to"),
        "amount": result.get("amount"),
        "asset": result.get("asset"),
    }
