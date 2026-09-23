"""Cursor lifecycle adapter."""

from __future__ import annotations

import json
from typing import Any

from agent_efficiency.adapters.base import BaseAdapter
from agent_efficiency.contracts.effects import CanonicalEffect


class CursorAdapter(BaseAdapter):
    host = "cursor"
    event_map = {
        "sessionStart": "session.opened",
        "sessionEnd": "session.closed",
        "beforeSubmitPrompt": "turn.prompted",
        "preToolUse": "action.proposed",
        "postToolUse": "action.completed",
        "postToolUseFailure": "action.failed",
        "beforeShellExecution": "action.proposed",
        "afterShellExecution": "action.completed",
        "afterFileEdit": "file.changed",
        "subagentStart": "subagent.opened",
        "subagentStop": "subagent.closed",
        "preCompact": "context.compacting",
        "stop": "turn.stopping",
    }
    native_events = frozenset(event_map)
    capabilities = {
        "sessionStart": ("add_context",),
        "beforeSubmitPrompt": ("notify",),
        "preToolUse": ("deny_action",),
        "beforeShellExecution": ("deny_action",),
        "postToolUse": ("add_context",),
        "afterShellExecution": ("add_context",),
        "afterFileEdit": ("add_context",),
        "subagentStart": ("deny_action",),
        "subagentStop": ("continue_turn",),
        "preCompact": ("add_context",),
        "stop": ("continue_turn",),
        # The shared runtime uses normalized event names after input parsing.
        "SessionStart": ("add_context",),
        "PreToolUse": ("deny_action",),
        "PostToolUse": ("add_context",),
        "PostToolUseFailure": ("add_context",),
        "PreCompact": ("add_context",),
        "Stop": ("continue_turn",),
        "SubagentStop": ("continue_turn",),
    }

    def normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        native_event = str(payload.get("hook_event_name") or "")
        legacy_events = {
            "sessionStart": "SessionStart",
            "sessionEnd": "SessionEnd",
            "beforeSubmitPrompt": "UserPromptSubmit",
            "preToolUse": "PreToolUse",
            "postToolUse": "PostToolUse",
            "postToolUseFailure": "PostToolUseFailure",
            "beforeShellExecution": "PreToolUse",
            "afterShellExecution": "PostToolUse",
            "afterFileEdit": "PostToolUse",
            "subagentStart": "SubagentStart",
            "subagentStop": "SubagentStop",
            "preCompact": "PreCompact",
            "stop": "Stop",
        }
        normalized["hook_event_name"] = legacy_events.get(native_event, native_event)
        normalized["session_id"] = str(
            payload.get("session_id") or payload.get("conversation_id") or ""
        )
        if native_event in {"beforeShellExecution", "afterShellExecution"}:
            normalized["tool_name"] = "Shell"
            normalized["tool_input"] = {"command": payload.get("command", "")}
        elif native_event == "afterFileEdit":
            normalized["tool_name"] = "Write"
            normalized["tool_input"] = {"file_path": payload.get("file_path", "")}
            normalized.setdefault("tool_response", {"success": True})
        if not normalized.get("cwd"):
            roots = payload.get("workspace_roots")
            if isinstance(roots, list) and roots and isinstance(roots[0], str):
                normalized["cwd"] = roots[0]
        if "duration_ms" not in normalized and isinstance(
            payload.get("duration"), (int, float)
        ):
            normalized["duration_ms"] = payload["duration"]
        if "tool_response" not in normalized and "tool_output" in payload:
            value = payload.get("tool_output")
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (TypeError, ValueError):
                    pass
            normalized["tool_response"] = value
        return normalized

    def render(
        self, effect: CanonicalEffect, *, native_event: str
    ) -> dict[str, Any] | None:
        if effect.effect == "none":
            return None
        message = f"Agent Efficiency: {effect.agent_message}"
        if effect.effect == "continue_turn":
            return {"followup_message": message}
        if effect.effect == "deny_action":
            return {
                "permission": "deny",
                "user_message": effect.user_message or message,
            }
        if effect.effect == "notify":
            return {"continue": False, "user_message": effect.user_message or message}
        if native_event in {
            "sessionStart",
            "SessionStart",
            "postToolUse",
            "PostToolUse",
            "afterShellExecution",
            "afterFileEdit",
            "preCompact",
            "PreCompact",
        }:
            return {"additional_context": message}
        return None
