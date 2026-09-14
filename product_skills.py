#!/usr/bin/env python3
"""product_skills — serve a product's SKILL.md over its own MCP server.

Implements the SEP-2640 *shape* (modelcontextprotocol/ext-skills, extension id
`io.modelcontextprotocol/skills`) without advertising the extension capability.

WHY THE FLAG: SEP-2640 is a draft that was reworked twice in seven weeks
(2026-06-19, 2026-08-05) and both rounds were breaking. No MCP SDK ships typed
models for it. A server that advertises an extension while serving a pinned draft
is worse than one that serves nothing, because a conforming client will negotiate
a contract we only half-hold. So: `skill://` resources are live and readable
today; the capability advertisement is opt-in behind PRODUCT_SKILLS_ADVERTISE=1,
to be flipped only when the SDK ships typed models.

WIRE SHAPE (draft-pinned, dated 2026-09-14):
    skill://<product>/<skill-path>/SKILL.md      -> the skill body
    skill://<product>/<skill-path>/<file-path>   -> supporting file
    <skill-path>'s final segment == the frontmatter `name` field.

Stdlib only. Drop this file into any product repo as `mcp-resources/product_skills.py`
and call the four functions below from the product's existing MCP server.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

SKILLS_EXTENSION = "io.modelcontextprotocol/skills"
DEFAULT_SCHEME = "skill"

# --------------------------------------------------------------------------- #
# frontmatter
# --------------------------------------------------------------------------- #

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


def _minimal_yaml(text: str) -> dict:
    """Parse the small subset of YAML our frontmatter uses, with no dependency.

    Handles: `key: value`, quoted values, inline lists `[a, b]`, and one level of
    nesting (`metadata:` + indented keys). Anything more exotic should use PyYAML
    (we try that first).
    """
    out: dict[str, Any] = {}
    nest: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if indent > 0 and nest:
            out.setdefault(nest, {})
            if isinstance(out[nest], dict):
                out[nest][key] = _scalar(val)
            continue
        if val == "":
            nest = key
            out[key] = {}
        else:
            nest = None
            out[key] = _scalar(val)
    return out


def _scalar(val: str) -> Any:
    v = val.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        return [p.strip().strip("\"'") for p in v[1:-1].split(",") if p.strip()]
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    return v


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body). Falls back to a minimal parser."""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    block = m.group(1)
    try:  # prefer real YAML when available
        import yaml  # type: ignore

        data = yaml.safe_load(block) or {}
        if isinstance(data, dict):
            return data, text[m.end():]
    except Exception:
        pass
    return _minimal_yaml(block), text[m.end():]


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #

def _sha256_uri(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def discover_skills(skill_root: str | os.PathLike) -> list[dict]:
    """Find every SKILL.md under a root and build SEP-2640 skill entries.

    Nested skills are supported: each SKILL.md found at any depth is its own skill,
    whose `<skill-path>` is its directory path relative to the root.
    """
    root = Path(skill_root).expanduser().resolve()
    if not root.is_dir():
        return []
    entries: list[dict] = []
    for skill_md in sorted(root.rglob("SKILL.md")):
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        fm, _ = parse_frontmatter(text)
        name = str(fm.get("name") or skill_md.parent.name)
        # A skill may be the root of its own skill_root (one product, one skill dir)
        # or nested under it. `relative_to` yields "." for the former; SEP-2640 wants
        # the directory NAME there, not a dot segment, because the final URI segment
        # MUST equal the skill's frontmatter name.
        rel = skill_md.parent.relative_to(root)
        skill_path = root.name if rel == Path(".") else rel.as_posix()
        files = []
        for f in sorted(skill_md.parent.rglob("*")):
            if f.is_file():
                rel = f.relative_to(skill_md.parent).as_posix()
                files.append({"path": rel, "abs": f})
        entries.append({
            "skill_path": skill_path,
            "name": name,
            "frontmatter": fm,
            "description": str(fm.get("description") or ""),
            "dir": skill_md.parent,
            "skill_md": skill_md,
            "files": files,
        })
    return entries


# --------------------------------------------------------------------------- #
# SEP-2640 shapes
# --------------------------------------------------------------------------- #

def _uri(product: str, *parts: str, scheme: str = DEFAULT_SCHEME) -> str:
    return f"{scheme}://{product}/" + "/".join(p.strip("/") for p in parts if p)


def skills_list(skill_root, product: str, scheme: str = DEFAULT_SCHEME) -> dict:
    """`skills/list` result shape: {skills: [Skill, ...]}.

    Each Skill carries verbatim frontmatter plus a per-file {uri, digest} manifest.
    """
    skills = []
    for e in discover_skills(skill_root):
        resources = [
            {
                "uri": _uri(product, e["skill_path"], f["path"], scheme=scheme),
                "digest": _sha256_uri(f["abs"]),
            }
            for f in e["files"]
        ]
        skills.append({
            "uri": _uri(product, e["skill_path"], "SKILL.md", scheme=scheme),
            "name": e["name"],
            "description": e["description"],
            "frontmatter": e["frontmatter"],
            "resources": resources,
        })
    return {"skills": skills}


def _find_entry(skill_root, product: str, uri: str, scheme: str = DEFAULT_SCHEME):
    """Resolve a skill:// URI to (entry, relative_file_path)."""
    prefix = f"{scheme}://{product}/"
    if not uri.startswith(prefix):
        return None, None
    rest = uri[len(prefix):]
    entries = discover_skills(skill_root)
    # longest skill_path match wins (handles nesting)
    best = None
    for e in entries:
        sp = e["skill_path"] + "/"
        if rest == e["skill_path"] + "/SKILL.md" or rest.startswith(sp):
            if best is None or len(e["skill_path"]) > len(best["skill_path"]):
                best = e
    if best is None:
        return None, None
    rel = rest[len(best["skill_path"]):].lstrip("/")
    return best, (rel or "SKILL.md")


def skill_get(skill_root, product: str, uri: str, scheme: str = DEFAULT_SCHEME) -> dict | None:
    """`skills/get` result: one Skill entry, or None."""
    entry, rel = _find_entry(skill_root, product, uri, scheme=scheme)
    if entry is None:
        return None
    for s in skills_list(skill_root, product, scheme=scheme)["skills"]:
        if s["name"] == entry["name"] and s["uri"].endswith(entry["skill_path"] + "/SKILL.md"):
            return s
    return None


def resources_list(skill_root, product: str, scheme: str = DEFAULT_SCHEME) -> list[dict]:
    """MCP `resources/list` payload for a product's skills.

    Every file is a resource, per SEP-2640 §Resource Mapping. The SKILL.md resource
    carries name + description lifted from frontmatter.
    """
    out = []
    for e in discover_skills(skill_root):
        for f in e["files"]:
            res = {
                "uri": _uri(product, e["skill_path"], f["path"], scheme=scheme),
                "mimeType": _mime(f["path"]),
            }
            if f["path"] == "SKILL.md":
                res["name"] = e["name"]
                res["description"] = e["description"]
            out.append(res)
    return out


def skill_resource(skill_root, product: str, uri: str, scheme: str = DEFAULT_SCHEME) -> dict | None:
    """MCP `resources/read` payload for one skill file, or None if unknown.

    Returns {contents: [{uri, mimeType, text}]} — the bytes on disk, unmodified.
    """
    entry, rel = _find_entry(skill_root, product, uri, scheme=scheme)
    if entry is None or not rel:
        return None
    target = (entry["dir"] / rel).resolve()
    # path-traversal guard: never serve outside the skill directory
    try:
        target.relative_to(entry["dir"].resolve())
    except ValueError:
        return None
    if not target.is_file():
        return None
    body = target.read_text(encoding="utf-8", errors="replace")
    return {"contents": [{
        "uri": uri,
        "mimeType": _mime(rel),
        "text": body,
    }]}


def _mime(path: str) -> str:
    if path.endswith(".md"):
        return "text/markdown"
    if path.endswith(".json"):
        return "application/json"
    if path.endswith(".py"):
        return "text/x-python"
    if path.endswith((".yaml", ".yml")):
        return "application/yaml"
    if path.endswith((".txt", ".sh", ".csv")):
        return "text/plain"
    return "application/octet-stream"


# --------------------------------------------------------------------------- #
# capability negotiation
# --------------------------------------------------------------------------- #

def advertise_extension() -> bool:
    """Is the extension advertisement switched on? Off unless explicitly enabled.

    Flip this only when the MCP Python SDK ships typed SEP-2640 models — see the
    module docstring. Nothing else in this file depends on it.
    """
    return os.environ.get("PRODUCT_SKILLS_ADVERTISE", "").strip() in ("1", "true", "yes")


def capabilities_block(directory_read: bool = True) -> dict:
    """The `extensions` fragment to merge into a server's capabilities.

    Returns {} (an empty merge) while the advertisement is off.
    """
    if not advertise_extension():
        return {}
    ext: dict[str, Any] = {}
    if directory_read:
        ext["directoryRead"] = True
    return {"extensions": {SKILLS_EXTENSION: ext}}


# --------------------------------------------------------------------------- #
# CLI (debugging)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print(__doc__)
        print("usage: product_skills.py <skill_root> <product> [uri]")
        raise SystemExit(2)
    root, prod = sys.argv[1], sys.argv[2]
    if len(sys.argv) > 3:
        out = skill_resource(root, prod, sys.argv[3])
    else:
        out = skills_list(root, prod)
    print(json.dumps(out, indent=2, default=str))
