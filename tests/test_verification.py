from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from agent_efficiency.cli import main
from agent_efficiency.store import Store, project_identity
from agent_efficiency.verification.config import load_project_config
from agent_efficiency.verification.receipts import receipt_state
from agent_efficiency.verification.runner import run_checks
from agent_efficiency.verification.state import workspace_state


class VerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=self.root, check=True
        )
        (self.root / "sample.py").write_text("value = 1\n")
        (self.root / "agent-efficiency.toml").write_text(
            """version = 1
mode = "advise"

[[checks]]
id = "unit"
command = ["python", "-c", "raise SystemExit(0)"]
applies_to = ["**/*.py"]
required_for = ["code"]
timeout_seconds = 30
"""
        )
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.root, check=True)
        self.data = tempfile.TemporaryDirectory()
        self.addCleanup(self.data.cleanup)
        self.store = Store(self.data.name)

    def test_passing_receipt_is_current_until_the_workspace_changes(self) -> None:
        config = load_project_config(self.root)
        receipts = run_checks(config, config.checks, self.store)
        self.assertEqual(receipts[0]["result"], "pass")
        current = workspace_state(self.root, config.digest)
        self.assertEqual(
            receipt_state(
                receipts[0],
                check_digest=config.checks[0].digest,
                workspace_digest=current.digest,
            ),
            "current",
        )
        (self.root / "sample.py").write_text("value = 2\n")
        changed = workspace_state(self.root, config.digest)
        self.assertEqual(
            receipt_state(
                receipts[0],
                check_digest=config.checks[0].digest,
                workspace_digest=changed.digest,
            ),
            "stale",
        )

    def test_runner_does_not_store_command_or_process_output(self) -> None:
        marker = "private_command_marker_8472"
        output = "private_output_marker_9271"
        path = self.root / "agent-efficiency.toml"
        path.write_text(
            f"""version = 1
mode = "advise"
[[checks]]
id = "privacy"
command = ["python", "-c", "print('{output}') # {marker}"]
timeout_seconds = 30
"""
        )
        subprocess.run(["git", "add", str(path.name)], cwd=self.root, check=True)
        config = load_project_config(self.root)
        run_checks(config, config.checks, self.store)
        database = self.store.paths.database.read_bytes()
        self.assertNotIn(marker.encode(), database)
        self.assertNotIn(output.encode(), database)

    def test_configured_receipt_age_can_make_a_pass_stale(self) -> None:
        config_path = self.root / "agent-efficiency.toml"
        config_path.write_text(
            config_path.read_text().replace(
                "timeout_seconds = 30",
                "timeout_seconds = 30\nmax_age_seconds = 60",
            )
        )
        config = load_project_config(self.root)
        receipt = run_checks(config, config.checks, self.store)[0]
        current = workspace_state(self.root, config.digest)
        finished = datetime.fromisoformat(
            receipt["finished_at"].replace("Z", "+00:00")
        ).astimezone(UTC)
        self.assertEqual(
            receipt_state(
                receipt,
                check_digest=config.checks[0].digest,
                workspace_digest=current.digest,
                max_age_seconds=config.checks[0].max_age_seconds,
                now=finished + timedelta(seconds=61),
            ),
            "stale",
        )

    def test_non_git_workspace_is_inconclusive(self) -> None:
        other = self.root / "not-git"
        other.mkdir()
        state = workspace_state(other, "sha256:" + "0" * 64)
        self.assertEqual(state.result, "inconclusive")
        self.assertIsNone(state.digest)

    def test_shell_string_commands_are_rejected(self) -> None:
        (self.root / "agent-efficiency.toml").write_text(
            """version = 1
mode = "advise"
[[checks]]
id = "unsafe"
command = "python -m pytest"
"""
        )
        with self.assertRaisesRegex(ValueError, "command"):
            load_project_config(self.root)

    def test_unknown_config_fields_are_rejected(self) -> None:
        (self.root / "agent-efficiency.toml").write_text(
            """version = 1
mode = "advise"
unknown = true
[[checks]]
id = "unit"
command = ["python", "-V"]
"""
        )
        with self.assertRaisesRegex(ValueError, "unknown"):
            load_project_config(self.root)

    def test_environment_is_empty_without_an_allowlist(self) -> None:
        config = load_project_config(self.root)
        with patch.dict(os.environ, {"PRIVATE_CANARY": "secret"}, clear=False):
            with patch("subprocess.run", wraps=subprocess.run) as run:
                run_checks(config, config.checks, self.store)
        check_call = next(
            call for call in run.call_args_list if call.kwargs.get("env") is not None
        )
        self.assertNotIn("PRIVATE_CANARY", check_call.kwargs["env"])

    def test_schema_three_migrates_with_a_backup(self) -> None:
        self.store.ensure_session(
            "session-1", host="codex", cwd=str(self.root), model=None
        )
        with self.store.connect() as conn:
            conn.execute("DROP TABLE verification_receipts")
            conn.execute("DROP TABLE workspace_states")
            conn.execute("DROP TABLE check_definitions")
            conn.execute("UPDATE settings SET value = '3' WHERE key = 'schema_version'")
        status = self.store.migration_status()
        self.assertTrue(status["migration_required"])
        self.assertEqual(status["target_schema"], "7")
        self.store.ensure_current_schema()
        self.assertEqual(self.store.get_setting("schema_version"), "7")
        self.assertEqual(
            self.store.get_session("session-1")["evidence_version"],
            "legacy-observation",
        )
        backup = self.store.paths.database.with_name(
            f"{self.store.paths.database.name}.schema-3.bak"
        )
        self.assertTrue(backup.is_file())
        with self.store.connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertIn("verification_receipts", tables)

    def test_evidence_show_returns_one_exact_receipt(self) -> None:
        config = load_project_config(self.root)
        receipt = run_checks(config, config.checks, self.store)[0]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(
                main(
                    [
                        "--data-dir",
                        self.data.name,
                        "evidence",
                        "show",
                        str(receipt["receipt_id"]),
                        "--json",
                    ]
                ),
                0,
            )
        shown = json.loads(output.getvalue())
        self.assertEqual(shown["id"], receipt["receipt_id"])
        self.assertEqual(shown["check_id"], "unit")

    def test_receipts_are_bound_to_the_project(self) -> None:
        config = load_project_config(self.root)
        run_checks(config, config.checks, self.store)
        key, _ = project_identity(str(self.root))
        values = self.store.verification_receipts(key, check_id="unit")
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["result"], "pass")


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


class VerificationResultTests(unittest.TestCase):
    """A check result says why it is not a pass, and caches do not change it."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.data = str(Path(self.temp.name) / "data")
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "test@example.com")
        _git(self.root, "config", "user.name", "Test")
        (self.root / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_calc.py").write_text(
            "import unittest\n\nimport calc\n\n\n"
            "class CalcTest(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(calc.add(1, 2), 3)\n",
            encoding="utf-8",
        )
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-qm", "base")
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)

    def cli(self, *arguments: str) -> tuple[int, str]:
        with (
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = main(["--data-dir", self.data, *arguments])
        return code, output.getvalue()

    def write_config(self, command: list[str]) -> None:
        (self.root / "agent-efficiency.toml").write_text(
            'version = 1\nmode = "advise"\n\n[[checks]]\nid = "unit"\n'
            f"command = {json.dumps(command)}\n"
            'applies_to = ["**/*.py"]\nrequired_for = ["code"]\n'
            "timeout_seconds = 60\n",
            encoding="utf-8",
        )

    def test_readme_example_passes_on_the_first_run(self) -> None:
        self.assertEqual(self.cli("init")[0], 0)
        if shutil.which("python") is None:
            config = self.root / "agent-efficiency.toml"
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    '"python"', json.dumps(sys.executable)
                ),
                encoding="utf-8",
            )
        code, output = self.cli("check", "unit")
        self.assertEqual(code, 0, output)
        self.assertIn("check unit: pass", output)

    def test_cache_folders_a_check_writes_do_not_change_the_result(self) -> None:
        script = (
            "import pathlib\n"
            "for name in ('__pycache__', '.pytest_cache', '.mypy_cache', "
            "'.ruff_cache', 'tests/__pycache__'):\n"
            "    folder = pathlib.Path(name)\n"
            "    folder.mkdir(parents=True, exist_ok=True)\n"
            "    (folder / 'entry.bin').write_bytes(b'cache')\n"
        )
        self.write_config([sys.executable, "-c", script])
        code, output = self.cli("check", "unit")
        self.assertEqual(code, 0, output)
        self.assertIn("check unit: pass", output)

    def test_check_environment_turns_off_bytecode_files(self) -> None:
        self.write_config(
            [
                sys.executable,
                "-c",
                "import os, sys; "
                "sys.exit(0 if os.environ.get('PYTHONDONTWRITEBYTECODE') == '1' "
                "else 3)",
            ]
        )
        self.assertEqual(self.cli("check", "unit")[0], 0)

    def test_a_check_that_edits_a_tracked_file_is_not_a_pass(self) -> None:
        self.write_config(
            [
                sys.executable,
                "-c",
                "open('calc.py', 'a').write('# edited by the check\\n')",
            ]
        )
        code, output = self.cli("check", "unit")
        self.assertEqual(code, 1)
        self.assertIn("check unit: inconclusive", output)
        self.assertIn("workspace_changed", output)
        self.assertIn("changed files in the workspace", output)
        receipt_id = output.split("(receipt ")[1].split(")")[0]
        code, shown = self.cli("evidence", "show", receipt_id)
        self.assertEqual(code, 0)
        self.assertIn("reason_code: workspace_changed", shown)
        self.assertIn("reason: The check changed files", shown)
        code, shown_json = self.cli("evidence", "show", receipt_id, "--json")
        value = json.loads(shown_json)
        self.assertEqual(value["reason_code"], "workspace_changed")
        self.assertIn("changed files", value["reason"])
        code, listed = self.cli("evidence", "list")
        self.assertIn("unit: inconclusive: The check changed files", listed)

    def test_a_failing_check_names_its_exit_status(self) -> None:
        self.write_config([sys.executable, "-c", "raise SystemExit(4)"])
        code, output = self.cli("check", "unit")
        self.assertEqual(code, 1)
        self.assertIn("check unit: fail", output)
        self.assertIn("exit_nonzero", output)
        self.assertIn("status 4", output)

    def test_a_check_that_cannot_start_says_so(self) -> None:
        self.write_config(["agent-efficiency-no-such-program"])
        code, output = self.cli("check", "unit")
        self.assertEqual(code, 1)
        self.assertIn("check unit: blocked", output)
        self.assertIn("not_started", output)

    def test_a_workspace_without_a_commit_says_so(self) -> None:
        shutil.rmtree(self.root / ".git")
        _git(self.root, "init", "-q")
        self.write_config([sys.executable, "-c", "pass"])
        code, output = self.cli("check", "unit")
        self.assertEqual(code, 1)
        self.assertIn("check unit: inconclusive", output)
        self.assertIn("workspace_unknown", output)

    def test_every_stored_reason_has_a_sentence(self) -> None:
        from agent_efficiency.store import VERIFICATION_REASONS
        from agent_efficiency.verification.receipts import REASONS

        self.assertEqual(set(VERIFICATION_REASONS), set(REASONS))

    def test_an_older_database_gains_the_reason_column(self) -> None:
        store = Store(self.data)
        with store.connect() as conn:
            conn.execute("ALTER TABLE verification_receipts DROP COLUMN reason_code")
        self.write_config([sys.executable, "-c", "raise SystemExit(2)"])
        code, output = self.cli("check", "unit")
        self.assertEqual(code, 1)
        self.assertIn("exit_nonzero", output)


if __name__ == "__main__":
    unittest.main()
