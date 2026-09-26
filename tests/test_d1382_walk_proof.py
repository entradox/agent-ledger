"""D-1382 END-TO-END WALK — the exact customer journey, against a live server.

Why this exists as its own file: `test_two_customers_can_both_follow_the_documented_first_step`
already pins the two-customer case. This one does not add coverage — it produces the
ARTIFACT the deploy gate asks for: the literal stdout of two independent customers
following the published first instruction, from mint to first tracked write, on the
MERGED tree (D-1382 fix + everything already in origin/main).

It runs the real server on a free port with an isolated data dir, mints two workspaces
through POST /start the way a browser does, extracts the copy-paste curl from each
success screen the way a customer would, substitutes their own key, and executes it
verbatim. Both must reach 200 and return an agent_secret.

Not against production: minting consumes launch-window slots and leaves junk rows.
"""
import importlib
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
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
    tmp = tempfile.mkdtemp(prefix="agent-ledger-walk-")
    monkeypatch.setenv("AGENT_LEDGER_DATA", tmp)
    import ledger_engine, workspace_engine, identity, alert_delivery
    import proxy, routes_agents, routes_proxy, api_server
    for module in (proxy, alert_delivery, ledger_engine, workspace_engine, identity,
                   routes_agents, routes_proxy, api_server):
        importlib.reload(module)

    import uvicorn
    # Port retry, and a real teardown. Both are here because this test was FLAKY when
    # first written: it passed alone and failed inside the suite, once out of three runs,
    # because `_free_port()` closes its socket before uvicorn binds it — another test
    # can take the port in that window — and the fixture never stopped the server, so a
    # previous run's thread could outlive the test and mutate the shared module state the
    # next reload reads. A test that fails one run in three trains people to ignore red.
    server = None
    last = None
    for _ in range(5):
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(api_server.app, host="127.0.0.1",
                                               port=port, log_level="error"))
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 15
        while time.time() < deadline:
            if server.started:
                break
            if not t.is_alive():          # bind failed — port was taken; try another
                last = "server thread died (port in use)"
                break
            time.sleep(0.1)
        else:
            last = "server did not come up in 15s"
            continue
        if server.started:
            break
        server.should_exit = True
    else:
        raise RuntimeError(f"no free port would host the test server: {last}")

    yield base

    # Real teardown: stop the server and join its thread before the next fixture
    # reloads these modules.
    server.should_exit = True
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
        except Exception:
            break                             # refused connection == it is down
        time.sleep(0.1)
    t.join(timeout=5)


def _mint(base: str) -> str:
    """POST /start exactly as the form does. Returns the success-screen HTML."""
    req = urllib.request.Request(base + "/start", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode()


def _published_command(html: str, key: str) -> str:
    """Pull the copy-paste curl off the success screen and bind the customer's key.

    The screen is rendered by `api_server` and prints the real base URL. That URL is
    right in production and wrong in a test that runs the server on a free port, so we
    rewrite the host — the ROUTE and the BODY are still the product's own, verbatim,
    which is what this walk is about. Rewriting only the host is the difference between
    testing the published command and testing my own hand-written curl.
    """
    m = re.search(r"<pre[^>]*>(.*?)</pre>", html, re.S)
    assert m, "the success screen published no copy-paste command"
    cmd = m.group(1)
    cmd = (cmd.replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">")
              .replace("&amp;", "&"))
    return cmd.replace("YOUR_KEY", key)


def _run_command(cmd: str, base: str) -> tuple[int, str]:
    """Execute the published curl for real, via urllib so we can read the status."""
    url = re.search(r"(https?://\S+)", cmd).group(1)
    # Only the host is rewritten (the published page hardcodes the production base).
    # Route, body and headers are the product's own, byte for byte.
    url = base + urllib.parse.urlsplit(url).path
    body = re.search(r"-d '(\{.*\})'", cmd, re.S).group(1)
    headers = dict(re.findall(r'-H "([^:]+): ([^"]+)"', cmd))
    req = urllib.request.Request(url, data=body.encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_two_strangers_walk_the_published_first_step(live, capsys):
    """THE WALK. Prints the artifact; asserts both customers succeed."""
    print("\n" + "=" * 74)
    print("D-1382 END-TO-END WALK — two independent customers, published instruction")
    print("=" * 74)

    for n in (1, 2):
        html = _mint(live)
        key = re.search(r"wk_live_[A-Za-z0-9]+", html)
        assert key, f"customer {n}: /start issued no workspace_key"
        key = key.group(0)
        agent_id = re.search(r'"agent_id":"([^"]+)"', html)
        assert agent_id, f"customer {n}: the success screen published no agent_id"
        agent_id = agent_id.group(1)

        cmd = _published_command(html, key)
        status, resp = _run_command(cmd, live)

        print(f"\ncustomer {n}")
        print(f"  workspace_key  {key[:18]}…")
        print(f"  published id   {agent_id}")
        print(f"  instruction    {cmd[:110].replace(chr(10), ' ')}…")
        print(f"  -> HTTP {status}")

        assert status == 200, (
            f"customer {n} followed the product's OWN instruction and got HTTP {status}: {resp}")
        assert "agent_secret" in resp, f"customer {n} got no agent_secret: {resp}"
        print(f"  -> agent_secret issued ({len(resp)} bytes)")

    print("\nBOTH CUSTOMERS CLEARED STEP ONE")
    print("=" * 74)
    out = capsys.readouterr().out
    print(out)
