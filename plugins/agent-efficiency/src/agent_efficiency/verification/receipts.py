"""Verification receipt records and freshness checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class VerificationReceipt:
    check_id: str
    check_digest: str
    workspace_digest: str | None
    git_commit: str | None
    dirty: bool
    started_at: str
    finished_at: str
    duration_ms: int
    exit_code: int | None
    result: str
    runner_version: str
    host: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    schema_version: int = 1
    reason_code: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# Why a check result is not a pass. The code is stored with the receipt; the
# sentence is written from it when the receipt is shown, so no command text or
# output is ever stored.
REASONS = {
    "exit_nonzero": "The check command exited with status {exit_code}.",
    "timeout": "The check command did not finish within its time limit.",
    "not_started": (
        "The check command could not be started. Check that the program "
        "exists on PATH."
    ),
    "workspace_unknown": (
        "The workspace state could not be read before the check, so the result "
        "cannot be tied to it. Run checks at the root of a git repository with "
        "at least one commit."
    ),
    "workspace_changed": (
        "The check changed files in the workspace, so its result does not "
        "describe the files as they were checked. Commit or ignore the files "
        "the check writes, then run it again."
    ),
    "workspace_unreadable": (
        "The workspace state could not be read after the check, so the result "
        "cannot be tied to it."
    ),
}


def reason_text(receipt: dict[str, Any]) -> str | None:
    """Return one plain sentence for why a receipt is not a pass."""

    if receipt.get("result") == "pass":
        return None
    code = receipt.get("reason_code")
    template = REASONS.get(str(code)) if code else None
    if template is None:
        return "No reason was recorded; the receipt predates recorded reasons."
    return template.format(exit_code=receipt.get("exit_code"))


def receipt_state(
    receipt: dict[str, Any] | None,
    *,
    check_digest: str,
    workspace_digest: str | None,
    max_age_seconds: int | None = None,
    now: datetime | None = None,
) -> str:
    if receipt is None:
        return "missing"
    result = str(receipt.get("result") or "inconclusive")
    if result != "pass":
        return result
    if workspace_digest is None:
        return "inconclusive"
    if receipt.get("check_digest") != check_digest:
        return "stale"
    if receipt.get("workspace_digest") != workspace_digest:
        return "stale"
    if max_age_seconds is not None:
        try:
            finished = datetime.fromisoformat(
                str(receipt.get("finished_at") or "").replace("Z", "+00:00")
            ).astimezone(UTC)
        except ValueError:
            return "inconclusive"
        current = now or datetime.now(UTC)
        if (current - finished).total_seconds() > max_age_seconds:
            return "stale"
    return "current"
