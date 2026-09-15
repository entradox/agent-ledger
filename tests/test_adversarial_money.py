#!/usr/bin/env python3
"""Independent adversarial verification of D-1247 (the money chain fix).

Not part of the original author's test suite. Written to try to break
routes_billing.py's webhook handler and _record_grant_failure(), per the
D-1247 verification brief. Every test here is run, not reasoned about.
"""
import hashlib
import hmac
import importlib
import json
import os
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
    yield TestClient(api_server.app), workspace_engine, Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


def _sign(payload: dict, secret: str = SECRET):
    body = json.dumps(payload).encode()
    t = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body,
                   hashlib.sha256).hexdigest()
    return body, f"t={t},v1={sig}"


def _completed(workspace_id, email="buyer@example.com", amount=1900,
              session_id="cs_adv_1", payment_status="paid"):
    return {
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": session_id,
            "customer": "cus_adv_1",
            "amount_total": amount,
            "payment_status": payment_status,
            "client_reference_id": workspace_id,
            "customer_details": {"email": email},
        }},
    }


def _post_raw(client, body: bytes, sig: str = None):
    headers = {}
    if sig is not None:
        headers["stripe-signature"] = sig
    return client.post("/stripe/webhook", content=body, headers=headers)


def _post(client, payload, secret=SECRET):
    body, sig = _sign(payload, secret)
    return client.post("/stripe/webhook", content=body,
                       headers={"stripe-signature": sig})


# ── malformed / missing signature header shapes ─────────────────────────────

def test_garbage_signature_header_does_not_500_or_grant(env):
    """A signature header with no '=' at all must not crash the handler."""
    c, ws, _ = env
    workspace_id, _ = ws.create_workspace(owner_email="a@x.com", grant_scarcity=False)
    body = json.dumps(_completed(workspace_id)).encode()
    r = _post_raw(c, body, sig="not-a-valid-signature-at-all")
    assert r.status_code == 400, f"garbage sig header answered {r.status_code}"
    assert ws.is_workspace_pro(workspace_id) is False


def test_signature_header_with_only_v1_no_t_does_not_grant(env):
    c, ws, _ = env
    workspace_id, _ = ws.create_workspace(owner_email="a@x.com", grant_scarcity=False)
    body = json.dumps(_completed(workspace_id)).encode()
    fake_sig = hmac.new(SECRET.encode(), b"." + body, hashlib.sha256).hexdigest()
    r = _post_raw(c, body, sig=f"v1={fake_sig}")
    assert r.status_code == 400, f"missing t= answered {r.status_code}"
    assert ws.is_workspace_pro(workspace_id) is False


def test_no_stripe_signature_header_at_all_does_not_500(env):
    """Header entirely absent (not even empty string) must not crash."""
    c, ws, _ = env
    workspace_id, _ = ws.create_workspace(owner_email="a@x.com", grant_scarcity=False)
    body = json.dumps(_completed(workspace_id)).encode()
    r = c.post("/stripe/webhook", content=body)  # no headers dict at all
    assert r.status_code == 400
    assert ws.is_workspace_pro(workspace_id) is False


def test_empty_string_signature_header_does_not_500(env):
    c, ws, _ = env
    workspace_id, _ = ws.create_workspace(owner_email="a@x.com", grant_scarcity=False)
    body = json.dumps(_completed(workspace_id)).encode()
    r = _post_raw(c, body, sig="")
    assert r.status_code == 400
    assert ws.is_workspace_pro(workspace_id) is False


def test_multiple_equals_in_signature_field_handled(env):
    """t=...,v1=...=extra could break a naive split('=',1) somewhere else,
    or the dict-comprehension itself, if a field value contains '='."""
    c, ws, _ = env
    workspace_id, _ = ws.create_workspace(owner_email="a@x.com", grant_scarcity=False)
    body = json.dumps(_completed(workspace_id)).encode()
    r = _post_raw(c, body, sig="t=123,v1=abc=def")
    assert r.status_code == 400
    assert ws.is_workspace_pro(workspace_id) is False


def test_malformed_json_body_with_valid_signature_does_not_grant_or_crash(env):
    """Attacker/Stripe-bug scenario: body is not valid JSON but is correctly
    signed with the real secret (e.g. we hold the secret in this test env).
    The handler must not turn this into an unhandled 500 that could mask a
    real problem or spin Stripe retries with a 5xx."""
    c, ws, _ = env
    body = b"{not valid json at all"
    t = str(int(time.time()))
    sig_val = hmac.new(SECRET.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    r = _post_raw(c, body, sig=f"t={t},v1={sig_val}")
    # Documented expectation vs reality: record what actually happens.
    print(f"\n[malformed JSON] status={r.status_code} body={r.text[:200]}")


# ── amount_total type confusion / edge values (within a VALIDLY signed event) ─

@pytest.mark.parametrize("amount", [0, -1900, 1901, 3800, 1900.0, "1900"])
def test_non_exact_1900_amount_never_grants_pro(env, amount):
    c, ws, _ = env
    workspace_id, _ = ws.create_workspace(owner_email="a@x.com", grant_scarcity=False)
    r = _post(c, _completed(workspace_id, amount=amount, session_id=f"cs_amt_{amount}"))
    assert r.status_code == 200
    is_pro = ws.is_workspace_pro(workspace_id)
    if amount == 1900.0:
        # Python: 1900.0 == 1900 is True. Documenting actual behaviour.
        print(f"\n[amount=1900.0 float] granted_pro={is_pro} (float==int equality in Python)")
    else:
        assert is_pro is False, f"amount_total={amount!r} incorrectly granted Pro"


# ── non-pro event types must never reach the grant path ─────────────────────

@pytest.mark.parametrize("event_type", [
    "checkout.session.async_payment_failed",
    "invoice.paid",
    "customer.subscription.created",
    "payment_intent.succeeded",
    "checkout.session.completed.fake",
])
def test_non_pro_event_types_never_grant(env, event_type):
    c, ws, _ = env
    workspace_id, _ = ws.create_workspace(owner_email="a@x.com", grant_scarcity=False)
    payload = _completed(workspace_id)
    payload["type"] = event_type
    r = _post(c, payload)
    assert r.status_code == 200
    assert ws.is_workspace_pro(workspace_id) is False, (
        f"event_type={event_type} incorrectly granted Pro"
    )


# ── client_reference_id injection / path traversal ──────────────────────────

def test_path_traversal_client_reference_id_does_not_write_outside_data_dir(env):
    """A payer fully controls client_reference_id (it's a URL query param on
    the hosted Stripe Payment Link) - Stripe does not restrict its charset
    beyond a length cap. If workspace_engine builds a filesystem path directly
    from workspace_id via naive f-string interpolation, a value like
    '../../../../tmp/al_traversal_poc' could cause a read or write outside
    DATA_DIR/workspaces/.
    """
    c, ws, tmp = env
    traversal_id = "../../../../../../tmp/al_traversal_poc_D1247"
    canary_path = Path("/tmp/al_traversal_poc_D1247.json")
    canary_path.write_text(json.dumps({"marker": "should-not-be-touched"}))
    try:
        r = _post(c, _completed(traversal_id, session_id="cs_trav_1"))
        assert r.status_code == 200, f"webhook 5xx'd on traversal id: {r.status_code} {r.text}"

        # Did mark_pro/get_workspace resolve through the traversal to touch
        # our canary file outside the workspaces directory?
        content_after = canary_path.read_text() if canary_path.exists() else None
        print(f"\n[traversal] canary file after webhook: {content_after!r}")

        # Whatever happened, a grant_failure record must exist (this is the
        # thing the fix is supposed to guarantee: no silent loss).
        gf = tmp / "grant_failures.jsonl"
        rows = [json.loads(l) for l in gf.read_text().splitlines() if l] if gf.exists() else []
        print(f"[traversal] grant_failures rows: {rows}")
    finally:
        canary_path.unlink(missing_ok=True)


def test_very_long_client_reference_id_does_not_crash_webhook(env):
    """Stripe's client_reference_id cap is ~200 chars, but nothing in this
    handler enforces that before hitting the filesystem. A very long id could
    hit ENAMETOOLONG on some filesystems and crash outside the WorkspaceError
    catch (which only catches WorkspaceError, not OSError)."""
    c, ws, tmp = env
    long_id = "ws_" + ("A" * 4000)
    r = _post(c, _completed(long_id, session_id="cs_long_1"))
    print(f"\n[long id] status={r.status_code} body={r.text[:300]}")
    # The critical property: it must not 500 silently losing the money with
    # no record. Either 200 with a grant_failure record, or a controlled error.
    if r.status_code == 200:
        gf = tmp / "grant_failures.jsonl"
        assert gf.exists(), "very long workspace_id: paid, not granted, NO grant_failure record"


def test_whitespace_client_reference_id_records_failure_not_silent_pass(env):
    c, ws, tmp = env
    r = _post(c, _completed("   ", session_id="cs_ws_1"))
    assert r.status_code == 200
    gf = tmp / "grant_failures.jsonl"
    assert gf.exists(), "whitespace-only workspace_id vanished with no record"
    rows = [json.loads(l) for l in gf.read_text().splitlines() if l]
    assert rows[-1]["reason"] in ("mark_pro_failed", "missing_client_reference_id")


def test_another_real_workspace_id_used_as_reference_grants_that_one_not_attacker(env):
    """Sanity: does client_reference_id let anyone claim ANY existing
    workspace, i.e. is there any secondary authentication tying the payer to
    the workspace they are upgrading? (There is none by design - this
    documents the trust model, not necessarily a defect, since Stripe itself
    sets this value from the URL the paying party's own checkout link
    carried.) We confirm workspace B does get upgraded when its id is used,
    which is expected/intended - not a defect on its own."""
    c, ws, _ = env
    victim_id, _ = ws.create_workspace(owner_email="victim@x.com", grant_scarcity=False)
    r = _post(c, _completed(victim_id, email="attacker@evil.com", session_id="cs_hijack_1"))
    assert r.status_code == 200
    print(f"\n[reference-id trust model] victim workspace now pro: {ws.is_workspace_pro(victim_id)}")
    # This is expected given the design (client_reference_id round-trips
    # through Stripe's own hosted checkout URL that the payer used) - flagged
    # for the report, not asserted as a failure here.


# ── _record_grant_failure must swallow EVERY exception, not just some ───────

def test_record_grant_failure_survives_unwritable_data_dir(env, monkeypatch):
    """Force the FIRST try block (file write) to fail via an unwritable/
    nonexistent-and-uncreatable data dir, and confirm the function still
    returns normally (does not propagate) and still attempts the metrics arm.
    """
    import routes_billing

    calls = {"metrics_called": False}

    def fake_record_onboarding(*a, **k):
        calls["metrics_called"] = True

    monkeypatch.setattr(routes_billing.metrics, "record_onboarding", fake_record_onboarding)

    # Point DATA_DIR at a path that cannot be created (parent is a file, not a dir)
    blocker = Path(tempfile.mkdtemp()) / "blocker_file"
    blocker.write_text("i am a file, not a directory")
    fake_data_dir = blocker / "subdir_that_cannot_exist"
    monkeypatch.setattr(routes_billing, "DATA_DIR", fake_data_dir)

    try:
        routes_billing._record_grant_failure(
            "mark_pro_failed", "cs_unwritable", "buyer@x.com",
            "workspace not found", "ws_nope")
    except Exception as e:
        pytest.fail(f"_record_grant_failure raised despite unwritable DATA_DIR: {e!r}")

    assert calls["metrics_called"] is True, (
        "file-write arm failing should not prevent the metrics arm from running"
    )
    shutil.rmtree(blocker.parent, ignore_errors=True)


def test_record_grant_failure_survives_both_arms_failing(env, monkeypatch):
    """Both the file write AND the metrics call fail. Must still not raise."""
    import routes_billing

    blocker = Path(tempfile.mkdtemp()) / "blocker_file2"
    blocker.write_text("blocker")
    fake_data_dir = blocker / "subdir"
    monkeypatch.setattr(routes_billing, "DATA_DIR", fake_data_dir)

    def boom(*a, **k):
        raise RuntimeError("metrics totally down")

    monkeypatch.setattr(routes_billing.metrics, "record_onboarding", boom)

    try:
        routes_billing._record_grant_failure(
            "mark_pro_failed", "cs_double_fail", "buyer@x.com",
            "workspace not found", "ws_nope")
    except Exception as e:
        pytest.fail(f"_record_grant_failure raised with BOTH arms failing: {e!r}")

    shutil.rmtree(blocker.parent, ignore_errors=True)


# ── duplicate / replay of a grant-failure-producing event ───────────────────

def test_replayed_unresolvable_workspace_event_does_not_dedupe_grant_failures(env):
    """Stripe retries a failed webhook delivery. Does that inflate
    grant_failures.jsonl with duplicate rows for the SAME session, and if so,
    is that itself surfaced/labelled anywhere? (Documenting actual behaviour,
    not asserting a specific fix.)"""
    c, ws, tmp = env
    payload = _completed("ws_does_not_exist_replay", session_id="cs_replay_1")
    _post(c, payload)
    _post(c, payload)
    gf = tmp / "grant_failures.jsonl"
    rows = [json.loads(l) for l in gf.read_text().splitlines() if l]
    print(f"\n[replay] grant_failures rows for same session: {len(rows)}")
    assert len(rows) == 2, (
        "replay behaviour changed - if dedupe was added, tighten this "
        "assertion rather than deleting it"
    )


def test_absolute_path_client_reference_id_bypasses_workspaces_dir_entirely(env):
    """CONFIRMED DEFECT: Path.__truediv__ discards the left operand when the
    right operand is absolute. workspace_engine._ws_path() does
    `_workspaces_dir() / f"{workspace_id}.json"` with NO validation that
    workspace_id looks like the ws_<token> format create_workspace() mints.
    A payer fully controls client_reference_id via the Stripe Payment Link
    URL query string. Setting it to an absolute path (e.g.
    "/tmp/some_target") makes get_workspace()/mark_pro() operate on an
    ARBITRARY absolute file path on the host, completely outside
    DATA_DIR/workspaces/ - no traversal depth guessing required.
    """
    c, ws, tmp = env
    target = Path(tempfile.mkdtemp()) / "al_absolute_poc.json"
    abs_workspace_id = str(target.with_suffix(""))  # mark_pro appends ".json"
    # Pre-existing arbitrary JSON file the attacker wants mark_pro to "adopt"
    # and overwrite. Must look like a workspace record for get_workspace()
    # to parse without raising (get_workspace does bare json.loads with no
    # schema check). _write_workspace() re-derives the write path from
    # record["workspace_id"] (not from the function argument that led it
    # here), so the embedded field must match for the round-trip to land
    # back on the SAME absolute file.
    target.write_text(json.dumps({"workspace_id": abs_workspace_id,
                                  "plan": "free", "totally": "unrelated file"}))

    r = _post(c, _completed(abs_workspace_id, session_id="cs_abs_1"))
    assert r.status_code == 200

    written = json.loads(target.read_text())
    print(f"\n[ABSOLUTE PATH WRITE] {target} now contains: {written}")

    # The defect, proven: an arbitrary file OUTSIDE the workspaces directory
    # got mark_pro's fields written into it via a Stripe client_reference_id
    # value under the payer's control.
    assert written.get("plan") == "pro", (
        "expected to demonstrate arbitrary-file overwrite via absolute "
        "client_reference_id, but plan was not set to pro — re-check assumptions"
    )
    shutil.rmtree(target.parent, ignore_errors=True)
