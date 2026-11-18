"""Exercise advertised CLI contracts and explicit ordered milestone semantics."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import agent_efficiency
import test_verification
from agent_efficiency.verification.config import load_project_config
from agent_efficiency.verification.runner import run_checks
from agent_efficiency.verification.workflow import workflow_status


class WorkflowCliTests(unittest.TestCase):
    setUp = test_verification.VerificationTests.setUp

    def cli(self, *arguments, expected=0):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(
            Path(agent_efficiency.__file__).resolve().parent.parent
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_efficiency",
                "--data-dir",
                self.data.name,
                *arguments,
            ],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout

    def test_cli_reuse_explanation_and_rework_report(self):
        self.assertEqual(
            json.loads(self.cli("evidence", "explain", "--json"))[0]["reason"],
            "missing_receipt",
        )
        self.cli("check", "unit")
        self.assertIn(
            "reused current evidence", self.cli("check", "unit", "--reuse-current")
        )
        self.assertTrue(
            json.loads(self.cli("evidence", "explain", "--json"))[0]["reusable"]
        )
        self.cli(
            "workflow",
            "record",
            "--kind",
            "retry",
            "--cause",
            "tooling_failure",
            "--duration-ms",
            "200",
        )
        report = json.loads(self.cli("workflow", "report"))
        self.assertEqual(report["rework"]["events"], 1)
        self.assertFalse(report["duration_is_wall_time"])
        self.assertIsNone(report["coverage"]["external_background_results"])
        self.assertEqual(json.loads(self.cli("jobs")), [])
        self.cli("jobs", "not-an-id", expected=2)
        self.cli("check", "unit", "--plan", "--reuse-current", expected=2)
        self.cli("check", "unit", "--output", "/unused", expected=2)
        self.cli(
            "workflow",
            "record",
            "--kind",
            "retry",
            "--cause",
            "unknown",
            "--cost-usd",
            "1",
            expected=2,
        )

    def test_later_milestone_cannot_bypass_missing_or_failed_earlier_gate(self):
        path = self.root / "agent-efficiency.toml"
        path.write_text(
            path.read_text()
            + '\n[[checks]]\nid = "later"\ncommand = ["python", "-c", "raise SystemExit(0)"]\n'
            '\n[[milestones]]\nid = "first-stage"\nchecks = ["unit"]\n'
            '\n[[milestones]]\nid = "second-stage"\nchecks = ["later"]\n'
        )
        config = load_project_config(self.root)
        run_checks(config, [config.get("later")], self.store)
        states = workflow_status(config, self.store)
        self.assertEqual(
            [row["state"] for row in states["milestones"]], ["pending", "blocked"]
        )
        run_checks(config, [config.get("unit")], self.store)
        self.assertEqual(
            [row["state"] for row in workflow_status(config, self.store)["milestones"]],
            ["ready", "ready"],
        )
        path.write_text(
            path.read_text().replace(
                'id = "unit"\ncommand = ["python", "-c", "raise SystemExit(0)"]',
                'id = "unit"\ncommand = ["python", "-c", "raise SystemExit(1)"]',
            )
        )
        config = load_project_config(self.root)
        run_checks(config, config.checks, self.store)
        self.assertEqual(
            [row["state"] for row in workflow_status(config, self.store)["milestones"]],
            ["failed", "blocked"],
        )
        self.assertFalse(workflow_status(config, self.store)["release_approval"])

    def test_reuse_requires_current_transitive_prerequisite(self):
        path = self.root / "agent-efficiency.toml"
        path.write_text(
            path.read_text()
            + '\n[[checks]]\nid = "later"\ncommand = ["python", "-c", "raise SystemExit(0)"]\ndepends_on = ["unit"]\n'
        )
        config = load_project_config(self.root)
        target = config.get("later")
        (first,) = run_checks(config, [target], self.store)
        (rerun,) = run_checks(config, [target], self.store, reuse_current=True)
        self.assertNotEqual(first["receipt_id"], rerun["receipt_id"])
        run_checks(config, [config.get("unit")], self.store)
        (reused,) = run_checks(config, [target], self.store, reuse_current=True)
        self.assertTrue(reused["reused"])
        self.assertEqual(rerun["receipt_id"], reused["receipt_id"])
