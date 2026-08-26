"""Codex lifecycle adapter."""

from __future__ import annotations

from typing import Any

from agent_efficiency.adapters.claude import ClaudeAdapter
from agent_efficiency.contracts.effects import CanonicalEffect


class CodexAdapter(ClaudeAdapter):
    host = "codex"
    event_map = {
        **ClaudeAdapter.event_map,
        "PermissionRequest": "approval.requested",
    }
    native_events = frozenset(event_map)
    capabilities = {
        **ClaudeAdapter.capabilities,
        "PermissionRequest": ("notify", "deny_action"),
        "PreToolUse": ("add_context", "deny_action", "replace_action"),
    }

    def render(
        self, effect: CanonicalEffect, *, native_event: str
    ) -> dict[str, Any] | None:
        return super().render(effect, native_event=native_event)
