"""Report whether Claude Code asks before three actions in one folder.

Whether ``git merge``, ``gh``, and this tool's own command (which writes the
store) need a permission prompt is the user's choice per project. ``doctor``
shows the current answer from the settings files so the choice is visible. It
never changes a settings file, and the result does not change doctor's ok.

The rule reader is small on purpose. It reads ``Bash``, ``Bash(*)``,
``Bash(prefix:*)``, ``Bash(prefix *)``, ``Bash(prefix*)``, and
``Bash(exact command)``. Any other ``Bash`` rule, or one whose command holds
whitespace other than single spaces between words, is listed as not
interpreted, never guessed. Rules for other tools do not apply to these
actions. For this tool's own command only the bare name ``agent-efficiency``
is matched; rules for an interpreter or a full path are not evaluated.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Sequence

from agent_efficiency.claude_folder import git_root, is_trusted
from agent_efficiency.host_settings import project_settings_files, read_settings

ACTIONS = ("git merge", "gh", "agent-efficiency")
# Claude Code applies deny over ask over allow, whichever file a rule is in.
DECISION_ORDER = ("deny", "ask", "allow")
NOT_SET = "not set"
BASH_RULE = re.compile(r"Bash(?:\((?P<spec>.*)\))?", re.DOTALL)
# A tab, a newline, any other space character, or a run of spaces. The host may
# not match these as one space, so such a rule is not interpreted.
OTHER_WHITESPACE = re.compile(r"[^\S ]| {2}")
TOOL_COMMAND_CHECK = (
    "the agent-efficiency check matches only the bare command name "
    "agent-efficiency. Rules that allow an interpreter or a full path, such as "
    "Bash(python3:*), can also run the tool and are not evaluated here."
)
NOT_CHECKED = {
    "codex": "not checked: this report reads Claude Code settings only",
    "cursor": "not checked: this report reads Claude Code settings only",
    "managed": (
        "not checked: managed settings and command-line options can also "
        "allow, ask, or deny these actions"
    ),
}


def interpret_rule(rule: str) -> tuple[str, str] | None:
    """Return how a rule matches commands, or None when it is not a Bash rule.

    The result is ``("all", "")``, ``("word", prefix)`` for a prefix that ends
    at a word, ``("text", prefix)`` for a plain text prefix, ``("exact",
    command)``, or ``("unknown", "")`` for a Bash rule this reader cannot read.
    """

    text = rule.strip()
    match = BASH_RULE.fullmatch(text)
    if match is None:
        return ("unknown", "") if text.startswith("Bash") else None
    spec = match.group("spec")
    if spec is None:
        return ("all", "")
    spec = spec.strip()
    if spec in {"*", ":*"}:
        return ("all", "")
    if spec.endswith(":*") or spec.endswith(" *"):
        kind, prefix = "word", spec[:-2]
    elif spec.endswith("*"):
        kind, prefix = "text", spec[:-1]
    else:
        kind, prefix = "exact", spec
    if not prefix.strip() or "*" in prefix or OTHER_WHITESPACE.search(spec):
        return ("unknown", "")
    if kind == "text":
        return kind, prefix.lstrip()
    return kind, prefix.strip()


def coverage(kind: str, prefix: str, action: str) -> str | None:
    """Return ``"all"`` or ``"some"`` for the commands of ``action`` a rule
    covers, or None when it covers none of them."""

    if kind == "all":
        return "all"
    if kind == "word":
        if action == prefix or action.startswith(prefix + " "):
            return "all"
        return "some" if prefix.startswith(action + " ") else None
    if kind == "text":
        if action.startswith(prefix):
            return "all"
        return "some" if prefix.startswith(action + " ") else None
    if kind == "exact" and (prefix == action or prefix.startswith(action + " ")):
        return "some"
    return None


def permission_report(
    folder: Path, config_folders: Sequence[Path], home: Path
) -> dict[str, Any]:
    """Describe the permission rules for each action, per Claude Code profile."""

    folder = folder.expanduser().resolve()
    project_files = [
        (path, path.parent.parent) for path in project_settings_files(folder, home)
    ]
    profiles = []
    for config in config_folders:
        trust_file = (
            home / ".claude.json"
            if config == (home / ".claude").resolve()
            else config / ".claude.json"
        )
        profiles.append(
            _profile(
                config,
                [(config / "settings.json", config), *project_files],
                trusted=is_trusted(folder, git_root(folder), trust_file),
            )
        )
    return {
        "folder": str(folder),
        "informational": True,
        "profiles": profiles,
        "tool_command_check": TOOL_COMMAND_CHECK,
        "not_checked": NOT_CHECKED,
    }


def _profile(
    config: Path, files: list[tuple[Path, Path]], *, trusted: bool
) -> dict[str, Any]:
    read: list[dict[str, str]] = []
    matched: dict[str, list[dict[str, str]]] = {action: [] for action in ACTIONS}
    not_interpreted: list[dict[str, str]] = []
    default_modes: list[dict[str, str]] = []
    for path, base in files:
        document, problem = read_settings(path, base)
        if problem:
            read.append({"file": str(path), "status": problem})
            continue
        if document is None:
            continue
        read.append({"file": str(path), "status": "read"})
        permissions = document.get("permissions")
        if not isinstance(permissions, dict):
            continue
        mode = permissions.get("defaultMode")
        if isinstance(mode, str):
            default_modes.append({"file": str(path), "value": mode})
        for name in DECISION_ORDER:
            rules = permissions.get(name)
            for rule in rules if isinstance(rules, list) else []:
                if not isinstance(rule, str):
                    continue
                entry = {"rule": rule, "list": name, "file": str(path)}
                reading = interpret_rule(rule)
                if reading is None:
                    continue
                if reading[0] == "unknown":
                    not_interpreted.append(entry)
                    continue
                for action in ACTIONS:
                    covers = coverage(*reading, action)
                    if covers:
                        matched[action].append({**entry, "covers": covers})
    return {
        "config_folder": str(config),
        "workspace_trusted": trusted,
        "files": read,
        "actions": {
            action: {"decision": _decision(rules), "rules": rules}
            for action, rules in matched.items()
        },
        "not_interpreted": not_interpreted,
        "default_mode": default_modes,
    }


def _decision(rules: list[dict[str, str]]) -> str:
    """Deny over ask over allow, from rules that cover every form of the action.

    A rule that covers only some forms, such as ``Bash(gh pr view:*)`` for
    ``gh``, is listed but does not decide.
    """

    lists = {rule["list"] for rule in rules if rule["covers"] == "all"}
    return next((name for name in DECISION_ORDER if name in lists), NOT_SET)
