#!/usr/bin/env python3
"""product_skills_adapter — register a product skill surface onto either of the
two MCP frameworks the fleet runs.

WHY A SEPARATE FILE: the fleet has two MCP stacks in production.
  * `fastmcp>=3.0`   — trust-scan, agent-ledger
  * `mcp<2` (official SDK) — perimeter-watch, cited, agent-watch
Their `resources/list` + `resources/read` APIs are shaped differently (different
FunctionResource modules, different transport objects, different signatures). A
product repo should not have to care: it imports one function and passes its own
FastMCP instance. This is the only place that knows about both.

Both stacks expose `FunctionResource.from_function(fn, uri, name=, description=,
mime_type=)`, which is what we use — the common denominator, verified 2026-09-14
against fastmcp 3.2.4 and the installed mcp SDK.

USAGE (in a product repo, after dropping in product_skills.py + this file):

    from product_skills_adapter import register_product_skills
    ...
    mcp = FastMCP("my-product")
    register_product_skills(mcp, product="my-product", skill_dir="skill")

Nothing here advertises the SEP-2640 extension — see product_skills.py.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import product_skills as ps  # noqa: E402


def _load_function_resource():
    """Return FunctionResource from whichever MCP stack is installed."""
    for modpath in ("fastmcp.resources", "mcp.server.fastmcp.resources"):
        try:
            mod = __import__(modpath, fromlist=["FunctionResource"])
            fr = getattr(mod, "FunctionResource")
            if hasattr(fr, "from_function"):
                return fr, modpath
        except Exception:
            continue
    return None, None


def register_product_skills(
    mcp: Any,
    product: str,
    skill_dir: str | os.PathLike = "skill",
    *,
    include_meta_tools: bool = True,
    verbose: bool = False,
) -> dict:
    """Register resources + meta-tools for one product's skill directory.

    Args:
        mcp: the product's FastMCP instance (either stack).
        product: the product slug; MUST equal each skill's frontmatter `name`
            for a single-skill product, and is the URI authority segment.
        skill_dir: directory holding the skill(s). Relative paths resolve against
            CWD so a product server can pass "skill" or "mcp-resources/../skill".
        include_meta_tools: also expose `skills_list_tool` + `read_skill`, the
            discovery path for hosts with partial resource support.
        verbose: print each registered resource URI to stderr.

    Returns a small report dict; registration never raises on a missing dir —
    a product server must keep working when it has no skill yet.
    """
    root = Path(skill_dir)
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    report: dict[str, Any] = {"product": product, "skill_dir": str(root),
                              "resources": 0, "skills": 0, "framework": None,
                              "skipped": None}

    if not root.is_dir():
        report["skipped"] = f"skill dir not found: {root}"
        return report

    FunctionResource, modpath = _load_function_resource()
    report["framework"] = modpath

    # ---------------------------------------------------------------- #
    # RESOURCES — one MCP resource per skill file (SEP-2640 mapping)
    # ---------------------------------------------------------------- #
    if FunctionResource is not None:
        for res in ps.resources_list(root, product):
            uri = res["uri"]

            def _reader(_uri=uri):
                got = ps.skill_resource(root, product, _uri)
                if not got:
                    raise ValueError(f"unknown skill resource: {_uri}")
                return got["contents"][0]["text"]

            try:
                fr = FunctionResource.from_function(
                    fn=_reader,
                    uri=uri,
                    name=res.get("name") or uri,
                    description=res.get("description") or f"{product} skill file",
                    mime_type=res.get("mimeType", "text/markdown"),
                )
                mcp.add_resource(fr)
                report["resources"] += 1
                if verbose:
                    print(f"  resource: {uri}", file=sys.stderr)
            except Exception as exc:  # never let one bad resource kill the mount
                print(f"  WARN: could not register {uri}: {exc}", file=sys.stderr)

    report["skills"] = len(ps.discover_skills(root))

    # ---------------------------------------------------------------- #
    # META-TOOLS — mirrors the `*_api_docs` pattern the products ship.
    # Gives agents a discovery path even where resources are only partly
    # reachable, and keeps the skill readable over plain tool calls.
    # ---------------------------------------------------------------- #
    if include_meta_tools:
        @mcp.tool()
        def skills_list_tool() -> dict:
            """List this product's skills. Each entry carries the SKILL.md URI,
            its name and description, verbatim frontmatter, and a per-file
            sha256 manifest. Read a body with `read_skill`."""
            return ps.skills_list(root, product)

        @mcp.tool()
        def read_skill(uri: str) -> dict:
            """Read a product skill file by its skill:// URI.

            Args:
                uri: e.g. skill://<product>/<skill-name>/SKILL.md
                     Get valid URIs from `skills_list_tool`.
            """
            got = ps.skill_resource(root, product, uri)
            if not got:
                return {"error": {"type": "not_found",
                                  "message": f"no such skill resource: {uri}"}}
            return got

    return report
