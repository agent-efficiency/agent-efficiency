from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
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


if __name__ == "__main__":
    unittest.main()
