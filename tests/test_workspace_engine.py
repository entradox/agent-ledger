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
    """Guard rail for this phase: reissue must not be wired into any
    automatic path (spec: key rotation is explicitly out of scope)."""
    import subprocess
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    hits = subprocess.run(
        ["grep", "-rn", "reissue_key", "--include=*.py", str(repo)],
        capture_output=True, text=True).stdout.splitlines()
    callers = [h for h in hits
               if "/tests/" not in h and "workspace_engine.py" not in h]
    assert callers == [], f"reissue_key called automatically from: {callers}"
