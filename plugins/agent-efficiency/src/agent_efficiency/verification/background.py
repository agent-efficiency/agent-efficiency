"""Managed background verification: commands/output are never written to telemetry."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from agent_efficiency import __version__
from agent_efficiency.store import Store, project_identity, utc_now
from agent_efficiency.verification.config import load_project_config
from agent_efficiency.verification.jobs import JobLedger
from agent_efficiency.verification.receipts import VerificationReceipt
from agent_efficiency.verification.runner import _run_one


def failure_receipt(job, reason="not_started", result="blocked"):
    return VerificationReceipt(
        check_id=job["check_id"],
        check_digest=job["check_digest"],
        workspace_digest=job["workspace_digest"],
        git_commit=job["git_commit"],
        dirty=bool(job["dirty"]),
        started_at=job["started_at"],
        finished_at=utc_now(),
        duration_ms=0,
        exit_code=None,
        result=result,
        reason_code=reason,
        runner_version=__version__,
        host="cli",
    ).as_dict()


def launch(config, check, store, *, output=None):
    ledger = JobLedger(store)
    job, reused = ledger.reserve(config, check)
    if reused:
        return {"job": job, "reused": True}
    handle = None
    process = None
    try:
        # An explicit user-owned log is outside telemetry. Never truncate an existing file.
        if output is not None:
            fd = os.open(
                Path(output).expanduser(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            handle = os.fdopen(fd, "wb")
        process = subprocess.Popen(
            [sys.executable, "-m", __name__],
            stdin=subprocess.PIPE,
            stdout=handle if handle else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        payload = json.dumps(
            {
                "root": str(config.root),
                "data": str(store.paths.root),
                "job_id": job["job_id"],
            }
        ).encode()
        process.stdin.write(payload)
        process.stdin.close()
    except OSError:
        if process is not None:
            process.kill()
            process.wait()
        if ledger.claim(job["job_id"]):
            ledger.finish(job["job_id"], failure_receipt(job))
    finally:
        if handle is not None:
            handle.close()
    return {"job": ledger.get(job["job_id"]), "reused": False}


def execute(command, *, cwd, env, check, timeout):
    del check
    process = subprocess.Popen(
        command, cwd=cwd, env=env, start_new_session=os.name == "posix"
    )
    try:
        return subprocess.CompletedProcess(command, process.wait(timeout=timeout))
    except BaseException:
        # A completed wait may have reaped the child before interruption. Its
        # numeric group ID can then be reused; never signal that old identity.
        if process.returncode is None:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
        process.wait()
        raise


def worker(payload):
    store = Store(payload["data"])
    ledger = JobLedger(store)
    job = ledger.get(payload["job_id"])
    if project_identity(payload["root"])[0] != job["project_key"]:
        raise ValueError("verification job project mismatch")
    if not ledger.claim(job["job_id"]):
        return
    try:
        config = load_project_config(payload["root"])
        check = config.get(job["check_id"])
        if check is None:
            receipt = failure_receipt(job, "workspace_changed", "inconclusive")
        else:
            receipt = _run_one(
                config,
                check,
                store,
                host="cli",
                session_id=None,
                turn_id=None,
                persist=False,
                expected_workspace=job["workspace_digest"],
                expected_identity=job["check_digest"],
                executor=execute,
            )
        ledger.finish(job["job_id"], receipt)
    except KeyboardInterrupt:
        ledger.cancel(job["job_id"])
    except (ValueError, OSError):
        ledger.finish(
            job["job_id"], failure_receipt(job, "workspace_unknown", "inconclusive")
        )


def main():
    # Treat SIGTERM like interruption so a managed command's descendants are stopped.
    signal.signal(
        signal.SIGTERM,
        lambda _signum, _frame: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    raw = sys.stdin.buffer.read(16385)
    if len(raw) > 16384:
        raise ValueError("verification worker payload too large")
    worker(json.loads(raw))


if __name__ == "__main__":
    main()
