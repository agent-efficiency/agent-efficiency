"""Managed job lifecycle never mistakes launch success for verification success."""

import unittest

import test_verification
from agent_efficiency.store import project_identity
from agent_efficiency.verification.config import load_project_config


class JobLifecycleTests(unittest.TestCase):
    setUp = test_verification.VerificationTests.setUp

    def jobs(self):
        from agent_efficiency.verification.jobs import JobLedger

        return JobLedger(self.store)

    def start(self):
        config = load_project_config(self.root)
        return self.jobs().reserve(config, config.checks[0])

    def test_launch_is_pending_and_never_a_passing_receipt(self):
        job, reused = self.start()
        self.assertFalse(reused)
        self.assertEqual(job["state"], "pending")
        key, _ = project_identity(str(self.root))
        self.assertEqual(self.store.verification_receipts(key), [])

    def test_duplicate_launch_reuses_current_job(self):
        first, _ = self.start()
        second, reused = self.start()
        self.assertTrue(reused)
        self.assertEqual(first["job_id"], second["job_id"])

    def test_only_one_worker_can_claim_job(self):
        job, _ = self.start()
        self.assertTrue(self.jobs().claim(job["job_id"]))
        self.assertFalse(self.jobs().claim(job["job_id"]))

    def receipt(self, job):
        from agent_efficiency.verification.receipts import VerificationReceipt

        return VerificationReceipt(
            check_id=job["check_id"],
            check_digest=job["check_digest"],
            workspace_digest=job["workspace_digest"],
            git_commit=job["git_commit"],
            dirty=bool(job["dirty"]),
            started_at=job["started_at"],
            finished_at=job["started_at"],
            duration_ms=0,
            exit_code=0,
            result="pass",
            runner_version="test",
        ).as_dict()

    def test_duplicate_completion_creates_one_receipt(self):
        job, _ = self.start()
        self.jobs().claim(job["job_id"])
        first = self.jobs().finish(job["job_id"], self.receipt(job))
        second = self.jobs().finish(job["job_id"], self.receipt(job))
        self.assertEqual(first, second)
        self.assertEqual(first["state"], "pass")
        key, _ = project_identity(str(self.root))
        self.assertEqual(len(self.store.verification_receipts(key)), 1)

    def test_terminal_cancellation_cannot_be_overwritten_by_late_success(self):
        job, _ = self.start()
        self.jobs().claim(job["job_id"])
        self.jobs().cancel(job["job_id"])
        result = self.jobs().finish(job["job_id"], self.receipt(job))
        self.assertEqual(result["state"], "killed")
        key, _ = project_identity(str(self.root))
        self.assertEqual(self.store.verification_receipts(key), [])

    def test_changed_launch_binding_cannot_create_a_pass(self):
        job, _ = self.start()
        self.jobs().claim(job["job_id"])
        receipt = {**self.receipt(job), "workspace_digest": "different"}
        result = self.jobs().finish(job["job_id"], receipt)
        self.assertEqual(result["state"], "inconclusive")
        saved = self.store.verification_receipt(result["receipt_id"])
        self.assertEqual(saved["reason_code"], "workspace_changed")

    def worker(self, job):
        from agent_efficiency.verification.background import worker

        worker(
            {
                "root": str(self.root),
                "data": str(self.store.paths.root),
                "job_id": job["job_id"],
            }
        )

    def test_worker_records_real_command_result_only_after_completion(self):
        job, _ = self.start()
        self.worker(job)
        result = self.jobs().get(job["job_id"])
        self.assertEqual(result["state"], "pass")
        self.assertEqual(
            self.store.verification_receipt(result["receipt_id"])["exit_code"], 0
        )
        self.worker(job)
        key, _ = project_identity(str(self.root))
        self.assertEqual(len(self.store.verification_receipts(key)), 1)

    def test_worker_nonzero_is_failed_not_successful_launch(self):
        path = self.root / "agent-efficiency.toml"
        path.write_text(path.read_text().replace("SystemExit(0)", "SystemExit(4)"))
        job, _ = self.start()
        self.worker(job)
        result = self.jobs().get(job["job_id"])
        self.assertEqual(result["state"], "fail")
        self.assertEqual(
            self.store.verification_receipt(result["receipt_id"])["exit_code"], 4
        )

    def test_workspace_changed_before_worker_never_runs_command(self):
        from unittest.mock import patch

        job, _ = self.start()
        (self.root / "changed.py").write_text("changed = True\n")
        with patch("agent_efficiency.verification.background.execute") as execute:
            self.worker(job)
        execute.assert_not_called()
        self.assertEqual(self.jobs().get(job["job_id"])["state"], "inconclusive")

    def test_overdue_job_is_unknown_not_passed_or_automatically_relaunched(self):
        job, _ = self.start()
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE verification_jobs SET deadline_at='2000-01-01T00:00:00+00:00'"
            )
        key, _ = project_identity(str(self.root))
        self.assertEqual(self.jobs().report(key)[0]["observed_state"], "unknown")
        repeated, reused = self.start()
        self.assertTrue(reused)
        self.assertEqual(job["job_id"], repeated["job_id"])

    def test_task_state_survives_reopen_without_work_content(self):
        job, _ = self.start()
        same = self.jobs().get(job["job_id"])
        self.assertEqual(job, same)
        self.assertNotIn(str(self.root), str(same))
        self.assertNotIn("command", same)
        self.assertNotIn("output", same)
