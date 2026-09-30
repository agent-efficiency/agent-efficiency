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


def isolated_home(home: str | Path):
    """Point every host location at ``home`` for the duration of a test."""

    return mock.patch.dict(os.environ, isolated_environment(Path(home)), clear=True)


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
            isolated_home(temp),
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
            isolated_home(temp),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            code = main(["--data-dir", str(Path(temp) / "data"), "doctor"])
        self.assertEqual(code, 0)
        self.assertIn("Agent Efficiency doctor: OK", output.getvalue())


def plugin_copy(target: Path, *, stale_default_hooks: bool = False) -> Path:
    """Copy the plugin the way a host installs it, optionally with a stale file."""

    shutil.copytree(
        PLUGIN_ROOT,
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )
    if stale_default_hooks:
        shutil.copyfile(
            target / "hooks" / "codex-hooks.json", target / "hooks" / "hooks.json"
        )
    return target


PLUGIN_ID = "agent-efficiency@agent-efficiency"


def claude_install(
    home: Path, records: list[dict], *, user_enabled: bool | None = True
) -> None:
    """Write the install records, and the user setting Claude Code writes."""

    plugins = home / ".claude" / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {PLUGIN_ID: records}}),
        encoding="utf-8",
    )
    if user_enabled is not None:
        write_json(
            home / ".claude" / "settings.json",
            {"enabledPlugins": {PLUGIN_ID: user_enabled}},
        )


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def trust(home: Path, folder: Path) -> None:
    """Record accepted workspace trust the way Claude Code saves it."""

    write_json(
        home / ".claude.json",
        {"projects": {str(folder): {"hasTrustDialogAccepted": True}}},
    )


def claude_record(path: Path, *, scope: str = "user", **extra: str) -> dict:
    return {"scope": scope, "installPath": str(path), "version": "0.2.1", **extra}


def codex_install(home: Path, *, enabled: bool = True) -> Path:
    codex = home / ".codex"
    codex.mkdir(parents=True, exist_ok=True)
    (codex / "config.toml").write_text(
        '[plugins."agent-efficiency@agent-efficiency"]\n'
        f"enabled = {'true' if enabled else 'false'}\n",
        encoding="utf-8",
    )
    return codex / "plugins" / "cache" / "agent-efficiency" / "agent-efficiency"


def run_doctor(home: Path, folder: Path | None = None) -> tuple[int, dict]:
    folder = folder or home / "work"
    folder.mkdir(parents=True, exist_ok=True)
    with (
        isolated_home(home),
        contextlib.redirect_stdout(io.StringIO()) as output,
    ):
        code = main(
            ["--data-dir", str(home / "data"), "doctor", "--json", "--cwd", str(folder)]
        )
    return code, json.loads(output.getvalue())


class InstalledHostTests(unittest.TestCase):
    def doctor(self, home: Path) -> tuple[int, dict]:
        return run_doctor(home)

    def test_hosts_that_are_not_installed_are_reported_as_such(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            code, doctor = self.doctor(Path(temp))
        self.assertEqual(code, 0)
        self.assertTrue(doctor["ok"])
        for host in ("claude", "codex", "cursor"):
            installed = doctor["installed_hosts"][host]
            self.assertFalse(installed["installed"], host)
            self.assertEqual(installed["status"], "not installed", host)
            self.assertEqual(installed["installs"], [], host)
        self.assertTrue(doctor["host_packages_ready"])
        self.assertEqual(doctor["package_root"], str(PLUGIN_ROOT))

    def test_claude_install_is_checked_in_its_own_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            user = plugin_copy(home / "cache" / "0.2.1")
            project = plugin_copy(home / "cache" / "project")
            claude_install(
                home,
                [
                    claude_record(user),
                    claude_record(
                        project, scope="project", projectPath=str(home / "app")
                    ),
                ],
            )
            code, doctor = self.doctor(home)
        self.assertEqual(code, 0)
        claude = doctor["installed_hosts"]["claude"]
        self.assertTrue(claude["installed"])
        self.assertTrue(claude["ready"])
        self.assertEqual(
            [(item["scope"], item["path"]) for item in claude["installs"]],
            [("user", str(user)), ("project", str(project))],
        )
        self.assertEqual(claude["installs"][0]["version"], "0.2.1")
        self.assertEqual(claude["installs"][1]["project"], str(home / "app"))

    def test_stale_default_hook_file_in_the_installed_plugin_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            installed = plugin_copy(home / "cache" / "0.2.1", stale_default_hooks=True)
            claude_install(home, [claude_record(installed)])
            code, doctor = self.doctor(home)
        self.assertEqual(code, 1)
        self.assertFalse(doctor["ok"])
        self.assertTrue(doctor["host_packages_ready"])
        claude = doctor["installed_hosts"]["claude"]
        self.assertFalse(claude["ready"])
        reasons = " ".join(claude["installs"][0]["not_ready_reasons"])
        self.assertIn("hooks/hooks.json", reasons)
        self.assertIn(str(installed), reasons)

    def test_install_record_pointing_at_a_missing_folder_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            missing = home / "cache" / "gone"
            claude_install(home, [claude_record(missing)])
            code, doctor = self.doctor(home)
        self.assertEqual(code, 1)
        install = doctor["installed_hosts"]["claude"]["installs"][0]
        self.assertFalse(install["ready"])
        self.assertIn("does not exist", " ".join(install["not_ready_reasons"]))

    def test_claude_config_dir_is_honored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            installed = plugin_copy(home / "cache" / "0.2.1")
            claude_install(home / "custom", [claude_record(installed)])
            (home / "custom" / ".claude").rename(home / "config")
            with (
                isolated_home(home),
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home / "config")}),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                main(
                    [
                        "--data-dir",
                        str(home / "data"),
                        "doctor",
                        "--json",
                        "--cwd",
                        str(home),
                    ]
                )
        claude = json.loads(output.getvalue())["installed_hosts"]["claude"]
        self.assertTrue(claude["installed"])
        self.assertTrue(claude["ready"])

    def test_codex_enabled_version_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            cache = codex_install(home)
            plugin_copy(cache / "0.2.1+codex.20260101000000")
            code, doctor = self.doctor(home)
        self.assertEqual(code, 0)
        codex = doctor["installed_hosts"]["codex"]
        self.assertTrue(codex["installed"])
        self.assertTrue(codex["ready"])
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(codex["installs"][0]["version"], manifest["version"])

    def test_codex_stale_default_hook_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            cache = codex_install(home)
            plugin_copy(cache / "0.2.1+codex.1", stale_default_hooks=True)
            code, doctor = self.doctor(home)
        self.assertEqual(code, 1)
        self.assertFalse(doctor["installed_hosts"]["codex"]["ready"])

    def test_codex_enabled_without_a_cached_copy_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            codex_install(home)
            code, doctor = self.doctor(home)
        self.assertEqual(code, 1)
        codex = doctor["installed_hosts"]["codex"]
        self.assertTrue(codex["installed"])
        self.assertFalse(codex["ready"])

    def test_disabled_codex_plugin_is_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            cache = codex_install(home, enabled=False)
            plugin_copy(cache / "0.2.1+codex.1", stale_default_hooks=True)
            code, doctor = self.doctor(home)
        self.assertEqual(code, 0)
        self.assertEqual(doctor["installed_hosts"]["codex"]["status"], "disabled")

    def test_cursor_local_plugin_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            local = home / ".cursor" / "plugins" / "local"
            local.mkdir(parents=True)
            plugin_copy(local / "agent-efficiency", stale_default_hooks=True)
            code, doctor = self.doctor(home)
        self.assertEqual(code, 1)
        cursor = doctor["installed_hosts"]["cursor"]
        self.assertTrue(cursor["installed"])
        self.assertFalse(cursor["ready"])

    def test_text_output_names_each_host(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            installed = plugin_copy(home / "cache" / "0.2.1")
            claude_install(home, [claude_record(installed)])
            with (
                isolated_home(home),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                code = main(
                    ["--data-dir", str(home / "data"), "doctor", "--cwd", str(home)]
                )
        text = output.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn(f"Package files at {PLUGIN_ROOT}: ready", text)
        self.assertIn(
            f"Claude Code: ready, 0.2.1 (user scope) at {installed}", text
        )
        self.assertIn("Codex: not installed", text)
        self.assertIn("Cursor: not installed", text)


class FolderEnablementTests(unittest.TestCase):
    """Which Claude Code install runs in a folder, and whether it is enabled.

    Claude Code reads enabledPlugins from the user settings, then the folder's
    .claude/settings.json, then .claude/settings.local.json, and the last file
    that names the plugin decides. A project or local install record applies
    in its own project folder.
    """

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.cache = plugin_copy(self.home / "cache" / "0.2.1")
        self.project = self.home / "work" / "app"
        self.project.mkdir(parents=True)

    def folder(self, doctor: dict) -> dict:
        return doctor["installed_hosts"]["claude"]["this_folder"]

    def test_user_install_is_enabled_everywhere(self) -> None:
        claude_install(self.home, [claude_record(self.cache)])
        code, doctor = run_doctor(self.home, self.project)
        self.assertEqual(code, 0)
        folder = self.folder(doctor)
        self.assertEqual(folder["status"], "enabled")
        self.assertEqual(folder["scope"], "user")
        self.assertEqual(
            folder["decided_by"], str(self.home / ".claude" / "settings.json")
        )

    def test_project_install_disabled_in_its_folder_fails(self) -> None:
        claude_install(
            self.home,
            [
                claude_record(self.cache),
                claude_record(
                    self.cache, scope="project", projectPath=str(self.project)
                ),
            ],
        )
        write_json(
            self.project / ".claude" / "settings.json",
            {"enabledPlugins": {PLUGIN_ID: False}},
        )
        code, doctor = run_doctor(self.home, self.project)
        self.assertEqual(code, 1)
        self.assertFalse(doctor["ok"])
        folder = self.folder(doctor)
        self.assertEqual(folder["status"], "installed but disabled in this folder")
        self.assertEqual(folder["scope"], "project")
        self.assertEqual(
            folder["decided_by"], str(self.project / ".claude" / "settings.json")
        )
        fixes = " ".join(folder["fixes"])
        self.assertIn(
            f"claude plugin enable {PLUGIN_ID} --scope project", fixes
        )
        self.assertIn(
            f"claude plugin uninstall {PLUGIN_ID} --scope project --keep-data", fixes
        )

    def test_local_settings_override_project_settings(self) -> None:
        claude_install(
            self.home,
            [claude_record(self.cache, scope="local", projectPath=str(self.project))],
            user_enabled=None,
        )
        write_json(
            self.project / ".claude" / "settings.json",
            {"enabledPlugins": {PLUGIN_ID: True}},
        )
        write_json(
            self.project / ".claude" / "settings.local.json",
            {"enabledPlugins": {PLUGIN_ID: False}},
        )
        code, doctor = run_doctor(self.home, self.project)
        self.assertEqual(code, 1)
        folder = self.folder(doctor)
        self.assertEqual(folder["status"], "installed but disabled in this folder")
        self.assertIn("--scope local", " ".join(folder["fixes"]))

    def test_project_install_in_an_untrusted_folder_is_still_enabled(self) -> None:
        # Claude Code 2.1.280 drops an untrusted folder's permission rules but
        # still reads its enabledPlugins, so trust is reported, not failed.
        claude_install(
            self.home,
            [
                claude_record(self.cache),
                claude_record(
                    self.cache, scope="project", projectPath=str(self.project)
                ),
            ],
        )
        write_json(
            self.project / ".claude" / "settings.json",
            {"enabledPlugins": {PLUGIN_ID: True}},
        )
        code, doctor = run_doctor(self.home, self.project)
        self.assertEqual(code, 0)
        folder = self.folder(doctor)
        self.assertEqual(folder["status"], "enabled")
        self.assertEqual(folder["scope"], "project")
        self.assertFalse(folder["trusted"])

    def test_trusted_project_folder_is_enabled(self) -> None:
        claude_install(
            self.home,
            [claude_record(self.cache, scope="project", projectPath=str(self.project))],
            user_enabled=None,
        )
        write_json(
            self.project / ".claude" / "settings.json",
            {"enabledPlugins": {PLUGIN_ID: True}},
        )
        trust(self.home, self.project)
        code, doctor = run_doctor(self.home, self.project)
        self.assertEqual(code, 0)
        folder = self.folder(doctor)
        self.assertEqual(folder["status"], "enabled")
        self.assertTrue(folder["trusted"])

    def test_trust_saved_for_a_parent_folder_counts(self) -> None:
        claude_install(self.home, [claude_record(self.cache)])
        trust(self.home, self.home / "work")
        _, doctor = run_doctor(self.home, self.project)
        self.assertTrue(self.folder(doctor)["trusted"])

    def test_project_record_for_another_folder_leaves_the_user_install(self) -> None:
        other = self.home / "work" / "other"
        claude_install(
            self.home,
            [
                claude_record(self.cache),
                claude_record(self.cache, scope="project", projectPath=str(other)),
            ],
        )
        write_json(other / ".claude" / "settings.json", {"enabledPlugins": {PLUGIN_ID: False}})
        code, doctor = run_doctor(self.home, self.project)
        self.assertEqual(code, 0)
        folder = self.folder(doctor)
        self.assertEqual(folder["status"], "enabled")
        self.assertEqual(folder["scope"], "user")

    def test_project_only_install_is_not_enabled_elsewhere(self) -> None:
        claude_install(
            self.home,
            [claude_record(self.cache, scope="project", projectPath=str(self.project))],
            user_enabled=None,
        )
        write_json(
            self.project / ".claude" / "settings.json",
            {"enabledPlugins": {PLUGIN_ID: True}},
        )
        code, doctor = run_doctor(self.home, self.home / "work" / "other")
        self.assertEqual(code, 1)
        folder = self.folder(doctor)
        self.assertEqual(folder["status"], "installed but not enabled in this folder")
        self.assertIn(f"claude plugin install {PLUGIN_ID}", " ".join(folder["fixes"]))

    def test_a_git_subfolder_uses_the_repository_settings(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        nested = self.project / "src" / "pkg"
        nested.mkdir(parents=True)
        claude_install(
            self.home,
            [claude_record(self.cache, scope="project", projectPath=str(self.project))],
            user_enabled=None,
        )
        write_json(
            self.project / ".claude" / "settings.json",
            {"enabledPlugins": {PLUGIN_ID: True}},
        )
        code, doctor = run_doctor(self.home, nested)
        self.assertEqual(code, 0)
        self.assertEqual(self.folder(doctor)["scope"], "project")

    def test_claude_config_dir_moves_user_settings_and_trust(self) -> None:
        config = self.home / "config"
        claude_install(self.home, [claude_record(self.cache)], user_enabled=None)
        (self.home / ".claude").rename(config)
        write_json(config / "settings.json", {"enabledPlugins": {PLUGIN_ID: True}})
        write_json(
            config / ".claude.json",
            {"projects": {str(self.project): {"hasTrustDialogAccepted": True}}},
        )
        with (
            isolated_home(self.home),
            mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            main(
                [
                    "--data-dir",
                    str(self.home / "data"),
                    "doctor",
                    "--json",
                    "--cwd",
                    str(self.project),
                ]
            )
        folder = json.loads(output.getvalue())["installed_hosts"]["claude"][
            "this_folder"
        ]
        self.assertEqual(folder["status"], "enabled")
        self.assertEqual(folder["decided_by"], str(config / "settings.json"))
        self.assertTrue(folder["trusted"])

    def test_text_output_names_the_folder_problem_and_both_fixes(self) -> None:
        claude_install(
            self.home,
            [
                claude_record(self.cache),
                claude_record(
                    self.cache, scope="project", projectPath=str(self.project)
                ),
            ],
        )
        write_json(
            self.project / ".claude" / "settings.json",
            {"enabledPlugins": {PLUGIN_ID: False}},
        )
        with (
            isolated_home(self.home),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            code = main(
                [
                    "--data-dir",
                    str(self.home / "data"),
                    "doctor",
                    "--cwd",
                    str(self.project),
                ]
            )
        text = output.getvalue()
        self.assertEqual(code, 1)
        self.assertIn(
            f"Claude Code in {self.project}: installed but disabled in this folder",
            text,
        )
        self.assertEqual(text.count("  fix: "), 2, text)


if __name__ == "__main__":
    unittest.main()
