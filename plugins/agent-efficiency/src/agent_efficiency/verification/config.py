"""Parse the repository-owned verification contract."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CHECK_ID_RE = re.compile(r"^[a-z][a-z0-9.-]{0,63}$")
VALID_PROJECT_MODES = {"off", "observe", "advise", "guard"}
# A config is a short file. A larger file is not a valid config.
MAX_CONFIG_BYTES = 262_144
# Symbolic links are followed, as before. The open never waits, so a named
# pipe at the config path returns at once.
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


class ConfigMissing(ValueError):
    """The project has no agent-efficiency.toml.

    A missing file is the normal state of a project without a config. A file
    that exists and does not load raises a plain ValueError instead, so guard
    mode can tell the two apart.
    """


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
    depends_on: tuple[str, ...] = ()
    env_fingerprint: tuple[str, ...] = ()
    result_file: str | None = None


@dataclass(frozen=True, slots=True)
class MilestoneDefinition:
    milestone_id: str
    checks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    root: Path
    path: Path
    mode: str
    checks: tuple[CheckDefinition, ...]
    digest: str
    milestones: tuple[MilestoneDefinition, ...] = ()

    def get(self, check_id: str) -> CheckDefinition | None:
        return next(
            (check for check in self.checks if check.check_id == check_id), None
        )


def load_project_config(root: str | Path = ".") -> ProjectConfig:
    project_root = Path(root).expanduser().resolve()
    path = project_root / "agent-efficiency.toml"
    text = _read_config_text(path)
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid verification config: {exc}") from exc
    except (RecursionError, MemoryError) as exc:
        # Deep nesting can exhaust the parser. The file is not a valid config.
        raise ValueError(
            "invalid verification config: it is nested too deeply"
        ) from exc
    if set(raw) - {"version", "mode", "checks", "milestones"}:
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
    _validate_dependencies(checks)
    milestones = _parse_milestones(raw.get("milestones", []), seen)
    canonical = {
        "version": 1,
        "mode": mode,
        "checks": [_check_canonical(check) for check in checks],
    }
    if milestones:
        canonical["milestones"] = [
            {"id": item.milestone_id, "checks": item.checks} for item in milestones
        ]
    return ProjectConfig(
        root=project_root,
        path=path,
        mode=mode,
        checks=tuple(checks),
        digest=_digest(canonical),
        milestones=milestones,
    )


def _parse_milestones(values: Any, checks: set[str]) -> tuple[MilestoneDefinition, ...]:
    if not isinstance(values, list) or len(values) > 256:
        raise ValueError("milestones must be a bounded array")
    result = []
    seen = set()
    for value in values:
        if not isinstance(value, dict) or set(value) != {"id", "checks"}:
            raise ValueError("milestone requires only id and checks")
        key = value["id"]
        if not isinstance(key, str) or not CHECK_ID_RE.fullmatch(key) or key in seen:
            raise ValueError("invalid or duplicate milestone id")
        required = _string_tuple(value["checks"], "milestone checks", required=True)
        if len(set(required)) != len(required) or not set(required) <= checks:
            raise ValueError("milestone checks must be unique configured check IDs")
        seen.add(key)
        result.append(MilestoneDefinition(key, required))
    return tuple(result)


def _read_config_text(path: Path) -> str:
    """Return the config text, or raise when the file cannot be a config.

    The open does not wait, so a named pipe cannot stall a hook. Only a regular
    file of at most ``MAX_CONFIG_BYTES`` is read. A missing file raises
    ``ConfigMissing``; any other problem raises a plain ``ValueError`` whose
    message holds no file content.
    """

    try:
        descriptor = os.open(path, _OPEN_FLAGS)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise ConfigMissing(f"verification config not found: {path}") from exc
    except OSError as exc:
        raise ValueError("invalid verification config: it cannot be read") from exc
    chunks: list[bytes] = []
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("invalid verification config: it is not a regular file")
        remaining = MAX_CONFIG_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 65_536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise ValueError("invalid verification config: it cannot be read") from exc
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if len(data) > MAX_CONFIG_BYTES:
        raise ValueError(
            f"invalid verification config: it is larger than {MAX_CONFIG_BYTES} bytes"
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid verification config: it is not UTF-8 text") from exc


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
        "depends_on",
        "env_fingerprint",
        "result_file",
    }
    if set(value) - allowed:
        raise ValueError("check has unknown fields")
    check_id = str(value.get("id") or "")
    if not CHECK_ID_RE.fullmatch(check_id) or check_id in seen:
        raise ValueError(f"invalid or duplicate check id: {check_id}")
    seen.add(check_id)
    result_file = value.get("result_file")
    if result_file is not None and (
        not isinstance(result_file, str)
        or not result_file
        or len(result_file) > 512
        or Path(result_file).is_absolute()
        or ".." in Path(result_file).parts
        or "\x00" in result_file
    ):
        raise ValueError(
            f"{check_id}: result_file must be a relative path inside the project"
        )
    command = _string_tuple(value.get("command"), "command", required=True)
    if len(command) > 32 or any(len(part) > 1000 for part in command):
        raise ValueError(f"{check_id}: command is too large")
    applies_to = _string_tuple(value.get("applies_to", []), "applies_to")
    required_for = _string_tuple(value.get("required_for", []), "required_for")
    env_allowlist = _string_tuple(value.get("env_allowlist", []), "env_allowlist")
    env_fingerprint = _string_tuple(value.get("env_fingerprint", []), "env_fingerprint")
    if (
        len(env_fingerprint) > 32
        or len(set(env_fingerprint)) != len(env_fingerprint)
        or not set(env_fingerprint) <= set(env_allowlist) | {"PATH"}
    ):
        raise ValueError(
            f"{check_id}: environment fingerprint names must be unique and allowed"
        )
    depends_on = _string_tuple(value.get("depends_on", []), "depends_on")
    if len(set(depends_on)) != len(depends_on) or any(
        not CHECK_ID_RE.fullmatch(item) for item in depends_on
    ):
        raise ValueError(f"{check_id}: invalid or duplicate prerequisite")
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
    if result_file is not None:
        canonical["result_file"] = result_file
    if env_fingerprint:
        canonical["env_fingerprint"] = env_fingerprint
    if depends_on:
        canonical["depends_on"] = depends_on
    return CheckDefinition(
        check_id=check_id,
        command=command,
        applies_to=applies_to,
        required_for=required_for,
        timeout_seconds=timeout,
        max_age_seconds=max_age,
        env_allowlist=env_allowlist,
        digest=_digest(canonical),
        depends_on=depends_on,
        env_fingerprint=env_fingerprint,
        result_file=result_file,
    )


def _validate_dependencies(checks: list[CheckDefinition]) -> None:
    """Validate the complete graph without recursion, including unselected checks."""
    from collections import deque

    by_id = {check.check_id: check for check in checks}
    remaining = {check.check_id: len(check.depends_on) for check in checks}
    consumers: dict[str, list[str]] = {key: [] for key in by_id}
    for check in checks:
        for dependency in check.depends_on:
            if dependency not in by_id or dependency == check.check_id:
                raise ValueError(f"{check.check_id}: unknown or self prerequisite")
            consumers[dependency].append(check.check_id)
    ready = deque(key for key, count in remaining.items() if count == 0)
    visited = 0
    while ready:
        key = ready.popleft()
        visited += 1
        for consumer in consumers[key]:
            remaining[consumer] -= 1
            if remaining[consumer] == 0:
                ready.append(consumer)
    if visited != len(checks):
        raise ValueError("verification prerequisites contain a cycle")


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
        **({"depends_on": check.depends_on} if check.depends_on else {}),
        **({"env_fingerprint": check.env_fingerprint} if check.env_fingerprint else {}),
        **({"result_file": check.result_file} if check.result_file is not None else {}),
    }


def _digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()
