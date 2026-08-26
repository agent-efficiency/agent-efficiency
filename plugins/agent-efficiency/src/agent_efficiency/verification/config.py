"""Parse the repository-owned verification contract."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CHECK_ID_RE = re.compile(r"^[a-z][a-z0-9.-]{0,63}$")
VALID_PROJECT_MODES = {"off", "observe", "advise", "guard"}


@dataclass(frozen=True, slots=True)
class CheckDefinition:
    check_id: str
    command: tuple[str, ...]
    applies_to: tuple[str, ...]
    required_for: tuple[str, ...]
    timeout_seconds: int
    max_age_seconds: int | None
    env_allowlist: tuple[str, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    root: Path
    path: Path
    mode: str
    checks: tuple[CheckDefinition, ...]
    digest: str

    def get(self, check_id: str) -> CheckDefinition | None:
        return next(
            (check for check in self.checks if check.check_id == check_id), None
        )


def load_project_config(root: str | Path = ".") -> ProjectConfig:
    project_root = Path(root).expanduser().resolve()
    path = project_root / "agent-efficiency.toml"
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"verification config not found: {path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"invalid verification config: {exc}") from exc
    if set(raw) - {"version", "mode", "checks"}:
        raise ValueError("verification config has unknown top-level fields")
    if raw.get("version") != 1:
        raise ValueError("verification config version must be 1")
    mode = str(raw.get("mode") or "advise")
    if mode not in VALID_PROJECT_MODES:
        raise ValueError(f"invalid project mode: {mode}")
    values = raw.get("checks")
    if not isinstance(values, list) or not values:
        raise ValueError("verification config requires at least one check")
    checks: list[CheckDefinition] = []
    seen: set[str] = set()
    for value in values:
        checks.append(_parse_check(value, seen))
    canonical = {
        "version": 1,
        "mode": mode,
        "checks": [_check_canonical(check) for check in checks],
    }
    return ProjectConfig(
        root=project_root,
        path=path,
        mode=mode,
        checks=tuple(checks),
        digest=_digest(canonical),
    )


def _parse_check(value: Any, seen: set[str]) -> CheckDefinition:
    if not isinstance(value, dict):
        raise ValueError("each check must be a table")
    allowed = {
        "id",
        "command",
        "applies_to",
        "required_for",
        "timeout_seconds",
        "max_age_seconds",
        "env_allowlist",
    }
    if set(value) - allowed:
        raise ValueError("check has unknown fields")
    check_id = str(value.get("id") or "")
    if not CHECK_ID_RE.fullmatch(check_id) or check_id in seen:
        raise ValueError(f"invalid or duplicate check id: {check_id}")
    seen.add(check_id)
    command = _string_tuple(value.get("command"), "command", required=True)
    if len(command) > 32 or any(len(part) > 1000 for part in command):
        raise ValueError(f"{check_id}: command is too large")
    applies_to = _string_tuple(value.get("applies_to", []), "applies_to")
    required_for = _string_tuple(value.get("required_for", []), "required_for")
    env_allowlist = _string_tuple(value.get("env_allowlist", []), "env_allowlist")
    if any(not item.isidentifier() or len(item) > 80 for item in env_allowlist):
        raise ValueError(f"{check_id}: invalid environment variable name")
    timeout = value.get("timeout_seconds", 300)
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 1 <= timeout <= 3600
    ):
        raise ValueError(f"{check_id}: timeout_seconds must be 1 through 3600")
    max_age = value.get("max_age_seconds")
    if max_age is not None and (
        not isinstance(max_age, int)
        or isinstance(max_age, bool)
        or not 1 <= max_age <= 2_592_000
    ):
        raise ValueError(f"{check_id}: max_age_seconds must be 1 through 2592000")
    canonical = {
        "id": check_id,
        "command": command,
        "applies_to": applies_to,
        "required_for": required_for,
        "timeout_seconds": timeout,
        "max_age_seconds": max_age,
        "env_allowlist": env_allowlist,
    }
    return CheckDefinition(
        check_id=check_id,
        command=command,
        applies_to=applies_to,
        required_for=required_for,
        timeout_seconds=timeout,
        max_age_seconds=max_age,
        env_allowlist=env_allowlist,
        digest=_digest(canonical),
    )


def _string_tuple(value: Any, field: str, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (required and not value):
        raise ValueError(
            f"{field} must be a non-empty string array"
            if required
            else f"{field} must be a string array"
        )
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{field} must contain non-empty strings")
    return tuple(value)


def _check_canonical(check: CheckDefinition) -> dict[str, Any]:
    return {
        "id": check.check_id,
        "command": check.command,
        "applies_to": check.applies_to,
        "required_for": check.required_for,
        "timeout_seconds": check.timeout_seconds,
        "max_age_seconds": check.max_age_seconds,
        "env_allowlist": check.env_allowlist,
    }


def _digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()
