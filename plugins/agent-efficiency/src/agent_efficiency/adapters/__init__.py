"""Native host adapters."""

from agent_efficiency.adapters.base import HostAdapter
from agent_efficiency.adapters.claude import ClaudeAdapter
from agent_efficiency.adapters.codex import CodexAdapter
from agent_efficiency.adapters.cursor import CursorAdapter


ADAPTERS = {
    "claude": ClaudeAdapter(),
    "codex": CodexAdapter(),
    "cursor": CursorAdapter(),
}


def adapter_for(host: str) -> HostAdapter:
    """Return the adapter for a detected host."""
    return ADAPTERS.get(host, CodexAdapter(host="unknown"))


__all__ = [
    "ClaudeAdapter",
    "CodexAdapter",
    "CursorAdapter",
    "HostAdapter",
    "adapter_for",
]
