#!/usr/bin/env python3
"""Validate the standalone Agent Efficiency source distribution."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "agent-efficiency"
SRC = PLUGIN / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_efficiency import __version__  # noqa: E402
from agent_efficiency.capability_pack import (  # noqa: E402
    capability_pack_digest,
    load_bundled_capability_pack,
)
from agent_efficiency.policy import PolicyPack  # noqa: E402


FORBIDDEN = (
    "maintainer/components",
    "repository-only source",
    "SEE NOTICE",
    "personal project",
)
TEXT_SUFFIXES = {
    ".json",
    ".md",
    ".py",
    ".toml",
    ".yaml",
    ".yml",
}
RUNTIME_LIMIT_BYTES = 600 * 1024
# Each host manifest must name its own hook file. Claude Code loads a plugin's
# default hooks/hooks.json in addition to the file its manifest names, so the
# package must not ship that default file for any host.
HOST_HOOK_FILES = {
    "claude": "./hooks/claude-hooks.json",
    "codex": "./hooks/codex-hooks.json",
    "cursor": "./hooks/cursor-hooks.json",
}


def validate_distribution(root: Path = ROOT) -> dict[str, Any]:
    """Return standalone publication checks and stable failure messages."""

    errors: list[str] = []
    references: list[str] = []
    broken_links: list[str] = []
    invalid_json: list[str] = []
    for path in sorted(root.rglob("*")):
        if (
            not path.is_file()
            or ".git" in path.parts
            or "__pycache__" in path.parts
            or path.suffix not in TEXT_SUFFIXES
            or path.resolve() == Path(__file__).resolve()
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lowered = text.casefold()
        for term in FORBIDDEN:
            if term.casefold() in lowered:
                references.append(f"{path.relative_to(root)}: {term}")
        if re.search(r"/(?:home|Users)/[^/\s]+/", text):
            references.append(f"{path.relative_to(root)}: local home path")
        if re.search(
            r"[A-Za-z0-9._%+-]+@(?:gmail|outlook|yahoo)[.]com",
            text,
            re.IGNORECASE,
        ):
            references.append(f"{path.relative_to(root)}: personal email")
        for url in re.findall(r"https://github[.]com/[^\s)\"\x60]+", text):
            if not url.startswith("https://github.com/agent-efficiency"):
                references.append(f"{path.relative_to(root)}: external GitHub link")
        if path.suffix == ".json":
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                invalid_json.append(f"{path.relative_to(root)}: {exc}")
        if path.suffix == ".md":
            for target in re.findall(r"(?<!!)\[[^]]+\]\(([^)]+)\)", text):
                if target.startswith(("http://", "https://", "#", "mailto:")):
                    continue
                relative = target.split("#", 1)[0]
                if relative and not (path.parent / relative).resolve().exists():
                    broken_links.append(
                        f"{path.relative_to(root)}: {target}"
                    )
    if references:
        errors.append("forbidden project or personal references remain")
    if broken_links:
        errors.append("broken local Markdown links remain")
    if invalid_json:
        errors.append("invalid JSON files remain")

    if (root / "maintainer").exists() or (root / "capabilities").exists():
        errors.append("external maintainer trees remain")
    if (root / "NOTICE.md").exists():
        errors.append("mixed-license notice remains")

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = str(project["project"]["version"])
    if project_version != __version__:
        errors.append("Python package versions differ")
    authors = project["project"].get("authors", [])
    if authors != [{"name": "Agent Efficiency contributors"}]:
        errors.append("Python package author is not organization-owned")

    manifest_paths = {
        "claude": PLUGIN / ".claude-plugin" / "plugin.json",
        "cursor": PLUGIN / ".cursor-plugin" / "plugin.json",
        "codex": PLUGIN / ".codex-plugin" / "plugin.json",
    }
    manifests = {
        host: json.loads(path.read_text(encoding="utf-8"))
        for host, path in manifest_paths.items()
    }
    for host, manifest in manifests.items():
        if manifest.get("name") != "agent-efficiency":
            errors.append(f"{host} plugin name differs")
        if not str(manifest.get("version", "")).startswith(project_version):
            errors.append(f"{host} plugin version differs")
        author = manifest.get("author")
        if not isinstance(author, dict) or author.get("name") != (
            "Agent Efficiency maintainers"
        ):
            errors.append(f"{host} plugin author is not organization-owned")
        if "email" in author:
            errors.append(f"{host} plugin contains a personal email")

    claude_marketplace = json.loads(
        (root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    if claude_marketplace.get("version") != project_version:
        errors.append("Claude marketplace version differs")

    default_hooks = PLUGIN / "hooks" / "hooks.json"
    if default_hooks.exists():
        errors.append(
            f"{default_hooks}: remove this file. Claude Code loads the default "
            "hooks/hooks.json in addition to the hooks its manifest names, "
            "so no host may use it."
        )
    for host, hook_file in HOST_HOOK_FILES.items():
        manifest_path = manifest_paths[host]
        if manifests[host].get("hooks") != hook_file:
            errors.append(
                f"{manifest_path}: hooks must be {hook_file!r} so {host} "
                "loads only its own hook file"
            )
        elif not (PLUGIN / hook_file).is_file():
            errors.append(f"{manifest_path}: hook file {hook_file!r} is missing")

    pack = load_bundled_capability_pack()
    policies = PolicyPack.load(root / ".validation-data")
    runtime_files = [
        path
        for path in PLUGIN.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    ]
    runtime_bytes = sum(path.stat().st_size for path in runtime_files)
    if runtime_bytes > RUNTIME_LIMIT_BYTES:
        errors.append("runtime package exceeds size limit")

    return {
        "ok": not errors,
        "errors": errors,
        "forbidden_references": references,
        "broken_links": broken_links,
        "invalid_json": invalid_json,
        "runtime_package_bytes": runtime_bytes,
        "runtime_package_limit_bytes": RUNTIME_LIMIT_BYTES,
        "policy_count": len(policies.ids()),
        "operational_policy_count": 9,
        "guidance_card_count": pack["card_count"],
        "guidance_pack_id": pack["pack_id"],
        "guidance_pack_digest": capability_pack_digest(pack),
        "hosts": sorted(manifests),
        "version": project_version,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = validate_distribution()
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result["ok"]:
        print("Distribution validation passed.")
    else:
        for error in result["errors"]:
            print(error, file=sys.stderr)
        for reference in result["forbidden_references"]:
            print(reference, file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
