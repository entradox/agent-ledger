
"""Does a REFUSED /start?plan=starter still create a workspace?

The 409 text and the in-code comment both claim the refusal happens BEFORE
minting, so no orphan workspace is created. create_workspace() is called
ABOVE the guard in api_server.py — this test measures which is true instead
of trusting the comment.
"""
import os, sys, tempfile
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient
import api_server, workspace_engine

def test_refused_plan_start_creates_no_orphan_workspace():
    c = TestClient(api_server.app)
    before = workspace_engine.workspace_count()
    r = c.post("/start?plan=starter", headers={"Accept": "application/json"})
    after = workspace_engine.workspace_count()
    print(f"status={r.status_code}  workspaces {before} -> {after}")
    assert r.status_code == 409, r.status_code
    assert after == before, f"REFUSED REQUEST STILL MINTED {after-before} WORKSPACE(S)"

def test_refused_plan_start_does_not_burn_a_daily_mint():
    c = TestClient(api_server.app)
    api_server._START_MINTS.clear()
    c.post("/start?plan=starter", headers={"Accept": "application/json"})
    used = len(api_server._START_MINTS.get("testclient", []))
    print(f"daily mint budget consumed by a REFUSED request: {used} of 3")
    assert used == 0, f"refusal consumed {used} of 3 daily mints"
