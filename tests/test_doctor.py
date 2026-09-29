from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_efficiency import cli as runtime_cli
from agent_efficiency.cli import main
from agent_efficiency.paths import PLUGIN_ROOT
from agent_efficiency.store import Store

HOST_ENVIRONMENT = (
    "AGENT_EFFICIENCY_DATA",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_PLUGIN_DATA",
    "CLAUDE_PLUGIN_ROOT",
    "CODEX_HOME",
    "CURSOR_PLUGIN_ROOT",
    "PLUGIN_DATA",
    "PLUGIN_ROOT",
    "XDG_DATA_HOME",
)


def isolated_environment(home: Path) -> dict[str, str]:
    """Return the current environment with every host location in ``home``."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in HOST_ENVIRONMENT
    }
    environment["HOME"] = str(home)
    return environment


class DoctorTextTests(unittest.TestCase):
    def test_plain_doctor_runs_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            home.mkdir()
            environment = isolated_environment(home)
            environment["PYTHONPATH"] = str(PLUGIN_ROOT / "src")
            completed = subprocess.run(
                [sys.executable, "-m", "agent_efficiency", "doctor"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertTrue(
            completed.stdout.startswith("Agent Efficiency doctor: OK"),
            completed.stdout,
        )
        self.assertIn("runtime_version:", completed.stdout)
        self.assertIn("data_dir:", completed.stdout)
        self.assertNotIn("Traceback", completed.stdout)

    def test_text_keys_are_all_produced_by_doctor(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.dict(os.environ, isolated_environment(Path(temp)), clear=True),
        ):
            result = runtime_cli._doctor(Store(Path(temp) / "data"))
        missing = [key for key in runtime_cli.DOCTOR_TEXT_KEYS if key not in result]
        self.assertEqual(missing, [])

    def test_text_output_skips_keys_the_result_lacks(self) -> None:
        text = runtime_cli._format_doctor({"ok": True, "runtime_version": "9"})
        self.assertEqual(text, "Agent Efficiency doctor: OK\nruntime_version: 9")

    def test_text_doctor_exit_code_follows_ok(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.dict(os.environ, isolated_environment(Path(temp)), clear=True),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            code = main(["--data-dir", str(Path(temp) / "data"), "doctor"])
        self.assertEqual(code, 0)
        self.assertIn("Agent Efficiency doctor: OK", output.getvalue())


if __name__ == "__main__":
    unittest.main()
