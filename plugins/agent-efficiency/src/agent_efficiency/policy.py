"""Deterministic classification and review-gated runtime policy selection."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from agent_efficiency.capability_pack import (
    capability_pack_policies,
    load_bundled_capability_pack,
)
from agent_efficiency.models import Nudge, TaskFacts, ToolFacts
from agent_efficiency.paths import RuntimePaths, bundled_file


DOC_EXTENSIONS = {".md", ".mdx", ".rst", ".txt", ".adoc"}
CODE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scss",
    ".sh",
    ".sql",
    ".svelte",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".vue",
    ".yaml",
    ".yml",
}


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class PolicyPack:
    def __init__(self, policies: Iterable[dict[str, Any]]) -> None:
        now = datetime.now(UTC)
        self._policies: dict[str, dict[str, Any]] = {}
        for policy in policies:
            if policy.get("status") != "reviewed":
                continue
            policy_id = str(policy.get("id", ""))
            expires = policy.get("expires_at")
            constitutional_floor = (
                policy_id.startswith("core.")
                and policy.get("constitutional_floor") is True
            )
            if not expires or (
                _parse_time(str(expires)) <= now and not constitutional_floor
            ):
                continue
            message = str(policy.get("message", "")).strip()
            if policy_id and message:
                self._policies[policy_id] = policy

    @classmethod
    def load(
        cls,
        data_root: str | Path | None = None,
        *,
        include_first_party: bool = True,
    ) -> "PolicyPack":
        bundled = bundled_file("policies", "core.json")
        documents = [_read_json(bundled)]
        first_party_policies: list[dict[str, Any]] = []
        if include_first_party:
            try:
                first_party_policies = capability_pack_policies(
                    load_bundled_capability_pack()
                )
            except (OSError, ValueError):
                # A broken optional knowledge layer must not disable operational
                # efficiency and safety checks in the hot path.
                first_party_policies = []
        active = RuntimePaths.from_root(data_root).active_pack
        if active.exists():
            documents.append(_read_json(active))
        policies: list[dict[str, Any]] = list(first_party_policies)
        for document in documents:
            policies.extend(document.get("policies", []))
        return cls(policies)

    def get(self, policy_id: str) -> Nudge | None:
        policy = self._policies.get(policy_id)
        if not policy:
            return None
        return Nudge(policy_id=policy_id, message=str(policy["message"]))

    def task_guidance(self, facts: TaskFacts, prompt: str) -> Nudge | None:
        core_map = {
            "diagnose": "core.root-cause",
            "review": "core.narrow-review",
            "research": "core.research-boundary",
            "design": "core.research-boundary",
            "build": "core.work-packet",
            "fix": "core.work-packet",
            "refactor": "core.work-packet",
            "migration": "core.work-packet",
        }
        lowered = prompt.casefold()
        candidates: list[tuple[int, str, dict[str, Any]]] = []
        for candidate_id, policy in self._policies.items():
            if policy.get("kind") != "knowledge_guidance":
                continue
            task_types = set(policy.get("task_types", []))
            if (
                task_types
                and facts.task_type not in task_types
                and "any" not in task_types
            ):
                continue
            keywords = [
                str(item).casefold() for item in policy.get("trigger_keywords", [])
            ]
            score = sum(1 for keyword in keywords if keyword and keyword in lowered)
            if score:
                candidates.append((score, candidate_id, policy))

        best = max(candidates, key=lambda item: (item[0], item[1]), default=None)
        if best and best[0] >= 2:
            _, candidate_id, policy = best
            return Nudge(policy_id=candidate_id, message=str(policy["message"]))

        policy_id = core_map.get(facts.task_type)
        if policy_id and facts.substantial:
            return self.get(policy_id)

        if best:
            _, candidate_id, policy = best
            return Nudge(policy_id=candidate_id, message=str(policy["message"]))
        return None

    def ids(self) -> set[str]:
        return set(self._policies)


def classify_task(prompt: str) -> TaskFacts:
    lowered = prompt.casefold()
    prompt_chars = len(prompt)
    if re.search(r"\b(review|audit|critique|assess|inspect the diff)\b", lowered):
        task_type = "review"
    elif re.search(
        r"\b(debug|diagnos(?:e|is)|investigate|root cause|failing|failure|trace why)\b",
        lowered,
    ):
        task_type = "diagnose"
    elif re.search(r"\b(migrat(?:e|ion)|schema change|data backfill)\b", lowered):
        task_type = "migration"
    elif re.search(r"\b(refactor|restructure|reorganize)\b", lowered):
        task_type = "refactor"
    elif re.search(
        r"\b(research|compare|look up|find the latest|survey|evaluate options)\b",
        lowered,
    ):
        task_type = "research"
    elif re.search(
        r"\b(design|architect(?:ure)?|strategy|proposal|plan for)\b", lowered
    ):
        task_type = "design"
    elif re.search(r"\b(fix|repair|resolve|correct)\b", lowered):
        task_type = "fix"
    elif re.search(
        r"\b(build|implement|add|create|change|update|write|set up|setup|ship)\b",
        lowered,
    ):
        task_type = "build"
    elif re.search(r"\b(explain|what is|how does|summarize)\b", lowered):
        task_type = "explain"
    else:
        task_type = "unknown"

    high_risk = re.search(
        r"\b(production|deploy|release|authentication|authorization|security|"
        r"permission|secret|credential|billing|payment|database|delete|destructive|"
        r"migration|customer data|personal data|pii|compliance)\b",
        lowered,
    )
    risk = "high" if high_risk else "normal"

    trivial = bool(
        prompt_chars < 100
        and re.search(
            r"\b(typo|rename|format|one line|single line|spelling)\b", lowered
        )
    )
    inherently_substantial = task_type in {
        "review",
        "diagnose",
        "research",
        "design",
        "migration",
        "refactor",
    }
    substantial = not trivial and (
        inherently_substantial
        or risk == "high"
        or prompt_chars >= 140
        or "\n" in prompt.strip()
    )
    return TaskFacts(
        task_type=task_type,
        risk=risk,
        substantial=substantial,
        prompt_chars=prompt_chars,
    )


def classify_tool(tool_name: str, tool_input: Any) -> ToolFacts:
    safe_input = tool_input if isinstance(tool_input, dict) else {}
    lowered_name = tool_name.casefold()
    command = _command_text(safe_input) if _is_shell_tool(lowered_name) else ""
    command_class = _command_class(command, lowered_name)
    extensions = tuple(sorted(_extract_extensions(safe_input)))
    is_edit = _is_edit_tool(lowered_name)
    if not is_edit and command:
        is_edit = bool(
            re.search(
                r"(^|[;&|]\s*)(apply_patch|sed\s+-i|perl\s+-pi|"
                r"git\s+apply|tee\s+\S+|cp\s+\S+\s+\S+|mv\s+\S+\s+\S+)",
                command,
            )
        )
    is_code_edit = is_edit and (
        not extensions or bool(set(extensions) & CODE_EXTENSIONS)
    )
    is_subagent = (
        lowered_name in {"agent", "spawn_agent", "task", "subagent"}
        or "spawn_agent" in lowered_name
        or "subagent" in lowered_name
    )
    broad_scan = _is_broad_scan(command, lowered_name, safe_input)
    signature_payload = json.dumps(
        {"tool": tool_name, "input": safe_input},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    signature = hashlib.sha256(
        ("agent-efficiency-v1:" + signature_payload).encode("utf-8")
    ).hexdigest()[:20]
    is_validation = command_class in {"test", "lint", "typecheck", "build", "check"}
    return ToolFacts(
        tool_name=tool_name,
        command_class=command_class,
        signature=signature,
        is_edit=is_edit,
        is_code_edit=is_code_edit,
        is_validation=is_validation,
        is_subagent=is_subagent,
        is_broad_scan=broad_scan,
        file_extensions=extensions,
        safe_details={
            "extensions": list(extensions),
            "broad_scan": broad_scan,
        },
    )


def _is_shell_tool(lowered_name: str) -> bool:
    return lowered_name in {"bash", "shell", "exec", "exec_command", "terminal"} or (
        "exec_command" in lowered_name
    )


def _is_edit_tool(lowered_name: str) -> bool:
    return (
        lowered_name in {"edit", "write", "apply_patch", "multiedit", "notebookedit"}
        or "apply_patch" in lowered_name
        or lowered_name.endswith("__write_file")
        or lowered_name.endswith("__edit_file")
    )


def _command_text(tool_input: dict[str, Any]) -> str:
    for key in ("command", "cmd", "script"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value.casefold()
    return ""


def _command_class(command: str, lowered_name: str) -> str:
    if not command:
        if _is_edit_tool(lowered_name):
            return "edit"
        if lowered_name in {"read", "grep", "glob"}:
            return "search"
        if "web" in lowered_name or "fetch" in lowered_name:
            return "research"
        if "agent" in lowered_name or "task" == lowered_name:
            return "subagent"
        return "other"
    patterns = (
        (
            "test",
            r"(^|[;&|]\s*)(pytest|python(?:3)?\s+-m\s+(pytest|unittest)|"
            r"npm\s+(run\s+)?test|pnpm\s+(run\s+)?test|yarn\s+test|"
            r"bun\s+test|cargo\s+test|go\s+test|mvn\s+test|gradle\w*\s+test|"
            r"dotnet\s+test|rspec|phpunit)\b",
        ),
        (
            "lint",
            r"(^|[;&|]\s*)(ruff|eslint|biome\s+(lint|check)|flake8|pylint|"
            r"shellcheck|hadolint|yamllint|ansible-lint|stylelint)\b",
        ),
        (
            "typecheck",
            r"(^|[;&|]\s*)(mypy|pyright|basedpyright|tsc\b|"
            r"npm\s+run\s+typecheck|pnpm\s+typecheck|go\s+vet)\b",
        ),
        (
            "build",
            r"(^|[;&|]\s*)(npm|pnpm|yarn|bun)\s+(run\s+)?build\b|"
            r"(^|[;&|]\s*)(cargo|go|mvn|gradle\w*|dotnet)\s+build\b",
        ),
        (
            "check",
            r"\bgit\s+diff\s+--check\b|\bplugin\s+validate\b|"
            r"\bquick_validate\.py\b|\bvalidate_plugin\.py\b|\bdoctor\b",
        ),
        (
            "install",
            r"(^|[;&|]\s*)(pip(?:3)?\s+install|python(?:3)?\s+-m\s+pip\s+install|"
            r"uv\s+(pip\s+)?install|npm\s+(install|ci)|pnpm\s+install|"
            r"yarn\s+install|bun\s+install|cargo\s+install|apt(?:-get)?\s+install)\b",
        ),
        ("search", r"(^|[;&|]\s*)(rg|grep|find|fd|ls)\b"),
        (
            "branch-create",
            r"(^|[;&|]\s*)git\s+(?:switch\s+-c|checkout\s+-b|branch\s+[^-\s;&|][^\s;&|]*)\b",
        ),
        ("fetch", r"(^|[;&|]\s*)git\s+(fetch|pull)\b"),
        (
            "git",
            r"(^|[;&|]\s*)git\s+(status|diff|log|show|branch|add|commit|push|merge|rebase)\b",
        ),
        (
            "network",
            r"(^|[;&|]\s*)(curl|wget|gh\s+api|gh\s+repo|git\s+(clone|fetch|pull))\b",
        ),
        ("inspect", r"(^|[;&|]\s*)(sed|head|tail|cat|wc|jq)\b"),
    )
    for category, pattern in patterns:
        if re.search(pattern, command):
            return category
    return "other"


def _extract_extensions(tool_input: dict[str, Any]) -> set[str]:
    extensions: set[str] = set()
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str):
            suffix = Path(value).suffix.casefold()
            if suffix:
                extensions.add(suffix)
    for key in ("patch", "input", "content"):
        value = tool_input.get(key)
        if not isinstance(value, str):
            continue
        if "*** " not in value and "diff --git " not in value:
            continue
        for match in re.finditer(
            r"(?:\*\*\* (?:Add|Update|Delete) File: |[ab]/)([^\s]+)", value
        ):
            suffix = Path(match.group(1)).suffix.casefold()
            if suffix:
                extensions.add(suffix)
    return extensions


def _is_broad_scan(command: str, lowered_name: str, tool_input: dict[str, Any]) -> bool:
    if command:
        dangerous_scope = (
            r"\bfind\s+/(?:\s|$)",
            r"\b(?:grep|rg)\b[^\n]*\s/(?:\s|$)",
            r"\bls\s+-[a-z]*r[a-z]*\s+/(?:\s|$)",
            r"\b(?:grep|rg)\s+-[^\n]*r[^\n]*\s+(?:~|\$home)(?:\s|$)",
        )
        return any(re.search(pattern, command) for pattern in dangerous_scope)
    if lowered_name == "glob":
        pattern = str(tool_input.get("pattern", ""))
        path = str(tool_input.get("path", ""))
        return pattern in {"**/*", "**/**"} and path in {"/", "~"}
    return False


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path}")
    return value
