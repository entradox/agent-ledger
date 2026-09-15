#!/usr/bin/env python3
"""PoC for the absolute-path client_reference_id defect (verifier report #3).

Reproduces, under test, the claim that a payer-controlled client_reference_id
starting with "/" escapes the workspaces directory and writes mark_pro()'s
fields into an arbitrary absolute path.

Run: /opt/miniconda3/bin/python3 -m pytest tests/test_poc_absolute_path.py -v
"""
import hashlib
import hmac
import importlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
SECRET = "whsec_test"


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET_AL", SECRET)
    import ledger_engine, workspace_engine, identity, routes_billing, api_server
    for m in (workspace_engine, ledger_engine, identity, routes_billing, api_server):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    yield TestClient(api_server.app), workspace_engine
    shutil.rmtree(tmp, ignore_errors=True)


def _sign(payload):
    body = json.dumps(payload).encode()
    t = str(int(time.time()))
    return body, f"t={t},v1=" + hmac.new(
        SECRET.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()


def test_absolute_client_reference_id_does_not_write_outside_workspaces_dir(env):
    """A client_reference_id starting with '/' must never become a path.

    Path.__truediv__ discards the left operand when the right is absolute:
        Path("/data/workspaces") / "/tmp/evil.json"  ==  Path("/tmp/evil.json")

    mark_pro() does `_workspaces_dir() / f"{workspace_id}.json"`, so an absolute
    id escapes the workspaces directory entirely. client_reference_id is a URL
    query parameter the payer controls, and Stripe's 200-char cap leaves plenty
    of room for a path.
    """
    c, ws = env

    # A file that already exists at an attacker-chosen absolute path, and that
    # contains a workspace_id field equal to that same path — the shape
    # mark_pro() will happily rewrite.
    target = Path(tempfile.gettempdir()) / "al_poc_target.json"
    ref = str(target)[: -len(".json")]     # what the payer puts in the URL
    target.write_text(json.dumps({
        # mark_pro() rewrites the record at _ws_path(record["workspace_id"]),
        # so for the write to land back on THIS file its workspace_id must
        # equal the same reference that opened it — path without ".json".
        "workspace_id": ref,
        "plan": "free",
        "pro_until": 12345,
        "stripe_customer_id": None,
    }))
    before = json.loads(target.read_text())
    assert before["plan"] == "free", "precondition: target starts non-pro"

    payload = {
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_poc_abs",
            "customer": "cus_poc",
            "amount_total": 1900,
            "payment_status": "paid",
            "customer_details": {"email": "attacker@example.com"},
            "client_reference_id": ref,
        }},
    }
    body, sig = _sign(payload)
    r = c.post("/stripe/webhook", content=body, headers={"stripe-signature": sig})
    assert r.status_code == 200

    after = json.loads(target.read_text())
    target.unlink(missing_ok=True)

    assert after["plan"] != "pro", (
        "ARBITRARY FILE WRITE: a payer-controlled client_reference_id caused "
        f"mark_pro() to rewrite {target} outside the workspaces directory "
        f"(plan {before['plan']!r} -> {after['plan']!r})"
    )
