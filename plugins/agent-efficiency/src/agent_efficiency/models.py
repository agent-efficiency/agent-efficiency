"""Small allocation-light records shared by policy, hook, and reports."""

from __future__ import annotations

VALID_MODES = ("off", "observe", "advise", "guard")


class TaskFacts:
    __slots__ = ("task_type", "risk", "substantial", "prompt_chars")

    def __init__(
        self, task_type: str, risk: str, substantial: bool, prompt_chars: int
    ) -> None:
        self.task_type = task_type
        self.risk = risk
        self.substantial = substantial
        self.prompt_chars = prompt_chars


class ToolFacts:
    __slots__ = (
        "tool_name",
        "command_class",
        "signature",
        "is_edit",
        "is_code_edit",
        "is_validation",
        "is_subagent",
        "is_broad_scan",
        "file_extensions",
        "safe_details",
    )

    def __init__(
        self,
        tool_name: str,
        command_class: str,
        signature: str,
        *,
        is_edit: bool = False,
        is_code_edit: bool = False,
        is_validation: bool = False,
        is_subagent: bool = False,
        is_broad_scan: bool = False,
        file_extensions: tuple[str, ...] = (),
        safe_details: dict | None = None,
    ) -> None:
        self.tool_name = tool_name
        self.command_class = command_class
        self.signature = signature
        self.is_edit = is_edit
        self.is_code_edit = is_code_edit
        self.is_validation = is_validation
        self.is_subagent = is_subagent
        self.is_broad_scan = is_broad_scan
        self.file_extensions = file_extensions
        self.safe_details = safe_details or {}


class Nudge:
    __slots__ = ("policy_id", "message")

    def __init__(self, policy_id: str, message: str) -> None:
        self.policy_id = policy_id
        self.message = message
