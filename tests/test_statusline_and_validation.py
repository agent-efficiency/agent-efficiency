from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_efficiency import cli as runtime_cli
from agent_efficiency.cli import main
from agent_efficiency.paths import PLUGIN_ROOT
from agent_efficiency.statusline import process_statusline
from agent_efficiency.store import Store
from scripts import validate_distribution as validation
from scripts.validate_distribution import validate_distribution

FORBIDDEN_TERM = " ".join(("personal", "project"))
HOME_PATH = "/".join(("", "home", "someone", ""))
HOST_HOOK_FILES = {
    "claude": "./hooks/claude-hooks.json",
    "codex": "./hooks/codex-hooks.json",
    "cursor": "./hooks/cursor-hooks.json",
}


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

    @staticmethod
    def _source_copy(temp: str) -> Path:
        root = Path(temp) / "checkout"
        shutil.copytree(
            validation.ROOT,
            root,
            ignore=shutil.ignore_patterns(
                ".git",
                ".venv",
                "venv",
                "__pycache__",
                "*.pyc",
                "*.egg-info",
                "build",
                "dist",
            ),
        )
        return root

    @staticmethod
    def _add_virtual_environment(root: Path) -> None:
        site = root / ".venv" / "lib" / "python3" / "site-packages" / "example"
        site.mkdir(parents=True)
        # Built from parts so this file does not trip the check it tests.
        (site / "notes.md").write_text(
            f"A {FORBIDDEN_TERM} installed at {HOME_PATH}tools.\n",
            encoding="utf-8",
        )

    def _validate_copy(self, root: Path) -> dict:
        with mock.patch.object(
            validation, "PLUGIN", root / "plugins" / "agent-efficiency"
        ):
            return validate_distribution(root)

    def test_validation_ignores_untracked_virtual_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self._source_copy(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            self._add_virtual_environment(root)
            result = self._validate_copy(root)
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["forbidden_references"], [])

    def test_validation_skips_virtual_environment_outside_git(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self._source_copy(temp)
            self._add_virtual_environment(root)
            result = self._validate_copy(root)
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["forbidden_references"], [])

    def test_validation_still_reports_tracked_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self._source_copy(temp)
            (root / "docs" / "extra.md").write_text(
                f"Written as a {FORBIDDEN_TERM}.\n", encoding="utf-8"
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            result = self._validate_copy(root)
        self.assertFalse(result["ok"])
        self.assertIn(
            f"docs/extra.md: {FORBIDDEN_TERM}", result["forbidden_references"]
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
        self.assertEqual(doctor["runtime_version"], "0.2.0")
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


class HostHookFileTests(unittest.TestCase):
    """Each host must load only its own hook file.

    Claude Code loads a plugin's default hooks/hooks.json in addition to the
    file its manifest names. A Codex command in that default file then runs
    under Claude Code with an empty ${PLUGIN_ROOT} and fails on every event.
    """

    def _plugin_copy(self, temp: str) -> Path:
        target = Path(temp) / "repo" / "plugins" / "agent-efficiency"
        shutil.copytree(
            PLUGIN_ROOT,
            target,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "*.pyo", "*.egg-info"
            ),
        )
        (target / "hooks" / "hooks.json").unlink(missing_ok=True)
        for host, hook_file in HOST_HOOK_FILES.items():
            self._set_manifest_hooks(target, host, hook_file)
        return target

    @staticmethod
    def _set_manifest_hooks(plugin: Path, host: str, value: str | None) -> None:
        path = plugin / f".{host}-plugin" / "plugin.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if value is None:
            manifest.pop("hooks", None)
        else:
            manifest["hooks"] = value
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _hook_errors(plugin: Path) -> list[str]:
        with mock.patch.object(validation, "PLUGIN", plugin):
            errors = validate_distribution()["errors"]
        return [error for error in errors if "hooks" in error]

    def test_shipped_package_names_each_host_hook_file(self) -> None:
        self.assertFalse((PLUGIN_ROOT / "hooks" / "hooks.json").exists())
        for host, hook_file in HOST_HOOK_FILES.items():
            manifest = json.loads(
                (PLUGIN_ROOT / f".{host}-plugin" / "plugin.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest.get("hooks"), hook_file, host)
            self.assertTrue((PLUGIN_ROOT / hook_file).is_file(), host)

    def test_validation_accepts_host_specific_hook_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            plugin = self._plugin_copy(temp)
            self.assertEqual(self._hook_errors(plugin), [])

    def test_validation_rejects_default_hook_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            plugin = self._plugin_copy(temp)
            shutil.copyfile(
                plugin / "hooks" / "codex-hooks.json",
                plugin / "hooks" / "hooks.json",
            )
            errors = self._hook_errors(plugin)
        self.assertTrue(
            any(
                "hooks.json" in error and "Claude Code loads" in error
                for error in errors
            ),
            errors,
        )

    def test_validation_requires_codex_manifest_to_name_codex_hooks(self) -> None:
        for value in (None, "./hooks/hooks.json", "./hooks/claude-hooks.json"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp:
                plugin = self._plugin_copy(temp)
                self._set_manifest_hooks(plugin, "codex", value)
                errors = self._hook_errors(plugin)
                self.assertTrue(
                    any(
                        ".codex-plugin" in error and "./hooks/codex-hooks.json" in error
                        for error in errors
                    ),
                    errors,
                )

    def test_validation_requires_each_host_manifest_to_name_its_hooks(
        self,
    ) -> None:
        for host, hook_file in HOST_HOOK_FILES.items():
            with self.subTest(host=host), tempfile.TemporaryDirectory() as temp:
                plugin = self._plugin_copy(temp)
                self._set_manifest_hooks(plugin, host, "./hooks/other-hooks.json")
                errors = self._hook_errors(plugin)
                self.assertTrue(
                    any(
                        f".{host}-plugin" in error and hook_file in error
                        for error in errors
                    ),
                    errors,
                )

    def test_doctor_reads_codex_hook_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            plugin = self._plugin_copy(temp)
            with mock.patch.object(runtime_cli, "PLUGIN_ROOT", plugin):
                status = runtime_cli._host_package_status()
        self.assertTrue(status["codex"]["manifest_ready"])
        self.assertTrue(status["codex"]["hooks_ready"])
        self.assertTrue(status["codex"]["ready"])

    def test_doctor_rejects_codex_manifest_without_its_hook_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            plugin = self._plugin_copy(temp)
            self._set_manifest_hooks(plugin, "codex", None)
            with mock.patch.object(runtime_cli, "PLUGIN_ROOT", plugin):
                status = runtime_cli._host_package_status()
        self.assertFalse(status["codex"]["manifest_ready"])
        self.assertFalse(status["codex"]["ready"])

    def test_doctor_flags_stale_default_hook_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            plugin = self._plugin_copy(temp)
            shutil.copyfile(
                plugin / "hooks" / "codex-hooks.json",
                plugin / "hooks" / "hooks.json",
            )
            with (
                mock.patch.object(runtime_cli, "PLUGIN_ROOT", plugin),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                main(["--data-dir", temp, "doctor", "--json"])
        doctor = json.loads(output.getvalue())
        self.assertFalse(doctor["ok"])
        self.assertFalse(doctor["host_packages_ready"])
        claude = doctor["hosts"]["claude"]
        self.assertFalse(claude["ready"])
        reasons = " ".join(claude["not_ready_reasons"])
        self.assertIn("hooks/hooks.json", reasons)
        self.assertIn("remove", reasons)
        self.assertIn("empty plugin root", reasons)


if __name__ == "__main__":
    unittest.main()
