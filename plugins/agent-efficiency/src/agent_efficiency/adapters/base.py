"""Shared host adapter behavior."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from agent_efficiency.contracts.effects import CanonicalEffect
from agent_efficiency.contracts.events import CanonicalEvent


class HostAdapter(Protocol):
    """Normalize native events and render native effects."""

    host: str
    native_events: frozenset[str]

    def normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def to_event(self, payload: dict[str, Any]) -> CanonicalEvent | None: ...

    def render(
        self, effect: CanonicalEffect, *, native_event: str
    ) -> dict[str, Any] | None: ...


class BaseAdapter:
    """Common safe normalization for native hosts."""

    host = "unknown"
    event_map: dict[str, str] = {}
    capabilities: dict[str, tuple[str, ...]] = {}
    native_events: frozenset[str] = frozenset()

    def __init__(self, *, host: str | None = None) -> None:
        if host:
            self.host = host

    def normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(payload)

    def to_event(self, payload: dict[str, Any]) -> CanonicalEvent | None:
        normalized = self.normalize_payload(payload)
        native_event = str(payload.get("hook_event_name") or "")
        name = self.event_map.get(native_event)
        session_id = str(normalized.get("session_id") or "").strip()
        if not name or not session_id:
            return None
        cwd = str(normalized.get("cwd") or Path.cwd())
        project_name = Path(cwd).name or "workspace"
        project_id = hashlib.sha256(cwd.encode("utf-8")).hexdigest()
        return CanonicalEvent(
            host=self.host,
            host_version=self._host_version(payload),
            name=name,
            session_id=session_id,
            turn_id=self._first_text(
                normalized, "turn_id", "prompt_id", "generation_id"
            ),
            event_id=self._first_text(normalized, "event_id", "tool_use_id"),
            project_id=project_id,
            project_name=project_name,
            model=self._model(normalized.get("model")),
            occurred_at=self._occurred_at(payload),
            facts=self._safe_facts(normalized),
            capabilities=self.capabilities.get(native_event, ()),
        )

    def render(
        self, effect: CanonicalEffect, *, native_event: str
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    @staticmethod
    def _first_text(payload: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _model(value: Any) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return BaseAdapter._first_text(value, "id", "display_name")
        return None

    @staticmethod
    def _occurred_at(payload: dict[str, Any]) -> str:
        value = payload.get("timestamp")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return datetime.now(UTC).isoformat()

    def _host_version(self, payload: dict[str, Any]) -> str | None:
        return self._first_text(payload, f"{self.host}_version", "host_version")

    @staticmethod
    def _safe_facts(payload: dict[str, Any]) -> dict[str, Any]:
        facts: dict[str, Any] = {}
        tool_name = payload.get("tool_name")
        if isinstance(tool_name, str) and tool_name:
            facts["tool_class"] = BaseAdapter._tool_class(tool_name)
        duration = payload.get("duration_ms")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            facts["duration_ms"] = max(0, int(duration))
        return facts

    @staticmethod
    def _tool_class(tool_name: str) -> str:
        normalized = tool_name.casefold()
        classes = (
            (("shell", "bash", "exec", "terminal", "command"), "shell"),
            (("write", "edit", "patch", "notebook"), "edit"),
            (("read", "view", "open"), "read"),
            (("grep", "glob", "search", "find"), "search"),
            (("task", "agent", "subagent"), "subagent"),
            (("web", "http", "fetch", "browser"), "network"),
        )
        for needles, tool_class in classes:
            if any(needle in normalized for needle in needles):
                return tool_class
        return "other"
