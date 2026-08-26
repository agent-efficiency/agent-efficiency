"""Safe canonical lifecycle events."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


EVENT_NAMES = (
    "session.opened",
    "session.closed",
    "turn.prompted",
    "action.proposed",
    "action.completed",
    "action.failed",
    "file.changed",
    "approval.requested",
    "subagent.opened",
    "subagent.closed",
    "context.compacting",
    "context.compacted",
    "turn.stopping",
)


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    """One host event reduced to safe cross-host facts."""

    host: str
    name: str
    session_id: str
    project_id: str
    project_name: str
    occurred_at: str
    schema_version: int = 1
    host_version: str | None = None
    turn_id: str | None = None
    event_id: str | None = None
    model: str | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.name not in EVENT_NAMES:
            raise ValueError(f"unknown canonical event: {self.name}")
        if not self.host or not self.session_id:
            raise ValueError("canonical events require host and session identity")
        object.__setattr__(self, "facts", MappingProxyType(dict(self.facts)))
