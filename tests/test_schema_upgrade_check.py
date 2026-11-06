from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from agent_efficiency.paths import PLUGIN_ROOT
from agent_efficiency.store import Store
from scripts import check_schema_upgrade as check


PLUGIN = Path("plugins/agent-efficiency")
STORE = PLUGIN / "src/agent_efficiency/store.py"


class SchemaUpgradeCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.fixture.cleanup)
        cls.fixture_repo = Path(cls.fixture.name) / "repo"
        cls.fixture_repo.mkdir()
        shutil.copytree(
            PLUGIN_ROOT,
            cls.fixture_repo / PLUGIN,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
        )
        # This local fixture tests the runner without requiring Git history in CI.
        # A separate release rehearsal uses the actual schema 7 source commit.
        source = cls.fixture_repo / STORE
        candidate = source.read_text()
        old = candidate.replace('SCHEMA_VERSION = "8"', 'SCHEMA_VERSION = "7"')
        old = old.replace(
            "                    resolves INTEGER NOT NULL DEFAULT 0 CHECK(resolves IN (0, 1)),\n",
            "",
        )
        source.write_text(old)
        cls.git(cls.fixture_repo, "init", "-q")
        cls.git(cls.fixture_repo, "add", ".")
        cls.git(cls.fixture_repo, "commit", "-qm", "test: schema 7 fixture")
        cls.baseline = cls.git(cls.fixture_repo, "rev-parse", "HEAD")
        source.write_text(candidate)
        cls.git(cls.fixture_repo, "add", ".")
        cls.git(cls.fixture_repo, "commit", "-qm", "test: schema 8 fixture")
        cls.candidate = cls.git(cls.fixture_repo, "rev-parse", "HEAD")

    @staticmethod
    def git(repo: Path, *arguments: str) -> str:
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        return subprocess.check_output(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *arguments],
            env=env,
            stderr=subprocess.PIPE,
            text=True,
        ).strip()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        shutil.copytree(self.fixture_repo, self.repo)
        self.artifacts = self.root / "attempt"

    def run_check(self, baseline: str | None = None) -> dict:
        return check.run_rehearsal(self.repo, baseline or self.baseline, self.artifacts)

    def commit(self) -> None:
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-qm", "test: change candidate fixture")

    def test_rehearsal_binds_sources_and_preserves_records_through_rollback(
        self,
    ) -> None:
        result = self.run_check()
        self.assertTrue(
            result["ok"], f"{result.get('failed_step')}: {result.get('error')}"
        )
        self.assertEqual(result["result"], "passed")
        self.assertEqual(result["inputs"]["baseline_commit"], self.baseline)
        self.assertEqual(result["inputs"]["candidate_commit"], self.candidate)
        snapshots = result["snapshots"]
        self.assertEqual(snapshots["baseline"]["schema"], "7")
        self.assertEqual(snapshots["migrated"]["schema"], "8")
        self.assertEqual(snapshots["restored"], snapshots["baseline"])
        self.assertGreater(
            snapshots["baseline"]["tables"]["vault_receipts"]["count"], 0
        )
        self.assertGreater(
            snapshots["baseline"]["tables"]["verification_receipts"]["count"], 0
        )
        self.assertEqual(
            snapshots["upgraded"]["tables"]["sessions"]["count"],
            snapshots["baseline"]["tables"]["sessions"]["count"] + 1,
        )
        self.assertTrue(all(step["status"] == "passed" for step in result["steps"]))
        self.assertTrue((self.artifacts / "upgraded/agent-efficiency.db").is_file())
        self.assertTrue((self.artifacts / "backup/rehearsal-marker.json").is_file())
        self.assertEqual(
            json.loads((self.artifacts / "result.json").read_text()), result
        )

    def test_existing_artifacts_are_never_replaced(self) -> None:
        self.artifacts.mkdir()
        marker = self.artifacts / "result.json"
        marker.write_text('{"result": "failed"}\n')
        with self.assertRaises(FileExistsError):
            self.run_check()
        self.assertEqual(marker.read_text(), '{"result": "failed"}\n')
        self.assertEqual(list(self.artifacts.iterdir()), [marker])

    def test_a_dangling_output_link_is_refused(self) -> None:
        target = self.root / "missing"
        self.artifacts.symlink_to(target)
        with self.assertRaises(FileExistsError):
            self.run_check()
        self.assertFalse(target.exists())

    def test_dirty_runtime_code_is_refused_and_retained(self) -> None:
        (self.repo / STORE).write_text("uncommitted fixture\n")
        result = self.run_check()
        self.assertEqual(result["result"], "failed")
        self.assertEqual(result["failed_step"], "resolve_inputs")
        self.assertIn("uncommitted", result["error"])
        self.assertTrue((self.artifacts / "result.json").is_file())
        self.assertFalse((self.artifacts / "baseline-data").exists())

    def test_an_untracked_runtime_file_is_refused(self) -> None:
        (self.repo / PLUGIN / "extra.py").write_text("# uncommitted fixture\n")
        self.assertEqual(self.run_check()["failed_step"], "resolve_inputs")

    def test_an_unknown_ref_is_a_retained_failure(self) -> None:
        result = self.run_check("missing-ref")
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_step"], "resolve_inputs")
        self.assertEqual(
            json.loads((self.artifacts / "result.json").read_text()), result
        )

    def test_a_schema_eight_baseline_cannot_pass(self) -> None:
        result = self.run_check(self.candidate)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_step"], "seed_baseline")
        self.assertIn("schema 7", result["error"])

    def test_migration_failure_keeps_logs_and_cannot_be_retried_in_place(self) -> None:
        cli = self.repo / PLUGIN / "src/agent_efficiency/cli.py"
        text = cli.read_text().replace(
            '    if args.command == "migrate":\n',
            '    if args.command == "migrate":\n'
            "        if args.apply:\n"
            '            raise RuntimeError("fixture migration failure")\n',
        )
        cli.write_text(text)
        self.commit()
        result = self.run_check()
        self.assertEqual(result["failed_step"], "migrate")
        self.assertFalse(result["ok"])
        log = self.artifacts / result["steps"][-1]["stderr"]
        self.assertIn("fixture migration failure", log.read_text())
        before = (self.artifacts / "result.json").read_bytes()
        with self.assertRaises(FileExistsError):
            self.run_check()
        self.assertEqual((self.artifacts / "result.json").read_bytes(), before)
        self.assertTrue((self.artifacts / "backup/agent-efficiency.db").is_file())

    def test_a_successful_exit_does_not_hide_lost_receipts(self) -> None:
        source = self.repo / STORE
        text = source.read_text().replace(
            "\n    def ensure_current_schema(self)",
            '\n            conn.execute("DELETE FROM vault_receipts")\n'
            "\n    def ensure_current_schema(self)",
        )
        source.write_text(text)
        self.commit()
        result = self.run_check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_step"], "verify_migration")
        self.assertIn("records", result["error"])

    def test_a_timeout_is_incomplete_and_keeps_partial_output(self) -> None:
        real_run = subprocess.run

        def timeout(command, *args, **kwargs):
            if command[0] == sys.executable:
                raise subprocess.TimeoutExpired(command, 1, output=b"partial fixture")
            return real_run(command, *args, **kwargs)

        with mock.patch.object(check.subprocess, "run", side_effect=timeout):
            result = self.run_check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "incomplete")
        self.assertEqual(result["failed_step"], "seed_baseline")
        log = self.artifacts / result["steps"][-1]["stdout"]
        self.assertEqual(log.read_text(), "partial fixture")

    def test_exported_links_are_refused_without_touching_the_target(self) -> None:
        outside = self.root / "outside"
        outside.write_text("retained\n")
        (self.repo / PLUGIN / "escape.py").symlink_to(outside)
        self.commit()
        result = self.run_check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_step"], "export_candidate")
        self.assertEqual(outside.read_text(), "retained\n")

    def test_a_failed_final_receipt_write_cannot_report_success(self) -> None:
        real_save = check.Runner.save

        def fail_final_save(runner):
            if runner.report["ok"]:
                raise OSError("fixture write failure")
            return real_save(runner)

        with mock.patch.object(check.Runner, "save", fail_final_save):
            result = self.run_check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "incomplete")
        receipt = json.loads((self.artifacts / "result.json").read_text())
        self.assertFalse(receipt["ok"])


class DatabaseStateTests(unittest.TestCase):
    def test_records_still_in_the_write_ahead_log_are_compared(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            store = Store(folder)
            before = check.data_state(folder)
            database_digest = check.digest(store.paths.database)
            with closing(sqlite3.connect(store.paths.database)) as conn:
                conn.execute("INSERT INTO settings VALUES('fixture-new', 'retained')")
                conn.commit()
                self.assertEqual(check.digest(store.paths.database), database_digest)
                after = check.data_state(folder)
                self.assertNotEqual(before["database"], after["database"])
