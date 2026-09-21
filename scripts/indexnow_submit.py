#!/usr/bin/env python3
"""Submit the AgentLedger sitemap to IndexNow.

IndexNow is a shared feed: one POST notifies Bing, Yandex, Seznam and Naver.
Google does NOT participate (it retired its sitemap ping in 2023), so this
accelerates the engines that take it and does nothing for Google — which finds
these pages through the sitemap and canonical alone.

Run this DELIBERATELY, not from the web process:

    /opt/miniconda3/bin/python3 scripts/indexnow_submit.py            # all sitemap URLs
    /opt/miniconda3/bin/python3 scripts/indexnow_submit.py /pricing   # specific paths

Why not automatic: a request handler has no business making outbound calls, and
IndexNow expects a submission when content actually CHANGES. Fire it after a
deploy that changes page content, or on a schedule — not on every request.

Exit codes: 0 = accepted, 1 = rejected/failed. 200 or 202 from the endpoint both
mean accepted (200 = key validated immediately, 202 = queued for validation).
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request

BASE = "https://aiagentscity.com"
ENDPOINT = "https://api.indexnow.org/IndexNow"
HOST = "aiagentscity.com"
KEY_LOCATION = f"{BASE}/indexnow.txt"


def _get(url: str, timeout: int = 30) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode()


def fetch_key() -> str:
    """Read the published key. It must match what we submit with, or IndexNow
    rejects the whole batch as unproven (HTTP 422) — so fetch it rather than
    hardcoding a second copy that can drift from the server's."""
    return _get(KEY_LOCATION).strip()


def url_list(paths: list[str] | None) -> list[str]:
    if paths:
        return [p if p.startswith("http") else f"{BASE}{p}" for p in paths]
    body = _get(f"{BASE}/sitemap.xml")
    return re.findall(r"<loc>([^<]+)</loc>", body)


def submit(urls: list[str], key: str) -> int:
    payload = json.dumps({
        "host": HOST,
        "key": key,
        "keyLocation": KEY_LOCATION,
        "urlList": urls,
    }).encode()
    req = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print(f"IndexNow accepted: HTTP {r.status} ({len(urls)} URLs)")
            return 0
    except urllib.error.HTTPError as e:
        # 403 = key file not found/does not match; 422 = URLs invalid or not
        # under the declared host. Both mean nothing was queued.
        print(f"IndexNow REJECTED: HTTP {e.code} — {e.read().decode()[:300]}",
              file=sys.stderr)
        return 1
    except Exception as e:  # network/DNS — nothing submitted
        print(f"IndexNow submission failed: {e!r}", file=sys.stderr)
        return 1


def main(argv: list[str]) -> int:
    key = fetch_key()
    if not key or len(key) < 8:
        print(f"key file at {KEY_LOCATION} looks wrong: {key!r}", file=sys.stderr)
        return 1
    urls = url_list(argv[1:] or None)
    if not urls:
        print("no URLs to submit (empty sitemap?)", file=sys.stderr)
        return 1
    print(f"submitting {len(urls)} URLs with key {key[:8]}…")
    return submit(urls, key)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
