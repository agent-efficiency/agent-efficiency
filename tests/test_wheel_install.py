"""The package works when installed alone, with no plugin folder beside it.

A wheel holds the Python package and the package data pyproject.toml names,
nothing else. These tests lay out exactly that in a temporary folder and run
the command from it. scripts/check_wheel_install.py builds and installs a real
wheel; CI runs it.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from agent_efficiency.paths import PACKAGE_DIR

REPO_ROOT = Path(__file__).resolve().parents[1]
NOTE = """---
schema: 1
id: example
title: Example
type: feedback
classification: core
status: active
updated: 2026-09-29
hook: An example note.
---
Body.
"""


def package_data_patterns() -> list[str]:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    data = project.get("tool", {}).get("setuptools", {}).get("package-data", {})
    return list(data.get("agent_efficiency", []))


def wheel_layout(site: Path) -> Path:
    """Copy what a wheel would install: modules plus the named package data."""

    patterns = package_data_patterns()
    target = site / "agent_efficiency"
    for path in PACKAGE_DIR.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(PACKAGE_DIR).as_posix()
        if path.suffix == ".py" or any(
            fnmatch.fnmatch(relative, pattern) for pattern in patterns
        ):
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    return target


class WheelLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.site = self.base / "site"
        wheel_layout(self.site)
        self.home = self.base / "home"
        self.project = self.home / "project"
        self.project.mkdir(parents=True)

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "agent_efficiency", *arguments],
            cwd=self.project,
            env={
                "HOME": str(self.home),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONPATH": str(self.site),
                "LANG": "C.UTF-8",
            },
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def assert_runs(self, *arguments: str) -> str:
        completed = self.run_cli(*arguments)
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertNotIn("Traceback", output)
        return completed.stdout

    def test_the_layout_is_really_without_a_plugin_folder(self) -> None:
        completed = self.run_cli("--version")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse((self.site / ".claude-plugin").exists())
        self.assertFalse((self.base / ".claude-plugin").exists())

    def test_doctor_runs_and_says_there_is_no_plugin_folder(self) -> None:
        text = self.assert_runs("doctor")
        self.assertIn("Agent Efficiency doctor: OK", text)
        self.assertIn("installed Python package", text)
        doctor = json.loads(self.assert_runs("doctor", "--json"))
        self.assertTrue(doctor["ok"])
        self.assertIsNone(doctor["package_root"])
        self.assertIsNone(doctor["host_packages_ready"])
        self.assertTrue(doctor["embedded_capability_pack_ready"])
        self.assertGreater(doctor["active_policy_count"], 0)

    def test_knowledge_status_reads_the_bundled_pack(self) -> None:
        self.assertIn("agent-efficiency-guidance", self.assert_runs("knowledge", "status"))

    def test_smoke_test_runs_the_installed_package(self) -> None:
        for host in ("claude", "codex", "cursor"):
            with self.subTest(host=host):
                result = json.loads(self.assert_runs("smoke-test", host, "--json"))
                self.assertTrue(result["ok"])
                self.assertTrue(result["native_response_shape_valid"])
                self.assertIn("agent_efficiency.hook_entry", result["command_under_test"])

    def test_status_report_and_vault_check_run(self) -> None:
        self.assert_runs("status")
        self.assert_runs("report")
        tree = self.home / "vault-core"
        self.assert_runs("vault", "init", str(tree), "--classification", "core")
        (tree / "feedback" / "example.md").write_text(NOTE, encoding="utf-8")
        self.assert_runs("vault", "index", str(tree))
        self.assertIn("No findings.", self.assert_runs("vault", "check", str(tree)))


class RealWheelTests(unittest.TestCase):
    def test_a_built_wheel_installs_and_runs(self) -> None:
        if importlib.util.find_spec("setuptools") is None:
            self.skipTest(
                "setuptools is not installed; CI builds the wheel with "
                "scripts/check_wheel_install.py"
            )
        completed = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "check_wheel_install.py"),
                "--no-build-isolation",
            ],
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
