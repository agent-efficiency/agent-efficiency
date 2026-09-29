"""One data folder for every host, and the one-time copy of a host store."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from unittest import mock

from agent_efficiency import host_data
from agent_efficiency.cli import main
from agent_efficiency.paths import PLUGIN_ROOT, data_dir
from agent_efficiency.store import Store
from tests.test_doctor import isolated_environment, isolated_home

HOOK = PLUGIN_ROOT / "scripts" / "agent_efficiency_hook.py"
CLAUDE_DATA = Path(".claude") / "plugins" / "data" / "agent-efficiency-agent-efficiency"
CODEX_DATA = Path(".codex") / "plugins" / "data" / "agent-efficiency-agent-efficiency"


def default_folder(home: Path) -> Path:
    return (home / ".local" / "share" / "agent-efficiency").resolve()


def host_store(folder: Path, session_id: str) -> Store:
    """Make a store the way an earlier release left it in a host folder."""

    store = Store(folder)
    store.ensure_session(session_id, host="claude", cwd="/work/app")
    store.set_default_mode("observe")
    (folder / "vault.json").write_text(
        json.dumps({"schema": 1, "trees": []}) + "\n", encoding="utf-8"
    )
    (store.paths.capability_packs / "example.json").write_text(
        "{}\n", encoding="utf-8"
    )
    return store


def run_hook(environment: dict[str, str], payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-S", str(HOOK)],
        input=json.dumps(payload),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def run_cli(environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess:
    child = dict(environment)
    child["PYTHONPATH"] = str(PLUGIN_ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "agent_efficiency", *arguments],
        env=child,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _adopt(target: str, source: str) -> str | None:
    copied = host_data.adopt_host_store(
        Path(target), {"CLAUDE_PLUGIN_DATA": source}
    )
    return str(copied) if copied else None


class DataFolderResolutionTests(unittest.TestCase):
    def test_host_data_variables_do_not_choose_the_store(self) -> None:
        home = Path("/nonexistent-home")
        for variable in ("CLAUDE_PLUGIN_DATA", "PLUGIN_DATA"):
            with self.subTest(variable=variable):
                environ = {"HOME": str(home), variable: "/elsewhere/plugin-data"}
                self.assertEqual(data_dir(environ=environ), default_folder(home))

    def test_override_and_xdg_still_apply(self) -> None:
        environ = {
            "HOME": "/nonexistent-home",
            "XDG_DATA_HOME": "/xdg",
            "CLAUDE_PLUGIN_DATA": "/elsewhere",
        }
        self.assertEqual(data_dir(environ=environ), Path("/xdg/agent-efficiency"))
        environ["AGENT_EFFICIENCY_DATA"] = "/chosen"
        self.assertEqual(data_dir(environ=environ), Path("/chosen"))
        self.assertEqual(data_dir("/flag", environ=environ), Path("/flag"))

    def test_hook_and_terminal_use_one_store(self) -> None:
        for variable, relative in (
            ("CLAUDE_PLUGIN_DATA", CLAUDE_DATA),
            ("PLUGIN_DATA", CODEX_DATA),
        ):
            with self.subTest(variable=variable), tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                project = home / "app"
                project.mkdir()
                hook_environment = isolated_environment(home)
                hook_environment[variable] = str(home / relative)
                started = run_hook(
                    hook_environment,
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": f"one-store-{variable}",
                        "cwd": str(project),
                        "source": "startup",
                    },
                )
                self.assertEqual(started.returncode, 0, started.stderr)
                self.assertFalse((home / relative / "agent-efficiency.db").exists())
                status = run_cli(
                    isolated_environment(home), "status", "--json"
                )
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertEqual(
                    json.loads(status.stdout)["session_id"],
                    f"one-store-{variable}",
                )


class HostStoreCopyTests(unittest.TestCase):
    def test_hook_copies_a_host_store_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            source = home / CLAUDE_DATA
            host_store(source, "before-upgrade")
            environment = isolated_environment(home)
            environment["CLAUDE_PLUGIN_DATA"] = str(source)
            payload = {
                "hook_event_name": "SessionStart",
                "session_id": "after-upgrade",
                "cwd": str(home),
                "source": "startup",
            }
            self.assertEqual(run_hook(environment, payload).returncode, 0)
            target = default_folder(home)
            store = Store(target)
            self.assertIsNotNone(store.get_session("before-upgrade"))
            self.assertIsNotNone(store.get_session("after-upgrade"))
            self.assertEqual(store.default_mode(), "observe")
            self.assertTrue((target / "vault.json").is_file())
            self.assertTrue(
                (target / "knowledge" / "packs" / "example.json").is_file()
            )
            record = json.loads(
                (target / host_data.COPY_RECORD).read_text(encoding="utf-8")
            )
            self.assertEqual(record["source"], str(source.resolve()))

            Store(source).ensure_session(
                "later-in-old-store", host="claude", cwd="/work/app"
            )
            payload["session_id"] = "second-session"
            self.assertEqual(run_hook(environment, payload).returncode, 0)
            self.assertIsNone(store.get_session("later-in-old-store"))
            self.assertIsNotNone(store.get_session("second-session"))
            leftovers = [
                path.name
                for path in target.iterdir()
                if path.name.endswith(".tmp") or path.name.endswith(".lock")
            ]
            self.assertEqual(leftovers, [])

    def test_copy_includes_changes_still_in_the_write_ahead_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "plugin"
            store = host_store(source, "checkpointed")
            holder = sqlite3.connect(store.paths.database)
            try:
                # An open reader keeps the log from being folded back into
                # the database file when the writer closes.
                holder.execute("SELECT count(*) FROM sessions").fetchall()
                store.ensure_session("only-in-wal", host="claude", cwd="/work/app")
                self.assertGreater(
                    Path(f"{store.paths.database}-wal").stat().st_size, 0
                )
                target = Path(temp) / "data"
                copied = host_data.adopt_host_store(
                    target, {"CLAUDE_PLUGIN_DATA": str(source)}
                )
            finally:
                holder.close()
            self.assertEqual(copied, source.resolve())
            self.assertIsNotNone(Store(target).get_session("only-in-wal"))

    def test_existing_store_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "plugin"
            host_store(source, "plugin-session")
            target = Path(temp) / "data"
            Store(target).ensure_session(
                "own-session", host="claude", cwd="/work/app"
            )
            copied = host_data.adopt_host_store(
                target, {"CLAUDE_PLUGIN_DATA": str(source)}
            )
            self.assertIsNone(copied)
            store = Store(target)
            self.assertIsNone(store.get_session("plugin-session"))
            self.assertIsNotNone(store.get_session("own-session"))

    def test_existing_vault_list_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "plugin"
            host_store(source, "plugin-session")
            target = Path(temp) / "data"
            target.mkdir()
            own = json.dumps({"schema": 1, "trees": ["/own/tree"]}) + "\n"
            (target / "vault.json").write_text(own, encoding="utf-8")
            host_data.adopt_host_store(target, {"CLAUDE_PLUGIN_DATA": str(source)})
            self.assertEqual(
                (target / "vault.json").read_text(encoding="utf-8"), own
            )
            self.assertIsNotNone(Store(target).get_session("plugin-session"))

    def test_racing_hooks_copy_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "plugin"
            host_store(source, "raced")
            target = Path(temp) / "data"
            with ProcessPoolExecutor(max_workers=8) as pool:
                results = list(
                    pool.map(_adopt, [str(target)] * 16, [str(source)] * 16)
                )
            self.assertEqual(
                [result for result in results if result], [str(source.resolve())]
            )
            self.assertIsNotNone(Store(target).get_session("raced"))
            leftovers = [
                path.name
                for path in target.iterdir()
                if path.name.endswith(".tmp") or path.name.endswith(".lock")
            ]
            self.assertEqual(leftovers, [])

    def test_a_held_lock_blocks_the_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "plugin"
            host_store(source, "locked")
            target = Path(temp) / "data"
            target.mkdir()
            (target / host_data.LOCK_NAME).write_text("", encoding="utf-8")
            with mock.patch.object(host_data, "WAIT_SECONDS", 0.05):
                copied = host_data.adopt_host_store(
                    target, {"CLAUDE_PLUGIN_DATA": str(source)}
                )
            self.assertIsNone(copied)
            self.assertFalse((target / "agent-efficiency.db").exists())

    def test_a_stale_lock_is_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "plugin"
            host_store(source, "stale-lock")
            target = Path(temp) / "data"
            target.mkdir()
            lock = target / host_data.LOCK_NAME
            lock.write_text("", encoding="utf-8")
            old = time.time() - host_data.LOCK_STALE_SECONDS - 5
            os.utime(lock, (old, old))
            copied = host_data.adopt_host_store(
                target, {"CLAUDE_PLUGIN_DATA": str(source)}
            )
            self.assertEqual(copied, source.resolve())
            self.assertFalse(lock.exists())

    def test_terminal_copies_a_host_store_into_the_default_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            host_store(home / CLAUDE_DATA, "terminal-first")
            status = run_cli(isolated_environment(home), "status", "--json")
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)["session_id"], "terminal-first")

    def test_terminal_leaves_an_explicit_folder_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            host_store(home / CLAUDE_DATA, "not-copied")
            chosen = home / "chosen"
            status = run_cli(
                isolated_environment(home), "--data-dir", str(chosen), "status", "--json"
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIsNone(json.loads(status.stdout)["session"])
            self.assertFalse((chosen / host_data.COPY_RECORD).exists())


class UnusedStoreReportTests(unittest.TestCase):
    def _doctor(self, home: Path) -> dict:
        with (
            isolated_home(home),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            main(["doctor", "--json"])
        return json.loads(output.getvalue())

    def test_doctor_reports_a_store_it_did_not_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            Store(default_folder(home)).ensure_session(
                "own", host="claude", cwd="/work/app"
            )
            host_store(home / CLAUDE_DATA, "left-behind")
            host_store(home / CODEX_DATA, "left-behind-too")
            doctor = self._doctor(home)
        unused = {item["path"]: item for item in doctor["unused_data_dirs"]}
        self.assertEqual(
            set(unused),
            {
                str((home / CLAUDE_DATA).resolve()),
                str((home / CODEX_DATA).resolve()),
            },
        )
        for item in unused.values():
            self.assertFalse(item["copied"])
            self.assertIn("not copied", item["status"])
        self.assertTrue(doctor["ok"])

    def test_doctor_reports_a_store_it_copied(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            source = home / CLAUDE_DATA
            host_store(source, "copied")
            doctor = self._doctor(home)
        self.assertEqual(
            [(item["path"], item["copied"]) for item in doctor["unused_data_dirs"]],
            [(str(source.resolve()), True)],
        )
        self.assertEqual(doctor["data_dir"], str(default_folder(home)))

    def test_doctor_reports_nothing_without_host_stores(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            doctor = self._doctor(Path(temp))
        self.assertEqual(doctor["unused_data_dirs"], [])


if __name__ == "__main__":
    unittest.main()
