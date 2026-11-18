"""Real detached workers and transactional failure controls use temporary stores."""

import os
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest.mock import Mock, patch

import agent_efficiency
import test_verification_jobs
from agent_efficiency.store import project_identity
from agent_efficiency.verification.background import launch
from agent_efficiency.verification.config import load_project_config


class BackgroundVerificationTests(unittest.TestCase):
    setUp = test_verification_jobs.JobLifecycleTests.setUp
    jobs = test_verification_jobs.JobLifecycleTests.jobs
    start = test_verification_jobs.JobLifecycleTests.start
    receipt = test_verification_jobs.JobLifecycleTests.receipt

    def launch_and_wait(self, **options):
        config = load_project_config(self.root)
        processes = []
        original = subprocess.Popen

        def spawn(*args, **kwargs):
            process = original(*args, **kwargs)
            if self.is_worker(args[0]):
                processes.append(process)
            return process

        environment = {
            "PYTHONPATH": str(Path(agent_efficiency.__file__).resolve().parent.parent)
        }
        with (
            patch.dict(os.environ, environment),
            patch(
                "agent_efficiency.verification.background.subprocess.Popen",
                side_effect=spawn,
            ),
        ):
            result = launch(config, config.checks[0], self.store, **options)
        self.assertEqual(len(processes), 1)
        try:
            self.assertEqual(processes[0].wait(timeout=15), 0)
        finally:
            if processes[0].poll() is None:
                processes[0].kill()
                processes[0].wait()
        return self.jobs().get(result["job"]["job_id"])

    @staticmethod
    def is_worker(command):
        return len(command) >= 3 and command[1:3] == [
            "-m",
            "agent_efficiency.verification.background",
        ]

    def test_detached_worker_produces_real_passing_receipt(self):
        job = self.launch_and_wait()
        self.assertEqual(job["state"], "pass")
        self.assertEqual(
            self.store.verification_receipt(job["receipt_id"])["exit_code"], 0
        )

    def test_launch_failure_is_blocked_not_pending_or_passed(self):
        config = load_project_config(self.root)
        original = subprocess.Popen

        def spawn(command, **kwargs):
            if self.is_worker(command):
                raise OSError("synthetic worker launch failure")
            return original(command, **kwargs)

        with patch(
            "agent_efficiency.verification.background.subprocess.Popen",
            side_effect=spawn,
        ):
            result = launch(config, config.checks[0], self.store)
        job = result["job"]
        self.assertEqual(job["state"], "blocked")
        self.assertEqual(
            self.store.verification_receipt(job["receipt_id"])["reason_code"],
            "not_started",
        )

    def test_full_output_is_only_in_explicit_user_owned_log(self):
        path = self.root / "agent-efficiency.toml"
        path.write_text(
            path.read_text().replace("raise SystemExit(0)", "print(731968421047)")
        )
        output = Path(self.data.name) / "private-user-output.log"
        self.assertEqual(self.launch_and_wait(output=output)["state"], "pass")
        self.assertIn("731968421047", output.read_text())
        self.assertNotIn(b"731968421047", self.store.paths.database.read_bytes())
        self.assertNotIn(output.name.encode(), self.store.paths.database.read_bytes())
        if os.name == "posix":
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_existing_output_is_never_truncated(self):
        output = Path(self.data.name) / "existing.log"
        output.write_text("preserve me")
        config = load_project_config(self.root)
        original = subprocess.Popen

        def spawn(command, **kwargs):
            self.assertFalse(
                self.is_worker(command), "existing output must prevent worker launch"
            )
            return original(command, **kwargs)

        with patch(
            "agent_efficiency.verification.background.subprocess.Popen",
            side_effect=spawn,
        ):
            result = launch(config, config.checks[0], self.store, output=output)
        self.assertEqual(result["job"]["state"], "blocked")
        self.assertEqual(output.read_text(), "preserve me")

    def test_timeout_records_blocked_terminal_result(self):
        path = self.root / "agent-efficiency.toml"
        path.write_text(
            path.read_text()
            .replace("raise SystemExit(0)", "import time; time.sleep(10)")
            .replace("timeout_seconds = 30", "timeout_seconds = 1")
        )
        job = self.launch_and_wait()
        self.assertEqual(job["state"], "blocked")
        self.assertEqual(
            self.store.verification_receipt(job["receipt_id"])["reason_code"], "timeout"
        )

    @unittest.skipUnless(os.name == "posix", "POSIX process-group cleanup")
    def test_interruption_after_reaping_never_signals_old_process_group(self):
        from agent_efficiency.verification.background import execute

        process = Mock(pid=12345, returncode=0)
        process.wait.side_effect = [KeyboardInterrupt(), 0]
        with (
            patch(
                "agent_efficiency.verification.background.subprocess.Popen",
                return_value=process,
            ),
            patch("agent_efficiency.verification.background.os.killpg") as kill_group,
        ):
            with self.assertRaises(KeyboardInterrupt):
                execute(["test-command"], cwd=self.root, env={}, check=False, timeout=1)
        kill_group.assert_not_called()
        process.kill.assert_not_called()

    def test_terminal_update_failure_rolls_back_receipt_too(self):
        job, _ = self.start()
        self.jobs().claim(job["job_id"])
        with self.store.connect() as conn:
            conn.execute(
                "CREATE TRIGGER reject_terminal BEFORE UPDATE OF receipt_id ON verification_jobs BEGIN SELECT RAISE(ABORT, 'test rollback'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.jobs().finish(job["job_id"], self.receipt(job))
        key, _ = project_identity(str(self.root))
        self.assertEqual(self.store.verification_receipts(key), [])
        self.assertEqual(self.jobs().get(job["job_id"])["state"], "running")
