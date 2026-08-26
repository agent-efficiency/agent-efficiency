"""Claude Code lifecycle adapter."""

from __future__ import annotations

from typing import Any

from agent_efficiency.adapters.base import BaseAdapter
from agent_efficiency.contracts.effects import CanonicalEffect


class ClaudeAdapter(BaseAdapter):
    host = "claude"
    event_map = {
        "SessionStart": "session.opened",
        "SessionEnd": "session.closed",
        "UserPromptSubmit": "turn.prompted",
        "PreToolUse": "action.proposed",
        "PostToolUse": "action.completed",
        "PostToolUseFailure": "action.failed",
        "PostToolBatch": "action.completed",
        "SubagentStart": "subagent.opened",
        "SubagentStop": "subagent.closed",
        "PreCompact": "context.compacting",
        "PostCompact": "context.compacted",
        "Stop": "turn.stopping",
    }
    native_events = frozenset(event_map)
    capabilities = {
        "SessionStart": ("add_context",),
        "UserPromptSubmit": ("add_context", "notify"),
        "PreToolUse": ("add_context", "deny_action"),
        "PostToolUse": ("add_context",),
        "PostToolUseFailure": ("add_context",),
        "PostToolBatch": ("add_context",),
        "PreCompact": ("add_context",),
        "PostCompact": ("add_context",),
        "SubagentStop": ("continue_turn",),
        "Stop": ("continue_turn",),
    }

    def render(
        self, effect: CanonicalEffect, *, native_event: str
    ) -> dict[str, Any] | None:
        if effect.effect == "none":
            return None
        message = f"Agent Efficiency: {effect.agent_message}"
        if effect.effect == "continue_turn":
            return {"decision": "block", "reason": message}
        if effect.effect == "deny_action":
            return {
                "hookSpecificOutput": {
                    "hookEventName": native_event,
                    "permissionDecision": "deny",
                    "permissionDecisionReason": message,
                }
            }
        if effect.effect == "notify":
            return {"continue": False, "stopReason": effect.user_message or message}
        return {
            "hookSpecificOutput": {
                "hookEventName": native_event,
                "additionalContext": message,
            }
        }
