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
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
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


# ── the client-only package ───────────────────────────────────────────────

@pytest.mark.parametrize("argv", [
    ["track", "--agent-id", "x", "--rail", "manual", "--amount-cents", "1", "--service", "s"],
    ["set-budget", "--agent-id", "x", "--monthly-cents", "100"],
    ["report", "--agent-id", "x"],
    ["alerts", "--agent-id", "x"],
    ["list"],
])
def test_every_local_command_explains_itself_without_the_engine(argv, monkeypatch, capsys):
    """The client package ships the CLI, not the service.

    Found by actually installing the package and running it: `report` crashed
    with a raw ModuleNotFoundError while `track` printed a helpful message,
    because the guard had been added to one command instead of to the single
    place local mode begins. This walks EVERY local command.
    """
    monkeypatch.setitem(sys.modules, "ledger_engine", None)   # import raises
    with pytest.raises(SystemExit) as e:
        _run_cli(argv)
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "--api-base" in err, "the user was not told how to reach a real instance"
    assert "Traceback" not in err


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


# ── our published instructions must actually work (D-1382) ────────────────
# These exist because the product shipped a copy-paste command that returned
# 401 to every customer but the first. The gap was structural: every existing
# test walked the AGENT path (discovery -> MCP -> tools -> paywall) and none
# executed the HUMAN onboarding example. So the suite was green while the
# documented first step was broken.

def test_two_customers_can_both_follow_the_documented_first_step(live):
    """The one that would have caught it — and the shape matters.

    A single customer cannot reproduce the defect: whoever pastes first
    succeeds, whatever id the docs use. It only breaks for the SECOND customer,
    who follows the identical published instruction and is told the agent is
    already claimed. So this mints two workspaces and executes the command each
    customer is actually shown, verbatim.

    `agent_id` is a GLOBAL namespace. That means the invariant is not "the docs
    contain a valid id" — it is "the id handed to customer N is not the one
    handed to customer N-1". Any fixed constant fails the second assert.
    """
    base, _ = live

    def mint_and_read_the_published_command(name):
        req = urllib.request.Request(f"{base}/start", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            page = resp.read().decode()
        agent_id = re.search(r'"agent_id":"([^"]+)"', page)
        key = re.search(r"wk_live_[A-Za-z0-9_\-]+", page)
        assert agent_id and key, f"{name}: the /start page no longer shows a paste-ready command"
        return agent_id.group(1), key.group(0)

    def paste_the_command(agent_id, workspace_key):
        body = json.dumps({"agent_id": agent_id, "rail": "manual", "amount_cents": 100,
                           "service": "test", "workspace_key": workspace_key}).encode()
        req = urllib.request.Request(
            f"{base}/v1/track", data=body, method="POST",
            headers={"Content-Type": "application/json", "AL-API-Version": "2026-09-01"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    first_id, first_key = mint_and_read_the_published_command("customer 1")
    second_id, second_key = mint_and_read_the_published_command("customer 2")

    assert first_id != second_id, (
        "both customers were handed the same agent_id — agent_id is a GLOBAL "
        "namespace, so customer 2's first instruction must fail")

    assert paste_the_command(first_id, first_key) == 200, "customer 1 was blocked"
    assert paste_the_command(second_id, second_key) == 200, (
        "customer 2 was blocked. This is the live defect: our published first "
        "step used a fixed agent_id, which the first customer to paste it "
        "claimed permanently, so every later customer got 401 "
        "agent_secret_mismatch on the very first instruction.")


def test_init_needs_no_typed_agent_id(live, tmp_path):
    """The documented one-liner is `agent-ledger init` with nothing after it.

    Requiring --agent made the zero-friction path need a typed value, and
    whatever the customer invents is theirs alone — which is why the default is
    generated rather than a published constant.
    """
    base, _ = live
    env_file = tmp_path / ".env"
    _run_cli(["--api-base", base, "init", "--env-file", str(env_file)])
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines()
                  if "=" in line and not line.startswith("#"))
    agent_id = values["AGENT_LEDGER_AGENT_ID"]
    assert agent_id, "init minted no agent_id"
    assert agent_id.islower() and " " not in agent_id
    req = urllib.request.Request(f"{base}/v1/report/{agent_id}",
                                 headers={"X-Agent-Secret": values["AGENT_LEDGER_AGENT_SECRET"]})
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200


def test_no_published_surface_hands_out_a_claimable_constant():
    """A repo-wide guard, because the same string lived in eight files.

    Fixing one surface is how this class of defect comes back: the constant was
    in /start, the CLI help, README, npm/README, status.html, the OpenAPI
    x-guidance, the MCP docs and the onboarding email. Any one left behind
    re-breaks the funnel for the customer who reads that surface.

    Note what is NOT asserted: a surface need not contain a literal placeholder
    token. `REST_ENDPOINTS_MD` legitimately writes `{agent_id}` in an endpoint
    table, so requiring a specific placeholder string here would fail correct
    content. The invariant is narrower and real: no shipped surface may publish
    a constant that a customer could claim out from under the next one.
    """
    import docs_content
    import api_server
    surfaces = {
        "docs_content.QUICKSTART_MD": docs_content.QUICKSTART_MD,
        "docs_content.REST_ENDPOINTS_MD": docs_content.REST_ENDPOINTS_MD,
        "docs_content.MCP_TOOLS_MD": docs_content.MCP_TOOLS_MD,
        "api_server.LLMS_TXT": api_server.LLMS_TXT,
    }
    for name, text in surfaces.items():
        assert "my-agent" not in text, (
            f"{name} still hands out 'my-agent' — a GLOBAL agent_id already "
            f"claimed by our own init probe, so it 401s for every customer")

    # The copy-paste example is the one that actually gets pasted, so it must
    # show the customer something to substitute rather than a fixed value.
    quickstart = docs_content.QUICKSTART_MD
    assert "YOUR_AGENT_ID" in quickstart, (
        "the quickstart curl no longer shows a placeholder agent_id — the "
        "customer has nothing to substitute and will paste a shared id")

