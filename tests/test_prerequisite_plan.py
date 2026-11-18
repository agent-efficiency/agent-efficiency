"""PF-1 starts with an explicit, read-only prerequisite plan, not enforcement."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_efficiency.cli import main
from agent_efficiency.store import Store, project_identity
from agent_efficiency.verification.config import load_project_config
from agent_efficiency.verification.runner import run_checks


class PrerequisitePlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.store = Store(str(Path(self.temp.name) / "data"))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "--allow-empty",
                "-qm",
                "base",
            ],
            check=True,
        )

    def config(self, dependencies=None):
        dependencies = dependencies or {
            "reference": [],
            "unit": ["reference"],
            "publish": ["unit"],
        }
        text = 'version = 1\nmode = "observe"\n'
        for name, needs in dependencies.items():
            text += "\n[[checks]]\nid = " + json.dumps(name)
            text += "\ncommand = " + json.dumps([sys.executable, "-c", "pass"])
            text += "\ndepends_on = " + json.dumps(needs) + "\n"
        (self.root / "agent-efficiency.toml").write_text(text)
        return load_project_config(self.root)

    def plan(self, config):
        from agent_efficiency.verification.prerequisites import prerequisite_plan

        return prerequisite_plan(config, [config.get("publish")], self.store)

    def test_missing_transitive_prerequisites_are_named(self):
        config = self.config()
        result = self.plan(config)
        self.assertFalse(result["ready"])
        self.assertEqual(result["order"], ["reference", "unit", "publish"])
        self.assertEqual(
            result["prerequisites"], {"reference": "missing", "unit": "missing"}
        )

    def test_current_prerequisites_do_not_imply_target_passed(self):
        config = self.config()
        run_checks(config, [config.get("reference"), config.get("unit")], self.store)
        self.assertTrue(self.plan(config)["ready"])
        key, _ = project_identity(str(self.root))
        self.assertEqual(self.store.verification_receipts(key, check_id="publish"), [])

    def test_workspace_change_invalidates_prerequisites(self):
        config = self.config()
        run_checks(config, [config.get("reference"), config.get("unit")], self.store)
        (self.root / "changed.py").write_text("value = 1\n")
        result = self.plan(config)
        self.assertFalse(result["ready"])
        self.assertEqual(set(result["prerequisites"].values()), {"stale"})

    def test_unknown_duplicate_self_and_cyclic_dependencies_are_rejected(self):
        for dependencies in (
            {"unit": ["absent"]},
            {"unit": ["unit"]},
            {"unit": ["reference", "reference"], "reference": []},
            {"unit": ["reference"], "reference": ["unit"]},
        ):
            with self.subTest(dependencies=dependencies), self.assertRaises(ValueError):
                self.config(dependencies)

    def test_cli_plan_never_runs_a_check_or_writes_a_receipt(self):
        self.config()
        output = io.StringIO()
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with (
                patch("agent_efficiency.cli.run_checks") as run,
                contextlib.redirect_stdout(output),
            ):
                code = main(
                    [
                        "--data-dir",
                        str(Path(self.temp.name) / "data"),
                        "check",
                        "publish",
                        "--plan",
                    ]
                )
            run.assert_not_called()
        finally:
            os.chdir(previous)
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output.getvalue())["ready"])
        key, _ = project_identity(str(self.root))
        self.assertEqual(self.store.verification_receipts(key), [])

    def test_latest_failure_never_falls_back_to_an_older_pass(self):
        config = self.config()
        receipts = run_checks(
            config, [config.get("reference"), config.get("unit")], self.store
        )
        key, _ = project_identity(str(self.root))
        for result in ("fail", "blocked", "inconclusive"):
            with self.subTest(result=result):
                self.store.record_verification_receipt(
                    {**receipts[0], "result": result, "exit_code": 1},
                    project_key=key,
                )
                report = self.plan(config)
                self.assertFalse(report["ready"])
                self.assertEqual(report["prerequisites"]["reference"], result)

    def test_expired_pass_is_not_ready(self):
        self.config()
        path = self.root / "agent-efficiency.toml"
        path.write_text(
            path.read_text().replace(
                'id = "reference"', 'id = "reference"\nmax_age_seconds = 1'
            )
        )
        config = load_project_config(self.root)
        receipts = run_checks(
            config, [config.get("reference"), config.get("unit")], self.store
        )
        key, _ = project_identity(str(self.root))
        self.store.record_verification_receipt(
            {**receipts[0], "finished_at": "2000-01-01T00:00:00Z"},
            project_key=key,
        )
        report = self.plan(config)
        self.assertFalse(report["ready"])
        self.assertEqual(report["prerequisites"]["reference"], "stale")

    def test_unknown_workspace_is_not_ready_even_without_prerequisites(self):
        from types import SimpleNamespace

        config = self.config({"publish": []})
        with patch(
            "agent_efficiency.verification.prerequisites.workspace_state",
            return_value=SimpleNamespace(result="unknown", digest=None),
        ):
            self.assertFalse(self.plan(config)["ready"])

    def test_shared_dependencies_are_not_duplicated(self):
        config = self.config(
            {
                "publish": ["unit", "sanity"],
                "unit": ["reference"],
                "sanity": ["reference"],
                "reference": [],
            }
        )
        self.assertEqual(
            self.plan(config)["order"], ["reference", "unit", "sanity", "publish"]
        )

    def test_dependency_order_handles_deep_graph_without_recursion(self):
        dependencies = {"publish": ["check0"]}
        dependencies.update({f"check{i}": [f"check{i + 1}"] for i in range(1050)})
        dependencies["check1050"] = []
        config = self.config(dependencies)
        order = self.plan(config)["order"]
        self.assertEqual(len(order), 1052)
        self.assertEqual(order[0], "check1050")
        self.assertEqual(order[-1], "publish")

    def test_nonempty_dependencies_change_configuration_digest(self):
        with_dependencies = self.config()
        without_dependencies = self.config({"reference": [], "unit": [], "publish": []})
        self.assertNotEqual(with_dependencies.digest, without_dependencies.digest)
        self.assertNotEqual(
            with_dependencies.get("publish").digest,
            without_dependencies.get("publish").digest,
        )

    def test_empty_dependencies_preserve_existing_config_digests(self):
        config = self.config({"unit": []})
        path = self.root / "agent-efficiency.toml"
        path.write_text(path.read_text().replace("depends_on = []\n", ""))
        legacy = load_project_config(self.root)
        self.assertEqual(config.digest, legacy.digest)
        self.assertEqual(config.checks[0].digest, legacy.checks[0].digest)
