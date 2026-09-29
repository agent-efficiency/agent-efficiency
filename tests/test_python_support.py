from __future__ import annotations

import contextlib
import io
import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_efficiency import cli as runtime_cli
from agent_efficiency.cli import main
from agent_efficiency.paths import PLUGIN_ROOT
from agent_efficiency.python_support import (
    MINIMUM_PYTHON,
    unsupported_python_message,
)
from tests.test_doctor import isolated_home

OLD = (3, 9, 18, "final", 0)


class PythonSupportTests(unittest.TestCase):
    def test_minimum_matches_the_package_metadata(self) -> None:
        import tomllib

        project = tomllib.loads(
            (PLUGIN_ROOT.parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            project["project"]["requires-python"],
            ">=" + ".".join(str(part) for part in MINIMUM_PYTHON),
        )

    def test_an_old_python_gets_one_line_naming_the_minimum(self) -> None:
        message = unsupported_python_message(OLD)
        self.assertIsNotNone(message)
        self.assertIn("3.11 or newer", message)
        self.assertIn("3.9.18", message)
        self.assertNotIn("\n", message)

    def test_supported_pythons_pass(self) -> None:
        for version in ((3, 11, 0), (3, 12, 1), (3, 14, 7), (4, 0, 0)):
            with self.subTest(version=version):
                self.assertIsNone(unsupported_python_message(version))

    def test_entry_scripts_stop_before_importing_the_package(self) -> None:
        for script in ("agent_efficiency_hook.py", "agent_efficiency_cli.py"):
            with self.subTest(script=script):
                errors = io.StringIO()
                with (
                    mock.patch.object(sys, "version_info", OLD),
                    mock.patch("sys.stdin", io.StringIO("{}")),
                    contextlib.redirect_stderr(errors),
                    self.assertRaises(SystemExit) as stopped,
                ):
                    runpy.run_path(
                        str(PLUGIN_ROOT / "scripts" / script), run_name="__main__"
                    )
                self.assertEqual(stopped.exception.code, 1)
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertIn("3.11 or newer", errors.getvalue())

    def test_doctor_reports_python_against_the_minimum(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            isolated_home(temp),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            main(["--data-dir", str(Path(temp) / "data"), "doctor", "--json"])
        doctor = json.loads(output.getvalue())
        self.assertEqual(doctor["python_minimum"], "3.11")
        self.assertTrue(doctor["python_supported"])
        self.assertIn("hook_python", doctor)

    def test_doctor_fails_when_hook_python_is_too_old(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            isolated_home(temp),
            mock.patch.object(
                runtime_cli, "_hook_python", return_value=("/usr/bin/python3", "3.9.18")
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            code = main(["--data-dir", str(Path(temp) / "data"), "doctor"])
        self.assertEqual(code, 1)
        self.assertIn("3.9.18", output.getvalue())
        self.assertIn("3.11 or newer", output.getvalue())


if __name__ == "__main__":
    unittest.main()
