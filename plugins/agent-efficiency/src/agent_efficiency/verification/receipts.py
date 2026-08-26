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

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


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
