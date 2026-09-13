# tests/test_cli_init.py
"""BUILD-5 — `agent-ledger init` and `share`.

The promise is "zero to a metered, guarded agent in under two minutes using
only the CLI". That is a claim about a whole journey, so the test runs the whole
journey: a real uvicorn instance on a free port, the real CLI over real HTTP,
then the artifacts it produced are used for real.

Deliberately NOT against production: `init` mints a workspace and consumes a
launch-window slot. Testing it on the live instance would add exactly the junk
data that had to be purged from there earlier the same day.
"""
import importlib
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="agent-ledger-cli-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import ledger_engine, workspace_engine, identity, alert_delivery
    import proxy, routes_agents, routes_proxy, api_server
    for module in (proxy, alert_delivery, ledger_engine, workspace_engine, identity,
                   routes_agents, routes_proxy, api_server):
        importlib.reload(module)

    import uvicorn
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(api_server.app, host="127.0.0.1",
                                           port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=2):
                break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError("local instance never became healthy")
    yield base, Path(tmp)
    server.should_exit = True
    shutil.rmtree(tmp, ignore_errors=True)


def _run_cli(argv):
    import cli
    importlib.reload(cli)
    old = sys.argv
    sys.argv = ["agentledger"] + argv
    try:
        cli.main()
    finally:
        sys.argv = old


# ── init ──────────────────────────────────────────────────────────────────

def test_init_writes_a_usable_env_file(live, tmp_path):
    base, _ = live
    env_file = tmp_path / ".env"
    _run_cli(["--api-base", base, "init", "--agent", "cli-agent",
              "--env-file", str(env_file)])

    assert env_file.exists(), "init did not write an env file"
    text = env_file.read_text()
    for expected in ("AGENT_LEDGER_API_BASE=", "AGENT_LEDGER_WORKSPACE_KEY=wk_live_",
                     "AGENT_LEDGER_AGENT_ID=cli-agent", "AGENT_LEDGER_AGENT_SECRET="):
        assert expected in text, f"missing {expected!r} in the env file"

    # The credentials it produced must actually work.
    values = dict(line.split("=", 1) for line in text.splitlines()
                  if "=" in line and not line.startswith("#"))
    secret = values["AGENT_LEDGER_AGENT_SECRET"]
    req = urllib.request.Request(f"{base}/v1/report/cli-agent",
                                 headers={"X-Agent-Secret": secret})
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200


def test_init_does_not_leave_the_secret_out_of_the_printout(live, tmp_path, capsys):
    """The secret is shown once. A CLI that mints one and does not show it has
    created a credential nobody can use."""
    base, _ = live
    _run_cli(["--api-base", base, "init", "--agent", "shown-agent",
              "--env-file", str(tmp_path / ".env")])
    out = capsys.readouterr().out
    assert "agent_secret" in out.lower() or "secret" in out.lower()
    assert "X-AL-Agent" in out, "the next step was not spelled out"


def test_init_appends_rather_than_clobbering_an_existing_env(live, tmp_path):
    base, _ = live
    env_file = tmp_path / ".env"
    env_file.write_text("SOMETHING_ELSE=keep-me\n")
    _run_cli(["--api-base", base, "init", "--agent", "append-agent",
              "--env-file", str(env_file)])
    text = env_file.read_text()
    assert "SOMETHING_ELSE=keep-me" in text
    assert "AGENT_LEDGER_AGENT_SECRET=" in text


def test_a_second_init_for_the_same_agent_asks_for_the_secret(live, tmp_path):
    """Claiming an existing agent_id needs its secret — init must say so rather
    than minting a duplicate or failing opaquely."""
    base, _ = live
    _run_cli(["--api-base", base, "init", "--agent", "twice-agent",
              "--env-file", str(tmp_path / "a.env")])
    with pytest.raises(SystemExit) as e:
        _run_cli(["--api-base", base, "init", "--agent", "twice-agent",
                  "--env-file", str(tmp_path / "b.env")])
    assert e.value.code == 1
    assert not (tmp_path / "b.env").exists(), "a failed init still wrote a file"


# ── share ─────────────────────────────────────────────────────────────────

def test_share_prints_a_url_a_browser_can_open(live, tmp_path):
    base, _ = live
    env_file = tmp_path / ".env"
    _run_cli(["--api-base", base, "init", "--agent", "share-cli",
              "--env-file", str(env_file)])
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines()
                  if "=" in line and not line.startswith("#"))
    secret = values["AGENT_LEDGER_AGENT_SECRET"]

    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _run_cli(["--api-base", base, "share", "--agent-id", "share-cli",
                  "--agent-secret", secret])
    url = buf.getvalue().strip().splitlines()[0]
    assert url.startswith("http") and "?t=" in url

    # the whole point: no headers, no credential, still renders
    with urllib.request.urlopen(url, timeout=5) as resp:
        body = resp.read().decode()
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/html")
        assert "share-cli" in body


def test_share_without_a_credential_is_refused(live):
    base, _ = live
    with pytest.raises(SystemExit) as e:
        _run_cli(["--api-base", base, "share", "--agent-id", "x"])
    assert e.value.code == 1


def test_share_uses_the_workspace_key_too(live, tmp_path):
    base, _ = live
    env_file = tmp_path / ".env"
    _run_cli(["--api-base", base, "init", "--agent", "ws-share",
              "--env-file", str(env_file)])
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines()
                  if "=" in line and not line.startswith("#"))
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _run_cli(["--api-base", base, "share", "--agent-id", "ws-share",
                  "--workspace-key", values["AGENT_LEDGER_WORKSPACE_KEY"]])
    assert "?t=" in buf.getvalue()
