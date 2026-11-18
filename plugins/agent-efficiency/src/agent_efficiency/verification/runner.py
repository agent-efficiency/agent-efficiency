"""Run configured checks without a shell and record bounded receipts."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import UTC, datetime
from typing import Iterable

from agent_efficiency import __version__
from agent_efficiency.store import Store, project_identity
from agent_efficiency.verification.config import CheckDefinition, ProjectConfig
from agent_efficiency.verification.receipts import VerificationReceipt, receipt_state
from agent_efficiency.verification.environment import check_identity
from agent_efficiency.verification.state import workspace_state
from agent_efficiency.verification.results import (
    classify_summary,
    file_signature,
    read_summary,
)


def run_checks(
    config: ProjectConfig,
    checks: Iterable[CheckDefinition],
    store: Store,
    *,
    host: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    reuse_current: bool = False,
) -> list[dict]:
    results = []
    for check in checks:
        if reuse_current:
            from agent_efficiency.verification.prerequisites import prerequisite_plan

            state = workspace_state(config.root, config.digest)
            key, _ = project_identity(str(config.root))
            previous = store.verification_receipts(key, check_id=check.check_id)
            latest = previous[0] if previous else None
            if (
                receipt_state(
                    latest,
                    check_digest=check_identity(check, store),
                    workspace_digest=state.digest,
                    max_age_seconds=check.max_age_seconds,
                )
                == "current"
                and prerequisite_plan(config, [check], store)["ready"]
            ):
                results.append({**latest, "receipt_id": latest["id"], "reused": True})
                continue
        results.append(
            _run_one(
                config,
                check,
                store,
                host=host,
                session_id=session_id,
                turn_id=turn_id,
            )
        )
    return results


def _run_one(
    config: ProjectConfig,
    check: CheckDefinition,
    store: Store,
    *,
    host: str | None,
    session_id: str | None,
    turn_id: str | None,
    persist: bool = True,
    expected_workspace: str | None = None,
    expected_identity: str | None = None,
    executor=None,
) -> dict:
    before = workspace_state(config.root, config.digest)
    identity = check_identity(check, store)
    result_path = config.root / check.result_file if check.result_file else None
    previous_summary = file_signature(result_path) if result_path else None
    summary = None
    started = _now()
    started_ns = time.perf_counter_ns()
    exit_code: int | None = None
    reason: str | None = None
    if before.result != "ready":
        result = "inconclusive"
        reason = "workspace_unknown"
    elif identity is None:
        result = "inconclusive"
        reason = "environment_unknown"
    elif expected_workspace is not None and before.digest != expected_workspace:
        result, reason = "inconclusive", "workspace_changed"
    elif expected_identity is not None and identity != expected_identity:
        result, reason = "inconclusive", "environment_changed"
    else:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            # A Python check would otherwise write bytecode caches into the
            # workspace it is checking.
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        for name in check.env_allowlist:
            if name in os.environ:
                environment[name] = os.environ[name]
        try:
            completed = (executor or subprocess.run)(
                list(check.command),
                cwd=config.root,
                env=environment,
                check=False,
                timeout=check.timeout_seconds,
            )
            exit_code = completed.returncode
            result = "pass" if exit_code == 0 else "fail"
            if exit_code != 0:
                reason = "exit_nonzero"
        except subprocess.TimeoutExpired:
            result = "blocked"
            reason = "timeout"
        except OSError:
            result = "blocked"
            reason = "not_started"
    if result_path is not None and exit_code is not None:
        try:
            if not result_path.resolve().is_relative_to(config.root):
                raise ValueError("result path escapes project")
            summary = read_summary(result_path, previous_summary)
            structured_result, structured_reason = classify_summary(summary)
            if structured_result != "pass":
                result, reason = structured_result, structured_reason
        except (ValueError, OSError):
            result, reason = "inconclusive", "structured_invalid"
    after = workspace_state(config.root, config.digest)
    if result == "pass" and after.result != "ready":
        result = "inconclusive"
        reason = "workspace_unreadable"
    elif result == "pass" and after.digest != before.digest:
        result = "inconclusive"
        reason = "workspace_changed"
    final_identity = check_identity(check, store)
    if result == "pass" and final_identity is None:
        result, reason = "inconclusive", "environment_unknown"
    elif result == "pass" and final_identity != identity:
        result, reason = "inconclusive", "environment_changed"
    receipt = VerificationReceipt(
        check_id=check.check_id,
        check_digest=identity or check.digest,
        workspace_digest=after.digest,
        git_commit=after.commit,
        dirty=after.dirty,
        started_at=started,
        finished_at=_now(),
        duration_ms=(time.perf_counter_ns() - started_ns) // 1_000_000,
        exit_code=exit_code,
        result=result,
        runner_version=__version__,
        host=host,
        session_id=session_id,
        turn_id=turn_id,
        reason_code=reason,
    )
    data = receipt.as_dict()
    if summary is not None:
        data["summary"] = summary
    if not persist:
        return data
    project_key, _ = project_identity(str(config.root))
    receipt_id = store.record_verification_receipt(data, project_key=project_key)
    return {"receipt_id": receipt_id, **data}


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
