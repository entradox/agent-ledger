import os, shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest


@pytest.fixture
def engine(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import workspace_engine
    import importlib
    importlib.reload(workspace_engine)
    yield workspace_engine
    shutil.rmtree(tmp, ignore_errors=True)


def test_create_workspace_by_email_roundtrips(engine):
    ws_id, raw_key = engine.create_workspace(owner_email="a@example.com")
    found = engine.get_workspace_by_key(raw_key)
    assert found["workspace_id"] == ws_id
    assert found["owner_email"] == "a@example.com"
    assert found["plan"] == "pro"  # First workspace gets pro via scarcity window


def test_wrong_key_returns_none(engine):
    engine.create_workspace(owner_email="a@example.com")
    assert engine.get_workspace_by_key("not-the-real-key") is None


def test_raw_key_never_persisted(engine):
    ws_id, raw_key = engine.create_workspace(owner_email="a@example.com")
    ws = engine.get_workspace(ws_id)
    assert raw_key not in str(ws)
    assert "workspace_key_hash" in ws


def test_google_sub_lookup_is_idempotent(engine):
    ws_id1, key1 = engine.create_workspace(google_sub="g-123")
    ws_id2, key2 = engine.create_workspace(google_sub="g-123")
    # second call for an existing google_sub must return the SAME workspace,
    # not silently mint a duplicate
    assert ws_id1 == ws_id2
    # ...and must NOT mint (thereby invalidating) a key as a side effect of
    # what is only an identity lookup
    assert key1 is not None
    assert key2 is None
    assert engine.get_workspace_by_key(key1)["workspace_id"] == ws_id1


def test_wallet_lookup_is_idempotent(engine):
    ws_id1, key1 = engine.create_workspace(wallet_address="0xABC")
    ws_id2, key2 = engine.create_workspace(wallet_address="0xABC")
    assert ws_id1 == ws_id2
    assert key2 is None
    # the first key still works — a repeat payment must never break it
    assert engine.get_workspace_by_key(key1)["workspace_id"] == ws_id1


def test_workspace_count_and_scarcity_cap(engine):
    assert engine.workspace_count() == 0
    engine.create_workspace(owner_email="a@example.com")
    assert engine.workspace_count() == 1


def test_mark_pro_and_is_workspace_pro(engine):
    # Fill the scarcity window so next workspace will be free
    for i in range(50):
        engine.create_workspace(owner_email=f"scarcity_{i}@example.com")
    # Now create a workspace that will be free (beyond scarcity cap)
    ws_id, _ = engine.create_workspace(owner_email="a@example.com")
    assert engine.is_workspace_pro(ws_id) is False
    engine.mark_pro(ws_id, "cus_123")
    assert engine.is_workspace_pro(ws_id) is True
    assert engine.get_workspace(ws_id)["stripe_customer_id"] == "cus_123"


def test_scarcity_grant_has_a_one_year_expiry(engine):
    """I1: the offer is "Pro free for a YEAR" — the grant must carry an
    expiry, not be unbounded."""
    import ledger_engine
    ws_id, _ = engine.create_workspace(owner_email="scarce@example.com")
    record = engine.get_workspace(ws_id)
    assert record["pro_scarcity"] is True
    assert record["pro_until"] is not None
    expected = record["created_at"] + ledger_engine.SCARCITY_PRO_DURATION_SECONDS
    assert abs(record["pro_until"] - expected) < 5


def test_expired_scarcity_grant_is_no_longer_pro(engine, monkeypatch):
    ws_id, _ = engine.create_workspace(owner_email="expiring@example.com")
    assert engine.is_workspace_pro(ws_id) is True

    record = engine.get_workspace(ws_id)
    record["pro_until"] = record["created_at"] - 1  # already lapsed
    engine._write_workspace(record)
    assert engine.is_workspace_pro(ws_id) is False


def test_expired_scarcity_grant_falls_back_to_the_free_agent_cap(engine):
    """The expiry has to reach the thing that actually enforces limits —
    agent_cap is stored as None for a scarcity workspace forever, so reading
    the raw field would keep it unbounded for life."""
    ws_id, _ = engine.create_workspace(owner_email="capcheck@example.com")
    record = engine.get_workspace(ws_id)
    assert engine.effective_agent_cap(record) is None  # grant still live

    record["pro_until"] = record["created_at"] - 1
    engine._write_workspace(record)
    assert engine.effective_agent_cap(engine.get_workspace(ws_id)) \
        == engine.WORKSPACE_FREE_AGENT_CAP


def test_stripe_pro_never_expires(engine):
    """Paid subscriptions have no pro_until — and subscribing clears any
    scarcity expiry the workspace was carrying."""
    ws_id, _ = engine.create_workspace(owner_email="payer@example.com")
    engine.mark_pro(ws_id, "cus_abc")
    record = engine.get_workspace(ws_id)
    assert record["pro_until"] is None
    assert engine.is_workspace_pro(ws_id) is True
    assert engine.effective_agent_cap(record) is None


def test_first_50_workspaces_get_scarcity_pro(engine):
    for i in range(50):
        ws_id, _ = engine.create_workspace(owner_email=f"u{i}@example.com")
        assert engine.is_workspace_pro(ws_id) is True
    ws_51, _ = engine.create_workspace(owner_email="u51@example.com")
    assert engine.is_workspace_pro(ws_51) is False


def test_create_workspace_never_reissues_as_a_side_effect(engine):
    """An identity lookup must not be destructive. Re-creating by google_sub
    returns the existing workspace with no key, and the original key keeps
    working — the previous behavior invalidated it on every Google login."""
    ws_id, old_raw_key = engine.create_workspace(google_sub="g-test-123")
    assert engine.get_workspace_by_key(old_raw_key)["workspace_id"] == ws_id

    ws_id2, new_raw_key = engine.create_workspace(google_sub="g-test-123")
    assert ws_id2 == ws_id
    assert new_raw_key is None
    assert engine.get_workspace_by_key(old_raw_key)["workspace_id"] == ws_id


def test_reissue_key_is_explicit_and_invalidates_the_old_key(engine):
    """reissue_key remains available as the building block for a future
    "I lost my key" flow — when called deliberately, it still rotates."""
    ws_id, old_raw_key = engine.create_workspace(google_sub="g-reissue")
    new_raw_key = engine.reissue_key(ws_id)

    assert new_raw_key != old_raw_key
    assert engine.get_workspace_by_key(old_raw_key) is None
    assert engine.get_workspace_by_key(new_raw_key)["workspace_id"] == ws_id


def test_reissue_key_unknown_workspace_raises(engine):
    with pytest.raises(engine.WorkspaceError):
        engine.reissue_key("ws_does_not_exist")


def test_no_automatic_caller_of_reissue_key(engine):
    """Guard rail: reissue must never fire as an automatic side effect of a
    lookup or sync path (the original bug: every Google login silently
    invalidated the user's key). One caller is sanctioned: routes_billing's
    POST /v1/billing/x402/recover, where reissue IS the explicit,
    wallet-signature-authenticated intent of the request (PR #17)."""
    import re
    import subprocess
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    hits = subprocess.run(
        ["grep", "-rn", "reissue_key", "--include=*.py", str(repo)],
        capture_output=True, text=True).stdout.splitlines()
    callers = [h for h in hits
               if "/tests/" not in h and "workspace_engine.py" not in h]

    # Every caller must live in routes_billing.py — the billing module owns the
    # only sanctioned rotation path.
    outside = [h for h in callers if "routes_billing.py" not in h]
    assert outside == [], f"reissue_key called from an unvetted module: {outside}"

    # And inside routes_billing it must be inside x402_recover specifically —
    # the endpoint gated by a wallet signature — not any other function. The
    # function's span ends at the first non-blank line back at column 0.
    src = (repo / "routes_billing.py").read_text().splitlines()
    recover_start = next(i for i, l in enumerate(src)
                         if re.match(r"\s*async def x402_recover", l))
    next_def = next((i for i in range(recover_start + 1, len(src))
                     if src[i].strip() and not src[i][0].isspace()),
                    len(src))  # x402_recover may be the last def in the file
    for h in callers:
        lineno = int(h.split(":")[1])
        assert recover_start < lineno <= next_def, (
            f"reissue_key call at routes_billing.py:{lineno} is NOT inside the "
            "wallet-signed x402_recover endpoint — rotation reachable on an "
            "unvetted path")


def test_scarcity_claims_left_counts_workspaces_not_agents(monkeypatch):
    """The launch window is per WORKSPACE (create_workspace grants on
    workspace_count() < WORKSPACE_SCARCITY_CAP), so the public counter must
    measure that population. It used to count claimed agents, which meant
    deleting an unrelated test agent moved a launch-scarcity number — observed
    live on 2026-09-13 (44 -> 46 after purging two test agents)."""
    import shutil, tempfile, importlib
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import workspace_engine, ledger_engine
    importlib.reload(workspace_engine)
    importlib.reload(ledger_engine)
    try:
        cap = workspace_engine.WORKSPACE_SCARCITY_CAP
        assert ledger_engine.scarcity_claims_left() == cap

        _, key = workspace_engine.create_workspace(owner_email="a@example.com")
        assert ledger_engine.scarcity_claims_left() == cap - 1

        # Claiming an AGENT must not move it.
        ledger_engine.ensure_agent_secret("some-agent", workspace_key=key)
        assert ledger_engine.scarcity_claims_left() == cap - 1

        # ...and it bottoms out at zero rather than going negative.
        for i in range(cap):
            workspace_engine.create_workspace(owner_email=f"x{i}@example.com")
        assert ledger_engine.scarcity_claims_left() == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_first_write_sets_the_anchor_and_is_not_returning(engine):
    """The FIRST call for a workspace sets first_write_at and reports False --
    a workspace's first write is activation, not a return."""
    ws_id, _ = engine.create_workspace(owner_email="a@example.com")
    is_returning = engine.mark_write_and_check_returning(ws_id, returning_after_seconds=86400)
    assert is_returning is False
    assert engine.get_workspace(ws_id)["first_write_at"] is not None


def test_a_write_before_the_window_is_not_returning(engine):
    ws_id, _ = engine.create_workspace(owner_email="a@example.com")
    engine.mark_write_and_check_returning(ws_id, returning_after_seconds=86400)
    is_returning = engine.mark_write_and_check_returning(ws_id, returning_after_seconds=86400)
    assert is_returning is False


def test_a_write_after_the_window_is_returning(engine):
    """Backdate the anchor rather than sleep 24h in a test suite."""
    ws_id, _ = engine.create_workspace(owner_email="a@example.com")
    engine.mark_write_and_check_returning(ws_id, returning_after_seconds=86400)
    record = engine.get_workspace(ws_id)
    record["first_write_at"] -= 90_000  # 25h in the past
    engine._write_workspace(record)
    is_returning = engine.mark_write_and_check_returning(ws_id, returning_after_seconds=86400)
    assert is_returning is True


def test_unknown_workspace_raises(engine):
    with pytest.raises(engine.WorkspaceError):
        engine.mark_write_and_check_returning("ws_does_not_exist", returning_after_seconds=86400)


def test_concurrent_mint_same_wallet_mints_once(engine):
    """Red-team 2026-10-09: the identity check + mint + index write raced.
    Two concurrent creates for one new wallet could produce two workspaces,
    with the second by_wallet write orphaning the first for wallet lookups
    (recovery included)."""
    import threading
    results = []

    def mint():
        results.append(engine.create_workspace(
            wallet_address="0xAbCdEf0000000000000000000000000000Race"))

    threads = [threading.Thread(target=mint) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ws_ids = {r[0] for r in results}
    assert len(ws_ids) == 1
    keyed = [r[1] for r in results if r[1]]
    assert len(keyed) == 1  # exactly one caller gets the raw key
    found = engine.get_workspace_by_wallet("0xabcdef0000000000000000000000000000race")
    assert found["workspace_id"] == ws_ids.pop()
