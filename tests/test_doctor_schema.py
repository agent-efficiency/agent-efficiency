from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from agent_efficiency import __version__, cli
from agent_efficiency.store import SCHEMA_VERSION, Store
from test_doctor import (
    claude_install,
    claude_record,
    codex_install,
    plugin_copy,
    run_doctor,
)


STORE_SOURCE = Path("src/agent_efficiency/store.py")


def install_for(home: Path, host: str) -> Path:
    if host == "claude":
        root = plugin_copy(home / "cache" / __version__)
        claude_install(home, [claude_record(root, version=__version__)])
        return root
    if host == "codex":
        return plugin_copy(codex_install(home) / __version__)
    return plugin_copy(home / ".cursor/plugins/local/agent-efficiency")


def replace_schema(root: Path, version: str) -> None:
    source = root / STORE_SOURCE
    text = source.read_text(encoding="utf-8")
    old = f'SCHEMA_VERSION = "{SCHEMA_VERSION}"'
    if text.count(old) != 1:
        raise AssertionError("fixture has no unique schema declaration")
    source.write_text(text.replace(old, f'SCHEMA_VERSION = "{version}"'))


class InstalledSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        # Host version commands do not affect schema comparisons.
        self.enterContext(mock.patch.object(cli, "_tool_version", return_value=None))
        self.enterContext(
            mock.patch.object(cli, "_hook_python", return_value=("python3", "3.11.0"))
        )

    def test_matching_schemas_are_reported_for_every_host(self) -> None:
        for host in ("claude", "codex", "cursor"):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                install_for(home, host)
                code, report = run_doctor(home)
                self.assertEqual(code, 0, report)
                self.assertEqual(report["runtime_schema_version"], SCHEMA_VERSION)
                install = report["installed_hosts"][host]["installs"][0]
                self.assertEqual(install["schema_version"], SCHEMA_VERSION)
                self.assertTrue(install["schema_matches_runtime"])
                text = cli._format_doctor(report)
                self.assertIn(f"runtime_schema_version: {SCHEMA_VERSION}", text)
                self.assertIn(f"schema: {SCHEMA_VERSION}", text)

    def test_matching_package_labels_do_not_hide_an_old_schema(self) -> None:
        for host in ("claude", "codex", "cursor"):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                installed = install_for(home, host)
                replace_schema(installed, "7")
                with mock.patch.object(cli, "_probe_hook_command") as probe:
                    code, report = run_doctor(home)
                probe.assert_not_called()
                self.assertEqual(code, 1, report)
                install = report["installed_hosts"][host]["installs"][0]
                self.assertEqual(install["schema_version"], "7")
                self.assertFalse(install["schema_matches_runtime"])
                self.assertFalse(install["ready"])
                if host != "codex":
                    self.assertEqual(install["version"], __version__)
                text = cli._format_doctor(report)
                self.assertIn(str(installed), text)
                self.assertIn("schema 7", text)
                self.assertIn(f"schema {SCHEMA_VERSION}", text)
                self.assertIn("same release", text)

    def test_a_newer_installed_schema_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            installed = install_for(home, "claude")
            replace_schema(installed, str(int(SCHEMA_VERSION) + 1))
            code, report = run_doctor(home)
        self.assertEqual(code, 1)
        self.assertFalse(report["installed_hosts"]["claude"]["ready"])

    def test_unknown_schema_does_not_start_the_installed_hook(self) -> None:
        for source in (None, "SCHEMA_VERSION = (", "SCHEMA_VERSION = str(8)\n"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                installed = install_for(home, "cursor")
                path = installed / STORE_SOURCE
                if source is None:
                    path.unlink()
                else:
                    path.write_text(source)
                with mock.patch.object(cli, "_probe_hook_command") as probe:
                    code, report = run_doctor(home)
                probe.assert_not_called()
                self.assertEqual(code, 1)
                install = report["installed_hosts"]["cursor"]["installs"][0]
                self.assertIsNone(install["schema_version"])
                self.assertFalse(install["schema_matches_runtime"])
                self.assertIn("schema: unknown", cli._format_doctor(report))

    def test_schema_check_keeps_the_existing_database_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            installed = install_for(home, "claude")
            replace_schema(installed, "7")
            store = Store(home / "data")
            store.set_setting("schema_version", "7")
            with closing(sqlite3.connect(store.paths.database)) as connection:
                before = list(connection.iterdump())
            code, _ = run_doctor(home)
            with closing(sqlite3.connect(store.paths.database)) as connection:
                after = list(connection.iterdump())
            self.assertEqual(code, 1)
            self.assertEqual(before, after)
            self.assertEqual(list(store.paths.root.glob("*.bak")), [])

    def test_an_unrelated_project_schema_is_only_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            user = plugin_copy(home / "cache/user")
            other = plugin_copy(home / "cache/other")
            replace_schema(other, "7")
            claude_install(
                home,
                [
                    claude_record(user),
                    claude_record(
                        other, scope="project", projectPath=str(home / "other")
                    ),
                ],
            )
            code, report = run_doctor(home)
        self.assertEqual(code, 0, report)
        host = report["installed_hosts"]["claude"]
        self.assertTrue(host["ready"])
        self.assertTrue(host["warnings"])
        self.assertIn("schema 7", " ".join(host["warnings"]))


class SchemaSourceTests(unittest.TestCase):
    def test_reading_the_declaration_does_not_execute_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / STORE_SOURCE
            source.parent.mkdir(parents=True)
            source.write_text(
                'raise RuntimeError("must not execute")\nSCHEMA_VERSION = "8"\n'
            )
            self.assertEqual(cli._plugin_schema_version(root), "8")

    def test_unknown_declarations_are_not_guessed(self) -> None:
        for declaration in (
            'SCHEMA_VERSION = "8"\nSCHEMA_VERSION = "7"\n',
            "SCHEMA_VERSION = 8\n",
            'SCHEMA_VERSION = "eight"\n',
            'if True:\n    SCHEMA_VERSION = "8"\n',
            'SCHEMA_VERSION = "8"\ninvalid syntax\n',
        ):
            with (
                self.subTest(declaration=declaration),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                source = root / STORE_SOURCE
                source.parent.mkdir(parents=True)
                source.write_text(declaration)
                self.assertIsNone(cli._plugin_schema_version(root))

    def test_a_store_file_outside_the_plugin_is_not_used(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "plugin"
            source = root / STORE_SOURCE
            source.parent.mkdir(parents=True)
            outside = Path(temp) / "store.py"
            outside.write_text('SCHEMA_VERSION = "8"\n')
            source.symlink_to(outside)
            self.assertIsNone(cli._plugin_schema_version(root))
