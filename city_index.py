# city_index.py — the trust-and-outcome index, generated from city_registry.json
#
# Blueprint v2 (approved sha 441bc3b4a1d1): every index surface must let an
# agent classify each product as TRUST or OUTCOME — and outward / spine /
# inward — in ONE pass, from machine-readable FIELDS, not marketing copy.
# This module is the single generator: /llms.txt's product block, the
# /v1/products enrichment, openapi's x-products, the root
# Accept:application/json block, and the 404 index payload all read the same
# registry rows through the functions below.
#
# The registry is a FILE (city_registry.json), not a constant, so a test
# fixture can point CITY_REGISTRY_PATH at a staged copy and watch the
# rendered index change — that property is pinned by tests/test_city_index.py.
import json
import os
import threading
from pathlib import Path

_DEFAULT_PATH = Path(__file__).resolve().parent / "city_registry.json"
_lock = threading.Lock()
_cache: dict = {"path": None, "mtime": None, "data": None}


def _registry_path() -> Path:
    """CITY_REGISTRY_PATH wins (tests and local fixtures); the shipped file
    is the default. Read at CALL time so a monkeypatched env var takes effect
    without module reload."""
    return Path(os.environ.get("CITY_REGISTRY_PATH") or _DEFAULT_PATH)


def load_registry() -> dict:
    """The registry, cached by path+mtime. A fixture that rewrites the file
    — or repoints the env var — is reflected on the next call, which is
    exactly what the index-generated-from-registry test needs."""
    path = _registry_path()
    mtime = path.stat().st_mtime
    with _lock:
        if _cache["path"] != path or _cache["mtime"] != mtime:
            _cache.update(path=path, mtime=mtime,
                          data=json.loads(path.read_text()))
        return _cache["data"]


def products() -> list[dict]:
    return load_registry().get("products", [])


def frame() -> dict:
    reg = load_registry()
    return {"frame": reg.get("frame"),
            "tagline": reg.get("tagline"),
            "questions": reg.get("questions", {}),
            "directions": reg.get("directions", {}),
            "parked": reg.get("parked", [])}


def index_payload() -> dict:
    """The whole index as one payload — root Accept:application/json and the
    404 agent response both serve this shape."""
    return {
        **frame(),
        "products": [
            {"id": p["id"], "title": p["title"], "slot": p["slot"],
             "direction": p["direction"], "tagline": p["tagline"],
             "connect": p["connect"], "free_proof": p["free_proof"]}
            for p in products()
        ],
        "machine_surfaces": {
            "llms_txt": "https://aiagentscity.com/llms.txt",
            "products": "https://aiagentscity.com/v1/products",
            "openapi": "https://aiagentscity.com/openapi.json",
            "mcp_catalog": "https://aiagentscity.com/.well-known/mcp.json",
            "agent_card": "https://aiagentscity.com/.well-known/agent.json",
        },
    }


def registry_enrichment(product_id: str) -> dict:
    """The slot/direction/connect/free_proof fields for one catalog row —
    merged onto /v1/products entries so the catalog carries the frame fields
    without duplicating tool lists or status notes."""
    for p in products():
        if p["id"] == product_id:
            return {"slot": p["slot"], "direction": p["direction"],
                    "connect": p["connect"], "free_proof": p["free_proof"]}
    return {}


def llms_products_block() -> str:
    """The products section of /llms.txt, grouped under the two questions.
    An agent with zero context must be able to answer 'which products are
    TRUST, which are OUTCOME, and how do I connect to each' from this block
    alone — that is the card's J5 acceptance."""
    reg = load_registry()
    q = reg.get("questions", {})
    lines = [
        "## Products — the two questions (blueprint v2)",
        "",
        f"TRUST   — {q.get('trust', '')}",
        f"OUTCOME — {q.get('outcome', '')}",
        "",
    ]
    def _row(p):
        lines.append(f"- {p['id']} — {p['tagline']}")
        lines.append(f"  slot={p['slot']} direction={p['direction']}")
        for kind, target in p["connect"].items():
            lines.append(f"  connect.{kind}: {target}")
        lines.append(f"  free proof: {p['free_proof']}")

    for slot in ("trust", "outcome"):
        lines.append(f"### {slot.upper()}")
        for p in products():
            if p["slot"] == slot:
                _row(p)
        lines.append("")
    # A product answering BOTH questions (the inward keystone) is not a third
    # question — it gets a section named by its direction, matching the
    # blueprint table verbatim.
    dual = [p for p in products() if p["slot"] not in ("trust", "outcome")]
    for p in dual:
        lines.append(f"### {p['direction'].upper()} ({p['slot']} — "
                     "answers both questions)")
        _row(p)
    return "\n".join(lines) + "\n"


def openapi_products() -> list[dict]:
    """The x-products extension shape — slot/direction/connect as first-class
    fields, per the card's openapi requirement."""
    return [
        {"id": p["id"], "x-slot": p["slot"], "x-direction": p["direction"],
         "x-connect": p["connect"], "tagline": p["tagline"],
         "free_proof": p["free_proof"]}
        for p in products()
    ]


NOT_FOUND_PAGE = """<!doctype html><html><head><title>404 — AI Agent City</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font:16px/1.6 ui-monospace,monospace;background:#0d1117;color:#c9d1d9;
max-width:640px;margin:12vh auto;padding:0 20px}
a{color:#58a6ff;text-decoration:none}h1{font-size:22px}
.dim{color:#8b949e}.code{background:#161b22;padding:2px 6px;border-radius:4px}</style>
</head><body>
<h1>That path doesn't exist.</h1>
<p class="dim">AI Agent City — the trust-and-outcome clearing layer for the
agent economy. You probably wanted one of these:</p>
<p><a href="/products">/products</a> — every product, with its TRUST or OUTCOME
slot<br><a href="/llms.txt">/llms.txt</a> — the machine-readable index<br>
<a href="/v1/products">/v1/products</a> — the same index as JSON<br>
<a href="/start">/start</a> — free workspace, no signup</p>
<p class="dim">Agents: request this path with
<span class="code">Accept: application/json</span> for the machine index.</p>
</body></html>"""
