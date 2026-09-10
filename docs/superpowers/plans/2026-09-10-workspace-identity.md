# Workspace Identity & Per-Customer Billing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Execute via Command Code (`command-code -p ... --auto-accept`), not Claude Code CLI** — this is the Workbench's default coding executor as of 2026-09-10. Run tasks 1-3 sequentially (each depends on the last); tasks 4, 5, and 6 are independent of each other once Task 3 lands and may run as parallel Command Code subagents. **Every task, before being marked done, gets a separate red-team/security pass** (see "Required Review Per Task" below) — this is not optional given the auth/payments surface.

**Goal:** Replace AgentLedger's single global free-cap/pro-flag with per-workspace identity and billing — Google OAuth for human operators, x402 self-serve for autonomous agents with no human owner, and workspace-scoped reads so customers can't see each other's data.

**Architecture:** A new `workspace_engine.py` module owns workspace records (flat JSON files, same pattern as today's per-agent storage — no new database this phase). `ledger_engine.ensure_agent_secret()` gains a `workspace_key` precondition on brand-new claims only; already-claimed agents keep authenticating with their existing `agent_secret`, unchanged. `api_server.py` gains OAuth/session/dashboard routes, a per-workspace Stripe Checkout flow, and an x402 payment endpoint. Read endpoints gain an ownership check.

**Tech Stack:** FastAPI (existing), stdlib `urllib.request`/`hmac`/`hashlib`/`secrets` only — no new dependencies (matches the project's existing zero-extra-dependency style; Google OAuth, Stripe Checkout Session creation, and x402 facilitator verification are all plain REST calls, not SDK-dependent).

**Spec:** `docs/superpowers/specs/2026-09-10-workspace-identity-design.md`

## Global Constraints

- No new entries in `requirements.txt` — everything is a plain HTTP call via `urllib.request`, matching the codebase's existing style (see `send_onboarding_email.py`, `agent-ledger-dogfood-record.py`).
- `agent_secret` behavior for already-claimed agents does not change in any task — only the *claim* step (first write to a brand-new `agent_id`) gains the `workspace_key` requirement.
- Every new/changed endpoint returns the existing typed error envelope (`error_envelope()` in `ledger_engine.py`) on failure — no bare `HTTPException(detail=str)`.
- `agent_id`, `rail`, `amount_cents` validation rules are unchanged — do not touch `validate_agent_id()` or the rail whitelist.
- Every task ends with `/opt/miniconda3/bin/python3 -m pytest -q` fully green (79 existing tests + new ones) before commit.
- Workspace records live under `DATA_DIR / "workspaces"` — sibling to the existing `DATA_DIR / "agents"` directory, same `AGENT_LEDGER_DATA` env var.

## Required Review Per Task (all tasks)

After a task's steps are complete and tests pass, before marking it done:
1. Dispatch a fresh **security/red-team review** subagent (no context from the implementing agent — give it only the task's diff and the spec section it implements) with this brief: "Review this diff for auth bypass, injection, secret leakage into logs/errors, and idempotency/replay bugs. This is a payments/auth surface — assume an adversarial caller." Findings block the task from being marked done until resolved or explicitly accepted with a written reason.
2. This mirrors the existing repo pattern (`efbf18f`→`3391b3b`: Morgan-review-found issues fixed same day) — same rigor, applied per-task instead of per-release so issues are caught before they compound across parallel tasks.

---

## Task 1: Workspace engine core

**Files:**
- Create: `workspace_engine.py`
- Test: `tests/test_workspace_engine.py`

**Interfaces:**
- Produces: `create_workspace(*, owner_email: str | None = None, google_sub: str | None = None, wallet_address: str | None = None) -> tuple[str, str]` (returns `(workspace_id, raw_key)`, raw key shown once at record-creation, then only its hash is stored)
- Produces: `get_workspace_by_key(raw_key: str) -> dict | None`
- Produces: `get_workspace(workspace_id: str) -> dict | None`
- Produces: `get_workspace_by_google_sub(google_sub: str) -> dict | None`
- Produces: `get_workspace_by_wallet(wallet_address: str) -> dict | None`
- Produces: `workspace_count() -> int`
- Produces: `mark_pro(workspace_id: str, stripe_customer_id: str) -> None`
- Produces: `is_workspace_pro(workspace_id: str) -> bool`
- Produces: `WORKSPACE_SCARCITY_CAP = 50`, `WORKSPACE_FREE_AGENT_CAP = 3` (module constants)
- Produces: `class WorkspaceError(Exception)` — raised on not-found/invalid lookups callers must handle

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_workspace_engine.py
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
    assert found["plan"] == "free"


def test_wrong_key_returns_none(engine):
    engine.create_workspace(owner_email="a@example.com")
    assert engine.get_workspace_by_key("not-the-real-key") is None


def test_raw_key_never_persisted(engine):
    ws_id, raw_key = engine.create_workspace(owner_email="a@example.com")
    ws = engine.get_workspace(ws_id)
    assert raw_key not in str(ws)
    assert "workspace_key_hash" in ws


def test_google_sub_lookup_is_idempotent(engine):
    ws_id1, _ = engine.create_workspace(google_sub="g-123")
    ws_id2, _ = engine.create_workspace(google_sub="g-123")
    # second call for an existing google_sub must return the SAME workspace,
    # not silently mint a duplicate
    assert ws_id1 == ws_id2


def test_wallet_lookup_is_idempotent(engine):
    ws_id1, _ = engine.create_workspace(wallet_address="0xABC")
    ws_id2, _ = engine.create_workspace(wallet_address="0xABC")
    assert ws_id1 == ws_id2


def test_workspace_count_and_scarcity_cap(engine):
    assert engine.workspace_count() == 0
    engine.create_workspace(owner_email="a@example.com")
    assert engine.workspace_count() == 1


def test_mark_pro_and_is_workspace_pro(engine):
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/AI-Workbench/projects/agent-ledger/repo && /opt/miniconda3/bin/python3 -m pytest tests/test_workspace_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'workspace_engine'`

- [ ] **Step 3: Implement workspace_engine.py**

```python
#!/usr/bin/env python3
"""Workspace identity & billing state — the per-customer layer sitting
above the existing per-agent ledger. Flat-file storage (same pattern as
agent secrets/ledgers today), scoped under DATA_DIR/workspaces/. See
docs/superpowers/specs/2026-09-10-workspace-identity-design.md.
"""
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

DATA_DIR = Path(os.environ.get("AGENT_LEDGER_DATA", os.path.expanduser("~/.agent-ledger")))
WORKSPACE_SCARCITY_CAP = 50
WORKSPACE_FREE_AGENT_CAP = 3


class WorkspaceError(Exception):
    """Raised for invalid workspace lookups callers must handle explicitly."""


def _workspaces_dir() -> Path:
    d = DATA_DIR / "workspaces"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ws_path(workspace_id: str) -> Path:
    return _workspaces_dir() / f"{workspace_id}.json"


def _index_dir(name: str) -> Path:
    d = _workspaces_dir() / f"index_{name}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def workspace_count() -> int:
    return sum(1 for f in _workspaces_dir().glob("*.json"))


def _write_workspace(record: dict) -> None:
    _ws_path(record["workspace_id"]).write_text(json.dumps(record, indent=2))


def get_workspace(workspace_id: str) -> Optional[dict]:
    p = _ws_path(workspace_id)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def _lookup_by_index(index_name: str, key: str) -> Optional[dict]:
    idx_file = _index_dir(index_name) / _hash(key)
    if not idx_file.exists():
        return None
    return get_workspace(idx_file.read_text().strip())


def get_workspace_by_key(raw_key: str) -> Optional[dict]:
    return _lookup_by_index("by_key_hash", raw_key)


def get_workspace_by_google_sub(google_sub: str) -> Optional[dict]:
    return _lookup_by_index("by_google_sub", google_sub)


def get_workspace_by_wallet(wallet_address: str) -> Optional[dict]:
    return _lookup_by_index("by_wallet", wallet_address)


def create_workspace(*, owner_email: Optional[str] = None,
                      google_sub: Optional[str] = None,
                      wallet_address: Optional[str] = None) -> tuple[str, str]:
    """Idempotent per identity dimension: calling again with the same
    google_sub or wallet_address returns the SAME workspace (no duplicate
    minted), with a freshly generated key each time (old key invalidated —
    acceptable for now since there's no rotation UI yet; a repeat call is
    effectively a "lost my key" recovery path)."""
    if google_sub:
        existing = get_workspace_by_google_sub(google_sub)
        if existing:
            return existing["workspace_id"], _reissue_key(existing["workspace_id"], "by_google_sub", google_sub)
    if wallet_address:
        existing = get_workspace_by_wallet(wallet_address)
        if existing:
            return existing["workspace_id"], _reissue_key(existing["workspace_id"], "by_wallet", wallet_address)

    workspace_id = "ws_" + secrets.token_urlsafe(16)
    raw_key = "wk_live_" + secrets.token_urlsafe(32)
    pre_count = workspace_count()
    is_scarcity = pre_count < WORKSPACE_SCARCITY_CAP
    record = {
        "workspace_id": workspace_id,
        "owner_email": owner_email,
        "google_sub": google_sub,
        "wallet_address": wallet_address,
        "workspace_key_hash": _hash(raw_key),
        "stripe_customer_id": None,
        "plan": "pro" if is_scarcity else "free",
        "agent_cap": None if is_scarcity else WORKSPACE_FREE_AGENT_CAP,
        "created_at": time.time(),
        "pro_scarcity": is_scarcity,
    }
    _write_workspace(record)
    (_index_dir("by_key_hash") / _hash(raw_key)).write_text(workspace_id)
    if google_sub:
        (_index_dir("by_google_sub") / _hash(google_sub)).write_text(workspace_id)
    if wallet_address:
        (_index_dir("by_wallet") / _hash(wallet_address)).write_text(workspace_id)
    return workspace_id, raw_key


def _reissue_key(workspace_id: str, index_name: str, identity_value: str) -> str:
    raw_key = "wk_live_" + secrets.token_urlsafe(32)
    record = get_workspace(workspace_id)
    record["workspace_key_hash"] = _hash(raw_key)
    _write_workspace(record)
    (_index_dir("by_key_hash") / _hash(raw_key)).write_text(workspace_id)
    return raw_key


def mark_pro(workspace_id: str, stripe_customer_id: str) -> None:
    record = get_workspace(workspace_id)
    if record is None:
        raise WorkspaceError(f"workspace not found: {workspace_id}")
    record["plan"] = "pro"
    record["agent_cap"] = None
    record["stripe_customer_id"] = stripe_customer_id
    _write_workspace(record)


def is_workspace_pro(workspace_id: str) -> bool:
    record = get_workspace(workspace_id)
    return bool(record and record["plan"] == "pro")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/miniconda3/bin/python3 -m pytest tests/test_workspace_engine.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add workspace_engine.py tests/test_workspace_engine.py
git commit -m "feat: workspace engine — per-customer identity, replaces global pro.flag"
```

---

## Task 2: Claim-flow gating — workspace_key required on new claims

**Files:**
- Modify: `ledger_engine.py` (function `ensure_agent_secret`, ~line 195)
- Modify: `api_server.py` (`TrackRequest`/`BudgetRequest` models, `create_track`/`create_budget` handlers)
- Test: `tests/test_workspace_claim_gate.py`

**Interfaces:**
- Consumes: `workspace_engine.get_workspace_by_key(raw_key) -> dict | None`, `workspace_engine.WORKSPACE_FREE_AGENT_CAP`
- Produces: `ensure_agent_secret(agent_id, provided_secret=None, *, workspace_key=None, check_cap=True) -> tuple[str, bool]` (adds `workspace_key` kwarg; raises `WorkspaceKeyRequiredError` — new exception class — when a NEW claim has no valid workspace_key)
- Produces: agent records gain a sibling file `workspace_id.txt` next to `secret.txt` in `DATA_DIR/agents/{agent_id}/`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_workspace_claim_gate.py
import os, shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

AL_VERSION = "2026-09-01"


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, workspace_engine, api_server
    importlib.reload(workspace_engine)
    importlib.reload(ledger_engine)
    importlib.reload(api_server)
    from fastapi.testclient import TestClient
    yield TestClient(api_server.app), workspace_engine
    shutil.rmtree(tmp, ignore_errors=True)


def _headers():
    return {"AL-API-Version": AL_VERSION, "Content-Type": "application/json"}


def test_new_claim_without_workspace_key_is_rejected(client):
    c, ws = client
    r = c.post("/v1/track", headers=_headers(),
               json={"agent_id": "no-key-agent", "rail": "api_key",
                     "amount_cents": 100, "service": "s"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "workspace_key_required"


def test_new_claim_with_valid_workspace_key_succeeds(client):
    c, ws = client
    _, raw_key = ws.create_workspace(owner_email="a@example.com")
    r = c.post("/v1/track", headers=_headers(),
               json={"agent_id": "keyed-agent", "rail": "api_key",
                     "amount_cents": 100, "service": "s", "workspace_key": raw_key})
    assert r.status_code == 200
    assert "agent_secret" in r.json()


def test_existing_agent_write_needs_no_workspace_key(client):
    c, ws = client
    _, raw_key = ws.create_workspace(owner_email="a@example.com")
    r1 = c.post("/v1/track", headers=_headers(),
                json={"agent_id": "keyed-agent-2", "rail": "api_key",
                      "amount_cents": 100, "service": "s", "workspace_key": raw_key})
    secret = r1.json()["agent_secret"]
    r2 = c.post("/v1/track", headers=_headers(),
                json={"agent_id": "keyed-agent-2", "rail": "api_key",
                      "amount_cents": 50, "service": "s", "agent_secret": secret})
    assert r2.status_code == 200


def test_free_workspace_agent_cap_enforced(client):
    c, ws = client
    _, raw_key = ws.create_workspace(owner_email="a@example.com")
    for i in range(3):
        r = c.post("/v1/track", headers=_headers(),
                    json={"agent_id": f"cap-agent-{i}", "rail": "api_key",
                          "amount_cents": 1, "service": "s", "workspace_key": raw_key})
        assert r.status_code == 200
    r4 = c.post("/v1/track", headers=_headers(),
                json={"agent_id": "cap-agent-3", "rail": "api_key",
                      "amount_cents": 1, "service": "s", "workspace_key": raw_key})
    assert r4.status_code == 402
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/miniconda3/bin/python3 -m pytest tests/test_workspace_claim_gate.py -v`
Expected: FAIL (400/422 instead of the expected 401/200/402 — workspace_key not yet wired)

- [ ] **Step 3: Add `WorkspaceKeyRequiredError` and wire the gate in ledger_engine.py**

Add near the other error classes (~line 90, after `BetaCapExceededError`):

```python
class WorkspaceKeyRequiredError(LedgerError):
    """A brand-new agent_id claim did not present a valid workspace_key."""
```

Modify `ensure_agent_secret` signature and its claim branch (the `if path.exists()` block stays unchanged; only the NEW-claim branch below it changes):

```python
def ensure_agent_secret(agent_id: str, provided_secret: Optional[str] = None,
                         *, workspace_key: Optional[str] = None,
                         check_cap: bool = True) -> tuple[str, bool]:
    validate_agent_id(agent_id)
    path = _secret_path(agent_id)
    if path.exists():
        real = path.read_text().strip()
        if not provided_secret or not secrets.compare_digest(provided_secret, real):
            raise AuthError(
                f"agent_id '{agent_id}' is already claimed — pass its agent_secret "
                "(returned when the agent_id was first used) to write to it")
        return real, False

    # NEW claim from here — requires a valid workspace_key (D-<next>: workspace
    # identity, 2026-09-10). The site-wide BETA_AGENT_CAP/scarcity-by-agent-id
    # logic that used to live here is retired in favor of per-workspace caps.
    import workspace_engine
    workspace = workspace_engine.get_workspace_by_key(workspace_key) if workspace_key else None
    if workspace is None:
        raise WorkspaceKeyRequiredError(
            "a new agent_id requires a valid workspace_key — sign up at "
            "https://agent-ledger-production-0ff8.up.railway.app/login "
            "(or pay via x402 at /v1/billing/x402) to get one")

    agent_cap = workspace.get("agent_cap")
    if check_cap and agent_cap is not None:
        claimed_in_workspace = sum(
            1 for d in (DATA_DIR / "agents").glob("*")
            if d.is_dir() and (d / "workspace_id.txt").exists()
            and (d / "workspace_id.txt").read_text().strip() == workspace["workspace_id"])
        if claimed_in_workspace >= agent_cap:
            raise BetaCapExceededError(
                f"Free tier: {agent_cap} agents per workspace. Upgrade to Pro ($19/mo) "
                "for unlimited agents — https://buy.stripe.com/14AbJ0clUeoE9QN3Nl2400e")

    new_secret = secrets.token_urlsafe(24)
    _agent_dir(agent_id).mkdir(parents=True, exist_ok=True)
    path.write_text(new_secret)
    (_agent_dir(agent_id) / "workspace_id.txt").write_text(workspace["workspace_id"])
    return new_secret, True
```

Note: this replaces the D-818/D-819 scarcity-by-agent-id logic entirely
(scarcity now lives at the workspace layer, per the spec's section 5) —
delete the `inside_scarcity_window`/`pre_claim_count` block that previously
sat here, and delete the now-dead `SCARCITY_PRO_CAP`/`_set_pro_until`
per-agent scarcity path along with it (their tests in
`tests/test_scarcity_window.py` are superseded by this task's tests and
should be removed in this same commit — leaving them would test dead code).

- [ ] **Step 4: Wire `workspace_key` through the API layer**

In `api_server.py`, add `workspace_key: Optional[str] = None` to both
`TrackRequest` and `BudgetRequest` pydantic models, and pass it through in
`create_track`/`create_budget` wherever they call `ensure_agent_secret(...)`.
Catch `WorkspaceKeyRequiredError` alongside the existing `AuthError`/
`BetaCapExceededError` handling and translate to
`error_envelope(401, str(e), code="workspace_key_required")`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `/opt/miniconda3/bin/python3 -m pytest -q`
Expected: PASS, full suite (existing scarcity-by-agent-id tests removed per Step 3 note, replaced by this task's + Task 1's new tests)

- [ ] **Step 6: Commit**

```bash
git add ledger_engine.py api_server.py tests/test_workspace_claim_gate.py tests/test_scarcity_window.py
git commit -m "feat: require workspace_key on new agent claims, retire global scarcity-by-agent-id"
```

---

## Task 3: Workspace-scoped reads

**Files:**
- Modify: `api_server.py` (`get_report`, `get_report_html`, `get_alerts`, `get_tokens` if present)
- Test: `tests/test_workspace_read_scoping.py`

**Interfaces:**
- Consumes: `workspace_engine.get_workspace_by_key`, existing `agent_secret` file at `DATA_DIR/agents/{agent_id}/secret.txt`
- Produces: a shared dependency function `_authorize_agent_read(agent_id, request) -> None` (raises `HTTPException(401, ...)` if neither `X-Agent-Secret` nor `X-Workspace-Key` header matches)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_workspace_read_scoping.py
import os, shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest

AL_VERSION = "2026-09-01"


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, workspace_engine, api_server
    importlib.reload(workspace_engine)
    importlib.reload(ledger_engine)
    importlib.reload(api_server)
    from fastapi.testclient import TestClient
    tc = TestClient(api_server.app)
    _, raw_key = workspace_engine.create_workspace(owner_email="a@example.com")
    r = tc.post("/v1/track", headers={"AL-API-Version": AL_VERSION},
                json={"agent_id": "read-scope-agent", "rail": "api_key",
                      "amount_cents": 100, "service": "s", "workspace_key": raw_key})
    secret = r.json()["agent_secret"]
    yield tc, raw_key, secret
    shutil.rmtree(tmp, ignore_errors=True)


def test_report_requires_a_credential(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/report/read-scope-agent")
    assert r.status_code == 401


def test_report_accepts_agent_secret(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/report/read-scope-agent", headers={"X-Agent-Secret": secret})
    assert r.status_code == 200


def test_report_accepts_workspace_key(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/report/read-scope-agent", headers={"X-Workspace-Key": raw_key})
    assert r.status_code == 200


def test_report_rejects_wrong_credential(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/report/read-scope-agent", headers={"X-Agent-Secret": "wrong"})
    assert r.status_code == 401


def test_html_report_also_scoped(client):
    tc, raw_key, secret = client
    r = tc.get("/v1/report/read-scope-agent/html")
    assert r.status_code == 401
    r2 = tc.get("/v1/report/read-scope-agent/html", headers={"X-Agent-Secret": secret})
    assert r2.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/miniconda3/bin/python3 -m pytest tests/test_workspace_read_scoping.py -v`
Expected: FAIL (currently 200 with no headers — open reads today)

- [ ] **Step 3: Implement the shared authorization check**

Add to `api_server.py`, near the other helper functions:

```python
def _authorize_agent_read(agent_id: str, request: Request) -> None:
    """Reads used to be fully open (pre-workspace design). Now require
    either the agent's own secret or its workspace's key — either proves
    the caller has a legitimate claim to this agent_id's data."""
    from ledger_engine import _secret_path
    import workspace_engine
    agent_secret_header = request.headers.get("x-agent-secret", "")
    workspace_key_header = request.headers.get("x-workspace-key", "")
    secret_path = _secret_path(agent_id)
    if agent_secret_header and secret_path.exists():
        import secrets as _secrets
        if _secrets.compare_digest(agent_secret_header, secret_path.read_text().strip()):
            return
    if workspace_key_header:
        ws_dir = DATA_DIR / "agents" / agent_id / "workspace_id.txt"
        workspace = workspace_engine.get_workspace_by_key(workspace_key_header)
        if workspace and ws_dir.exists() and ws_dir.read_text().strip() == workspace["workspace_id"]:
            return
    raise HTTPException(401, "this agent's data requires its agent_secret or workspace_key")
```

Add `request: Request` as a parameter to `get_report`, `get_report_html`,
and `get_alerts`, and call `_authorize_agent_read(agent_id, request)` as
the first line of each (after `validate_agent_id` where present).

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/miniconda3/bin/python3 -m pytest -q`
Expected: PASS, full suite

- [ ] **Step 5: Update README/status.html/llms.txt examples to include the new required header on read calls** (they currently show headerless reads — this is the same class of doc-drift bug found earlier this session, don't reintroduce it)

- [ ] **Step 6: Commit**

```bash
git add api_server.py tests/test_workspace_read_scoping.py README.md status.html
git commit -m "feat: scope agent reads to workspace/agent ownership (breaking change, see spec 3c)"
```

---

## Task 4: Google OAuth + session dashboard (parallelizable with Tasks 5, 6)

**Files:**
- Create: `oauth_google.py`
- Create: `session_auth.py`
- Modify: `api_server.py` (routes: `/login`, `/auth/google/callback`, `/logout`, `/dashboard`)
- Test: `tests/test_oauth_session.py`

**Interfaces:**
- Consumes: `workspace_engine.create_workspace(google_sub=...)`, `workspace_engine.get_workspace_by_google_sub`
- Produces: `oauth_google.google_auth_url(state: str) -> str`, `oauth_google.exchange_code(code: str) -> dict` (returns `{"google_sub": str, "email": str}`)
- Produces: `session_auth.sign_session(workspace_id: str) -> str`, `session_auth.verify_session(cookie_value: str) -> str | None` (returns workspace_id or None)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_oauth_session.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os


def test_session_roundtrip(monkeypatch):
    monkeypatch.setenv("AL_SESSION_SECRET", "test-secret-for-signing")
    import importlib, session_auth
    importlib.reload(session_auth)
    token = session_auth.sign_session("ws_abc123")
    assert session_auth.verify_session(token) == "ws_abc123"


def test_tampered_session_rejected(monkeypatch):
    monkeypatch.setenv("AL_SESSION_SECRET", "test-secret-for-signing")
    import importlib, session_auth
    importlib.reload(session_auth)
    token = session_auth.sign_session("ws_abc123")
    tampered = token[:-4] + "xxxx"
    assert session_auth.verify_session(tampered) is None


def test_google_auth_url_contains_client_id(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    import importlib, oauth_google
    importlib.reload(oauth_google)
    url = oauth_google.google_auth_url("state123")
    assert "test-client-id" in url
    assert "state123" in url
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/miniconda3/bin/python3 -m pytest tests/test_oauth_session.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Implement session_auth.py**

```python
#!/usr/bin/env python3
"""Signed session cookies — HMAC-SHA256, stdlib only (no itsdangerous
dependency). Format: "<workspace_id>.<hex signature>"."""
import hmac
import hashlib
import os


def _secret() -> bytes:
    s = os.environ.get("AL_SESSION_SECRET", "")
    if not s:
        raise RuntimeError("AL_SESSION_SECRET not configured — refusing to sign sessions")
    return s.encode()


def sign_session(workspace_id: str) -> str:
    sig = hmac.new(_secret(), workspace_id.encode(), hashlib.sha256).hexdigest()
    return f"{workspace_id}.{sig}"


def verify_session(cookie_value: str) -> str | None:
    if not cookie_value or "." not in cookie_value:
        return None
    workspace_id, _, sig = cookie_value.rpartition(".")
    expected = hmac.new(_secret(), workspace_id.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(sig, expected):
        return workspace_id
    return None
```

- [ ] **Step 4: Implement oauth_google.py**

```python
#!/usr/bin/env python3
"""Google OAuth — plain REST calls, no SDK. Confirm the exact token/userinfo
endpoint shapes against Google's current OAuth2 docs at implementation time
(https://developers.google.com/identity/protocols/oauth2/web-server) —
these are the stable, long-standing endpoints but must be verified live,
not assumed from memory, per this project's no-guess-presented-as-proof
standard."""
import json
import os
import urllib.parse
import urllib.request

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"


def google_auth_url(state: str) -> str:
    client_id = os.environ["GOOGLE_OAUTH_CLIENT_ID"]
    redirect_uri = os.environ.get(
        "GOOGLE_OAUTH_REDIRECT_URI",
        "https://agent-ledger-production-0ff8.up.railway.app/auth/google/callback")
    params = {
        "client_id": client_id, "redirect_uri": redirect_uri,
        "response_type": "code", "scope": "openid email",
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str) -> dict:
    client_id = os.environ["GOOGLE_OAUTH_CLIENT_ID"]
    client_secret = os.environ["GOOGLE_OAUTH_CLIENT_SECRET"]
    redirect_uri = os.environ.get(
        "GOOGLE_OAUTH_REDIRECT_URI",
        "https://agent-ledger-production-0ff8.up.railway.app/auth/google/callback")
    body = urllib.parse.urlencode({
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_ENDPOINT, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        token_data = json.loads(resp.read())
    userinfo_req = urllib.request.Request(
        USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {token_data['access_token']}"})
    with urllib.request.urlopen(userinfo_req, timeout=10) as resp:
        userinfo = json.loads(resp.read())
    return {"google_sub": userinfo["sub"], "email": userinfo.get("email", "")}
```

- [ ] **Step 5: Add the routes to api_server.py**

```python
import secrets as _secrets_mod

@app.get("/login")
def login():
    state = _secrets_mod.token_urlsafe(16)
    from oauth_google import google_auth_url
    from fastapi.responses import RedirectResponse
    resp = RedirectResponse(google_auth_url(state))
    resp.set_cookie("al_oauth_state", state, httponly=True, max_age=600)
    return resp

@app.get("/auth/google/callback")
def google_callback(code: str, state: str, request: Request):
    from fastapi.responses import RedirectResponse
    if request.cookies.get("al_oauth_state") != state:
        raise HTTPException(400, "invalid oauth state")
    from oauth_google import exchange_code
    import workspace_engine
    userinfo = exchange_code(code)
    workspace = workspace_engine.get_workspace_by_google_sub(userinfo["google_sub"])
    if workspace is None:
        workspace_id, _ = workspace_engine.create_workspace(
            owner_email=userinfo["email"], google_sub=userinfo["google_sub"])
    else:
        workspace_id = workspace["workspace_id"]
    from session_auth import sign_session
    resp = RedirectResponse("/dashboard")
    resp.set_cookie("al_session", sign_session(workspace_id), httponly=True, max_age=2592000)
    # One-time key reveal: the raw key only exists right here (create_workspace
    # returns it once, then only its hash is ever stored). Carry it to the
    # dashboard's first load via a short-lived cookie the dashboard route
    # reads-and-deletes, so a page refresh never shows it twice.
    resp.set_cookie("al_key_reveal", raw_key, httponly=True, max_age=30)
    resp.delete_cookie("al_oauth_state")
    return resp

@app.get("/logout")
def logout():
    from fastapi.responses import RedirectResponse
    resp = RedirectResponse("/")
    resp.delete_cookie("al_session")
    return resp

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request):
    from session_auth import verify_session
    import workspace_engine
    workspace_id = verify_session(request.cookies.get("al_session", ""))
    if not workspace_id:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/login")
    ws = workspace_engine.get_workspace(workspace_id)
    reveal_key = request.cookies.get("al_key_reveal")
    key_html = (
        f'<p style="color:#d4af37"><b>Your workspace_key (save this now — shown once):</b><br>'
        f'<code>{html.escape(reveal_key)}</code></p>'
        if reveal_key else
        '<p>Workspace key already shown once. <a href="/login">Log in again</a> to reissue '
        'a new one if you lost it (this invalidates the old key).</p>'
    )
    agent_rows = "".join(
        f'<li><a href="/v1/report/{html.escape(d.name)}/html">{html.escape(d.name)}</a></li>'
        for d in (DATA_DIR / "agents").glob("*")
        if (d / "workspace_id.txt").exists()
        and (d / "workspace_id.txt").read_text().strip() == workspace_id
    ) or "<li>No agents claimed yet.</li>"
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>AgentLedger — Your Workspace</title></head><body style="font-family:sans-serif;padding:24px">
<h1>Your workspace</h1>
<p>Plan: <b>{html.escape(ws['plan'])}</b></p>
{key_html}
<h3>Your agents</h3><ul>{agent_rows}</ul>
<p><a href="/logout">Log out</a></p>
</body></html>"""
    resp = HTMLResponse(page)
    if reveal_key:
        resp.delete_cookie("al_key_reveal")
    return resp
```

The reveal cookie is `httponly` and 30-second-lived — long enough for the
redirect-and-render round trip, short enough that it can't be replayed
later even if something in the browser cached the URL/response.

- [ ] **Step 6: Run tests to verify they pass**

Run: `/opt/miniconda3/bin/python3 -m pytest -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add oauth_google.py session_auth.py api_server.py tests/test_oauth_session.py
git commit -m "feat: Google OAuth login + session-based workspace dashboard"
```

---

## Task 5: Per-workspace Stripe billing (parallelizable with Tasks 4, 6)

**Files:**
- Modify: `api_server.py` (`stripe_webhook`, new `POST /v1/billing/checkout`)
- Test: `tests/test_workspace_billing.py`

**Interfaces:**
- Consumes: `workspace_engine.mark_pro(workspace_id, stripe_customer_id)`, `session_auth.verify_session`
- Produces: `POST /v1/billing/checkout` (session-protected) → `{"checkout_url": str}`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_workspace_billing.py
import hashlib, hmac, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os, shutil, tempfile
import pytest


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET_AL", "whsec_test")
    import importlib
    import ledger_engine, workspace_engine, api_server
    importlib.reload(workspace_engine)
    importlib.reload(ledger_engine)
    importlib.reload(api_server)
    from fastapi.testclient import TestClient
    yield TestClient(api_server.app), workspace_engine
    shutil.rmtree(tmp, ignore_errors=True)


def _signed_webhook_body(payload: dict, secret: str) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    t = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={t},v1={sig}"


def test_completed_checkout_marks_correct_workspace_pro(client):
    c, ws = client
    workspace_id, _ = ws.create_workspace(owner_email="a@example.com")
    payload = {
        "type": "checkout.session.completed",
        "data": {"object": {
            "customer_details": {"email": "a@example.com"},
            "amount_total": 1900,
            "id": "cs_test_1",
            "client_reference_id": workspace_id,
            "customer": "cus_test_1",
        }},
    }
    body, sig = _signed_webhook_body(payload, "whsec_test")
    r = c.post("/stripe/webhook", content=body, headers={"stripe-signature": sig})
    assert r.status_code == 200
    assert ws.is_workspace_pro(workspace_id) is True


def test_completed_checkout_does_not_mark_other_workspaces_pro(client):
    c, ws = client
    workspace_a, _ = ws.create_workspace(owner_email="a@example.com")
    workspace_b, _ = ws.create_workspace(owner_email="b@example.com")
    payload = {
        "type": "checkout.session.completed",
        "data": {"object": {
            "customer_details": {"email": "a@example.com"},
            "amount_total": 1900, "id": "cs_test_2",
            "client_reference_id": workspace_a, "customer": "cus_test_2",
        }},
    }
    body, sig = _signed_webhook_body(payload, "whsec_test")
    c.post("/stripe/webhook", content=body, headers={"stripe-signature": sig})
    assert ws.is_workspace_pro(workspace_a) is True
    assert ws.is_workspace_pro(workspace_b) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/miniconda3/bin/python3 -m pytest tests/test_workspace_billing.py -v`
Expected: FAIL (webhook currently calls global `activate_pro()`, not per-workspace)

- [ ] **Step 3: Rewire the webhook handler**

In `api_server.py`'s `stripe_webhook`, replace the `if plan == "pro": from ledger_engine import activate_pro; activate_pro()` block with:

```python
    if plan == "pro":
        workspace_id = sess.get("client_reference_id")
        if workspace_id:
            import workspace_engine
            try:
                workspace_engine.mark_pro(workspace_id, sess.get("customer", ""))
            except workspace_engine.WorkspaceError:
                pass  # unknown workspace_id — log for investigation, don't crash the webhook
```

(Leave `activate_pro`/`pro_active` in `ledger_engine.py` alone — Task 2
already stopped calling them from the claim path; they become unused
dead code here, note for cleanup but don't delete in this task to keep
the diff focused.)

- [ ] **Step 4: Add the checkout-session-creation endpoint**

```python
@app.post("/v1/billing/checkout")
def create_checkout(request: Request):
    from session_auth import verify_session
    workspace_id = verify_session(request.cookies.get("al_session", ""))
    if not workspace_id:
        raise HTTPException(401, "log in first")
    # NOTE: confirm the exact Stripe Checkout Sessions API request shape
    # (https://docs.stripe.com/api/checkout/sessions/create) at
    # implementation time rather than assuming from memory — this call
    # needs STRIPE_API_KEY (already in x_api_creds.env as STRIPE_SECRET_KEY,
    # GasPermit account) and the existing STRIPE_PRO_PRICE_ID-equivalent
    # for AgentLedger's $19/mo price.
    import os, urllib.parse, urllib.request
    body = urllib.parse.urlencode({
        "mode": "subscription",
        "line_items[0][price]": os.environ["AL_STRIPE_PRICE_ID"],
        "line_items[0][quantity]": "1",
        "client_reference_id": workspace_id,
        "success_url": "https://agent-ledger-production-0ff8.up.railway.app/dashboard",
        "cancel_url": "https://agent-ledger-production-0ff8.up.railway.app/dashboard",
    }).encode()
    req = urllib.request.Request(
        "https://api.stripe.com/v1/checkout/sessions", data=body, method="POST",
        headers={"Authorization": f"Bearer {os.environ['STRIPE_API_KEY']}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        session = json.loads(resp.read())
    return {"checkout_url": session["url"]}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `/opt/miniconda3/bin/python3 -m pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add api_server.py tests/test_workspace_billing.py
git commit -m "feat: per-workspace Stripe billing, replaces global pro.flag webhook"
```

---

## Task 6: x402 self-serve workspace minting (parallelizable with Tasks 4, 5)

**Files:**
- Create: `x402_verify.py`
- Modify: `api_server.py` (`POST /v1/billing/x402`)
- Test: `tests/test_x402_billing.py`

**Interfaces:**
- Produces: `x402_verify.verify_payment(payment_header: str) -> dict` (returns `{"verified": bool, "payer_wallet": str, "tx_hash": str}`)
- Produces: `POST /v1/billing/x402` → `{"workspace_id": str, "workspace_key": str}` on success

**IMPORTANT — confirm before implementing:** the exact x402 facilitator
request/response schema (Coinbase's reference facilitator) must be pulled
from current docs at implementation time — do not guess the shape from
memory. The task below implements the verification as an isolated,
mockable function specifically so the real facilitator integration can
be dropped in without touching the endpoint or its tests.

- [ ] **Step 1: Write the failing tests (against a mocked verifier)**

```python
# tests/test_x402_billing.py
import shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import importlib
    import ledger_engine, workspace_engine, api_server
    importlib.reload(workspace_engine)
    importlib.reload(ledger_engine)
    importlib.reload(api_server)
    from fastapi.testclient import TestClient
    yield TestClient(api_server.app), api_server
    shutil.rmtree(tmp, ignore_errors=True)


def test_verified_payment_mints_workspace(client, monkeypatch):
    c, api_server = client
    monkeypatch.setattr(
        "x402_verify.verify_payment",
        lambda header: {"verified": True, "payer_wallet": "0xABC", "tx_hash": "0xTX1"})
    r = c.post("/v1/billing/x402", headers={"X-PAYMENT": "fake-payment-header"})
    assert r.status_code == 200
    assert "workspace_key" in r.json()


def test_unverified_payment_rejected(client, monkeypatch):
    c, api_server = client
    monkeypatch.setattr(
        "x402_verify.verify_payment",
        lambda header: {"verified": False, "payer_wallet": None, "tx_hash": None})
    r = c.post("/v1/billing/x402", headers={"X-PAYMENT": "bad"})
    assert r.status_code == 402


def test_same_wallet_twice_returns_same_workspace(client, monkeypatch):
    c, api_server = client
    monkeypatch.setattr(
        "x402_verify.verify_payment",
        lambda header: {"verified": True, "payer_wallet": "0xDEF", "tx_hash": "0xTX2"})
    r1 = c.post("/v1/billing/x402", headers={"X-PAYMENT": "fake"})
    r2 = c.post("/v1/billing/x402", headers={"X-PAYMENT": "fake-again"})
    assert r1.json()["workspace_id"] == r2.json()["workspace_id"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/miniconda3/bin/python3 -m pytest tests/test_x402_billing.py -v`
Expected: FAIL, `ModuleNotFoundError` / 404 on the route

- [ ] **Step 3: Implement x402_verify.py (isolated, real facilitator call)**

```python
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
    }
```

- [ ] **Step 4: Add the endpoint**

```python
@app.post("/v1/billing/x402")
def x402_billing(request: Request):
    payment_header = request.headers.get("x-payment", "")
    if not payment_header:
        raise HTTPException(402, "X-PAYMENT header required")
    import x402_verify, workspace_engine
    result = x402_verify.verify_payment(payment_header)
    if not result["verified"]:
        raise HTTPException(402, "payment not verified")
    workspace_id, raw_key = workspace_engine.create_workspace(wallet_address=result["payer_wallet"])
    return {"workspace_id": workspace_id, "workspace_key": raw_key}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `/opt/miniconda3/bin/python3 -m pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add x402_verify.py api_server.py tests/test_x402_billing.py
git commit -m "feat: x402 self-serve workspace minting — no human/OAuth required"
```

---

## Task 7: Migration — assign existing real agents to a default workspace

**Files:**
- Create: `scripts/migrate_agents_to_workspace.py`
- Test: manual dry-run against production data (not unit-testable — operates on real Railway volume)

**Interfaces:**
- Consumes: `workspace_engine.create_workspace`, existing `DATA_DIR/agents/*/secret.txt`

- [ ] **Step 1: Write the migration script**

```python
#!/opt/miniconda3/bin/python3
"""One-time: assign every currently-claimed agent_id (pre-workspace era)
to a single default workspace, so nothing breaks after Tasks 1-3 deploy.
Run with --dry-run first. Idempotent: re-running skips agents that
already have a workspace_id.txt."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import workspace_engine

DATA_DIR = workspace_engine.DATA_DIR


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-email", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    agents_dir = DATA_DIR / "agents"
    to_migrate = [
        d for d in agents_dir.glob("*")
        if d.is_dir() and (d / "secret.txt").exists()
        and not (d / "workspace_id.txt").exists()
    ]
    print(f"Found {len(to_migrate)} agent(s) needing migration: {[d.name for d in to_migrate]}")
    if args.dry_run:
        print("--dry-run: no changes made")
        return

    workspace_id, raw_key = workspace_engine.create_workspace(owner_email=args.owner_email)
    print(f"Created default workspace {workspace_id} (key shown once): {raw_key}")
    for d in to_migrate:
        (d / "workspace_id.txt").write_text(workspace_id)
        print(f"  migrated {d.name}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Dry-run against production data** (via `railway ssh`, per the existing deploy.sh/dogfood pattern — read-only, confirms the 4 expected agents are listed)

Run: `railway ssh -- python3 scripts/migrate_agents_to_workspace.py --owner-email you@example.com --dry-run`
Expected: lists `vega-trading-desk`, `verify-agent-1`, `hermes-fleet-dogfood`, `abhishek-command-code`

- [ ] **Step 3: Run for real, save the printed workspace_key immediately** (shown once, per the same pattern as `agent_secret` today)

Run: `railway ssh -- python3 scripts/migrate_agents_to_workspace.py --owner-email you@example.com`

- [ ] **Step 4: Commit the script (not the run output/key)**

```bash
git add scripts/migrate_agents_to_workspace.py
git commit -m "chore: one-time migration script, existing agents to default workspace"
```

---

## Final Integration Check (after all tasks land)

- [ ] Run `/opt/miniconda3/bin/python3 -m pytest -q` — full suite green
- [ ] Deploy via `./deploy.sh` (register `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `AL_SESSION_SECRET`, `AL_STRIPE_PRICE_ID`, `X402_FACILITATOR_URL` as Railway env vars first — none of this works without them)
- [ ] Run the migration script (Task 7) against production before or immediately after this deploy — existing agents lose write access otherwise (their `workspace_id.txt` is missing, but their `agent_secret` writes don't check for it, so this is a soft requirement for dashboard visibility, not a hard outage — confirm this during the red-team pass on Task 2)
- [ ] Smoke test all four claim paths live: existing agent write (unaffected), new claim with no key (401), new claim with a freshly-signed-up Google workspace key (200), x402 mint (200)
