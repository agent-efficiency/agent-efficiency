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
from agent_efficiency.verification.receipts import VerificationReceipt
from agent_efficiency.verification.state import workspace_state


def run_checks(
    config: ProjectConfig,
    checks: Iterable[CheckDefinition],
    store: Store,
    *,
    host: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> list[dict]:
    results = []
    for check in checks:
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
) -> dict:
    before = workspace_state(config.root, config.digest)
    started = _now()
    started_ns = time.perf_counter_ns()
    exit_code: int | None = None
    reason: str | None = None
    if before.result != "ready":
        result = "inconclusive"
        reason = "workspace_unknown"
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
            completed = subprocess.run(
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
    after = workspace_state(config.root, config.digest)
    if result == "pass" and after.result != "ready":
        result = "inconclusive"
        reason = "workspace_unreadable"
    elif result == "pass" and after.digest != before.digest:
        result = "inconclusive"
        reason = "workspace_changed"
    receipt = VerificationReceipt(
        check_id=check.check_id,
        check_digest=check.digest,
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
    project_key, _ = project_identity(str(config.root))
    receipt_id = store.record_verification_receipt(
        receipt.as_dict(), project_key=project_key
    )
    return {"receipt_id": receipt_id, **receipt.as_dict()}


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
