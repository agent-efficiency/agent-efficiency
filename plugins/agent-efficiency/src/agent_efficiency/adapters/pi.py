"""Pi's opt-in observe-only bridge. No action-changing capabilities."""

from agent_efficiency.adapters.base import BaseAdapter


class PiAdapter(BaseAdapter):
    host = "pi"
    event_map = {
        "SessionStart": "session.opened",
        "SessionEnd": "session.closed",
        "UserPromptSubmit": "turn.prompted",
        "PreToolUse": "action.proposed",
        "PostToolUse": "action.completed",
        "PostToolUseFailure": "action.failed",
        "PreCompact": "context.compacting",
        "PostCompact": "context.compacted",
        "Stop": "turn.stopping",
    }
    native_events = frozenset(event_map)
    capabilities = {}

    def render(self, effect, *, native_event):
        return None
