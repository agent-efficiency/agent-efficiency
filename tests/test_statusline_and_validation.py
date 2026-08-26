from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from agent_efficiency.cli import main
from agent_efficiency.statusline import process_statusline
from agent_efficiency.store import Store
from scripts.validate_distribution import validate_distribution


class StatuslineTests(unittest.TestCase):
    def test_claude_metrics_are_recorded_and_rendered(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            line = process_statusline(
                {
                    "session_id": "status-session",
                    "cwd": "/work/repo",
                    "model": {"id": "claude-sonnet", "display_name": "Sonnet"},
                    "cost": {
                        "total_cost_usd": 0.125,
                        "total_lines_added": 10,
                        "total_lines_removed": 2,
                    },
                    "context_window": {
                        "total_input_tokens": 12000,
                        "total_output_tokens": 800,
                        "used_percentage": 37,
                        "current_usage": {
                            "cache_read_input_tokens": 2000,
                            "cache_creation_input_tokens": 100,
                        },
                    },
                },
                store,
            )
            self.assertIn("AE advise", line)
            self.assertIn("$0.125", line)
            self.assertIn("37% ctx", line)
            session = store.get_session("status-session")
            self.assertEqual(session["input_tokens"], 12000)
            self.assertAlmostEqual(session["cost_usd"], 0.125)


class CursorStatusTests(unittest.TestCase):
    def test_cursor_status_reports_latest_cursor_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            store.ensure_session(
                "cursor-session",
                host="cursor",
                cwd="/work/cursor-project",
            )
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(
                    main(["--data-dir", temp, "cursor", "status", "--json"]),
                    0,
                )
        value = json.loads(output.getvalue())
        self.assertEqual(value["host"], "cursor")
        self.assertEqual(value["session_id"], "cursor-session")
        self.assertIn("failures", value)


class DistributionValidationTests(unittest.TestCase):
    def test_distribution_is_valid(self) -> None:
        result = validate_distribution()
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["forbidden_references"], [])
        self.assertGreaterEqual(result["policy_count"], 12)
        self.assertEqual(result["operational_policy_count"], 9)
        self.assertEqual(result["guidance_card_count"], 3)
        self.assertEqual(
            result["guidance_pack_id"],
            "agent-efficiency-guidance",
        )
        self.assertRegex(
            result["guidance_pack_digest"],
            r"^sha256:[0-9a-f]{64}$",
        )

    def test_runtime_cli_excludes_maintainer_commands(self) -> None:
        with (
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            main(["validate", "--json"])

    def test_migration_check_does_not_mutate_then_apply_backs_up(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            with store.connect() as conn:
                conn.execute(
                    "UPDATE settings SET value = '5' WHERE key = 'schema_version'"
                )
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(
                    main(["--data-dir", temp, "migrate", "--check", "--json"]),
                    0,
                )
            check = json.loads(output.getvalue())
            self.assertTrue(check["migration_required"])
            self.assertFalse(check["applied"])
            self.assertEqual(store.get_setting("schema_version"), "5")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(
                    main(["--data-dir", temp, "migrate", "--apply", "--json"]),
                    0,
                )
            applied = json.loads(output.getvalue())
            self.assertFalse(applied["migration_required"])
            self.assertTrue(applied["applied"])
            self.assertTrue((Path(temp) / "agent-efficiency.db.schema-5.bak").is_file())

    def test_packaged_command_smoke_tests_all_hosts(self) -> None:
        for host in ("claude", "cursor", "codex"):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["smoke-test", host, "--json"]), 0)
            result = json.loads(output.getvalue())
            self.assertTrue(result["ok"])
            self.assertTrue(result["native_response_shape_valid"])

    def test_doctor_reports_verified_first_party_pack(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                main(["--data-dir", temp, "doctor", "--json"]),
                0,
            )
        doctor = json.loads(output.getvalue())
        self.assertEqual(doctor["runtime_version"], "0.1.0")
        self.assertIn("does not prove", doctor["fetch_observation_limit"])
        self.assertTrue(doctor["embedded_capability_pack_ready"])
        self.assertEqual(
            doctor["embedded_capability_pack_id"],
            "agent-efficiency-guidance",
        )
        self.assertEqual(doctor["embedded_capability_pack_sequence"], 1)
        self.assertEqual(doctor["embedded_capability_pack_card_count"], 3)

    def test_doctor_reports_all_native_host_packages(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                main(["--data-dir", temp, "doctor", "--json"]),
                0,
            )
        doctor = json.loads(output.getvalue())
        self.assertTrue(doctor["host_packages_ready"])
        self.assertEqual(
            set(doctor["hosts"]),
            {"claude", "cursor", "codex"},
        )
        for host in doctor["hosts"].values():
            self.assertTrue(host["manifest_ready"])
            self.assertTrue(host["hooks_ready"])
            self.assertGreater(host["event_count"], 0)


if __name__ == "__main__":
    unittest.main()
