"""Transactional managed-check lifecycle. Pending activity is never proof."""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime, timedelta

from agent_efficiency.store import Store, project_identity, utc_now
from agent_efficiency.verification.environment import check_identity
from agent_efficiency.verification.state import workspace_state

_JOB = re.compile(r"^[0-9a-f]{32}$")
TERMINAL = frozenset({"pass", "fail", "blocked", "inconclusive", "killed"})
DDL = """
CREATE TABLE IF NOT EXISTS verification_jobs (
    job_id TEXT PRIMARY KEY CHECK(length(job_id)=32 AND job_id NOT GLOB '*[^0-9a-f]*'),
    project_key TEXT NOT NULL,
    check_id TEXT NOT NULL,
    check_digest TEXT NOT NULL,
    workspace_digest TEXT NOT NULL,
    git_commit TEXT,
    dirty INTEGER NOT NULL CHECK(dirty IN (0,1)),
    state TEXT NOT NULL CHECK(state IN ('pending','running','pass','fail','blocked','inconclusive','killed')),
    started_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    finished_at TEXT,
    receipt_id INTEGER,
    FOREIGN KEY(receipt_id) REFERENCES verification_receipts(id)
)
"""


class JobLedger:
    def __init__(self, store: Store):
        self.store = store
        store.ensure_current_schema()
        with store.connect() as conn:
            conn.execute(DDL)
            conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS verification_jobs_active
                ON verification_jobs(project_key, check_id, check_digest, workspace_digest)
                WHERE state IN ('pending','running')""")

    def reserve(self, config, check):
        if config.get(check.check_id) != check:
            raise ValueError("check is not configured for this project")
        state = workspace_state(config.root, config.digest)
        identity = check_identity(check, self.store)
        if state.result != "ready" or identity is None:
            raise ValueError(
                "cannot bind background check to workspace and environment"
            )
        project, _ = project_identity(str(config.root))
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """SELECT * FROM verification_jobs
                WHERE project_key=? AND check_id=? AND check_digest=? AND workspace_digest=?
                AND state IN ('pending','running')""",
                (project, check.check_id, identity, state.digest),
            ).fetchone()
            if existing:
                return dict(existing), True
            job_id = secrets.token_hex(16)
            conn.execute(
                """INSERT INTO verification_jobs
                (job_id,project_key,check_id,check_digest,workspace_digest,git_commit,dirty,state,started_at,deadline_at)
                VALUES (?,?,?,?,?,?,?,'pending',?,?)""",
                (
                    job_id,
                    project,
                    check.check_id,
                    identity,
                    state.digest,
                    state.commit,
                    int(state.dirty),
                    utc_now(),
                    (
                        datetime.now(UTC)
                        + timedelta(seconds=check.timeout_seconds + 120)
                    ).isoformat(),
                ),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM verification_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
            ), False

    @staticmethod
    def validate_id(job_id):
        if not isinstance(job_id, str) or not _JOB.fullmatch(job_id):
            raise ValueError("invalid verification job ID")

    def get(self, job_id):
        self.validate_id(job_id)
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM verification_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise ValueError("unknown verification job")
        return dict(row)

    def claim(self, job_id):
        self.validate_id(job_id)
        with self.store.connect() as conn:
            cursor = conn.execute(
                "UPDATE verification_jobs SET state='running' WHERE job_id=? AND state='pending'",
                (job_id,),
            )
            return cursor.rowcount == 1

    def cancel(self, job_id):
        """Record a trusted terminal cancellation; this does not send an OS signal."""
        self.validate_id(job_id)
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE verification_jobs SET state='killed',finished_at=? WHERE job_id=? AND state IN ('pending','running')",
                (utc_now(), job_id),
            )
        return self.get(job_id)

    def finish(self, job_id, receipt):
        self.validate_id(job_id)
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM verification_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown verification job")
            job = dict(row)
            if job["state"] in TERMINAL:
                return job
            if job["state"] != "running":
                raise ValueError("unclaimed verification job cannot complete")
            if receipt["check_id"] != job["check_id"]:
                raise ValueError("verification job check mismatch")
            receipt = dict(receipt)
            if receipt["result"] == "pass":
                if receipt["workspace_digest"] != job["workspace_digest"]:
                    receipt.update(
                        result="inconclusive", reason_code="workspace_changed"
                    )
                elif receipt["check_digest"] != job["check_digest"]:
                    receipt.update(
                        result="inconclusive", reason_code="environment_changed"
                    )
            receipt_id = self.store.record_verification_receipt(
                receipt,
                project_key=job["project_key"],
                _connection=conn,
            )
            conn.execute(
                "UPDATE verification_jobs SET state=?,finished_at=?,receipt_id=? WHERE job_id=?",
                (receipt["result"], utc_now(), receipt_id, job_id),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM verification_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
            )

    def report(self, project_key):
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM verification_jobs WHERE project_key=? ORDER BY started_at,job_id",
                (project_key,),
            ).fetchall()
        result = []
        now = datetime.now(UTC)
        for row in rows:
            item = dict(row)
            item["observed_state"] = item["state"]
            if (
                item["state"] in {"pending", "running"}
                and datetime.fromisoformat(item["deadline_at"]) < now
            ):
                item["observed_state"] = "unknown"
            result.append(item)
        return result
