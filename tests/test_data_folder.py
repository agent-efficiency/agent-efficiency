"""One data folder for every host, and the one-time copy of a host store."""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
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


def leftovers(target: Path) -> list[str]:
    """Temporary files a copy left behind. The lock file stays by design."""

    return sorted(
        str(path.relative_to(target))
        for path in target.rglob("*")
        if ".tmp" in path.name
    )


def lock_copy(target: Path):
    """Hold the copy lock the way a copier does, from this process."""

    target.mkdir(parents=True, exist_ok=True)
    handle = open(target / host_data.LOCK_NAME, "a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def lock_database(path: Path) -> sqlite3.Connection:
    """Hold an exclusive lock on a database so no reader can start."""

    holder = sqlite3.connect(path, timeout=5, check_same_thread=False)
    holder.execute("PRAGMA locking_mode = EXCLUSIVE")
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("UPDATE settings SET value = value WHERE key = 'default_mode'")
    return holder


def hook_payload(home: Path, session_id: str) -> dict:
    return {
        "hook_event_name": "SessionStart",
        "session_id": session_id,
        "cwd": str(home),
        "source": "startup",
    }


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
            self.assertEqual(leftovers(target), [])

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
            self.assertEqual(leftovers(target), [])

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

    def test_hooks_leave_an_explicit_folder_alone(self) -> None:
        for variable in ("CLAUDE_PLUGIN_DATA", "PLUGIN_DATA"):
            with self.subTest(variable=variable), tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                source = home / CLAUDE_DATA
                host_store(source, "old-history")
                chosen = home / "chosen"
                environment = isolated_environment(home)
                environment[variable] = str(source)
                environment["AGENT_EFFICIENCY_DATA"] = str(chosen)
                completed = run_hook(
                    environment,
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": "new-session",
                        "cwd": str(home),
                        "source": "startup",
                    },
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                store = Store(chosen)
                self.assertIsNotNone(store.get_session("new-session"))
                self.assertIsNone(store.get_session("old-history"))
                self.assertFalse((chosen / "vault.json").exists())
                self.assertFalse((chosen / host_data.COPY_RECORD).exists())

    def test_an_explicit_folder_never_receives_a_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            source = home / CLAUDE_DATA
            host_store(source, "old-history")
            chosen = home / "chosen"
            environ = {
                "HOME": str(home),
                "CLAUDE_PLUGIN_DATA": str(source),
                "AGENT_EFFICIENCY_DATA": str(chosen),
            }
            host_data.settle_data_dir(None, environ)
            self.assertFalse((chosen / "agent-efficiency.db").exists())
            host_data.settle_data_dir(
                chosen, {"HOME": str(home), "PLUGIN_DATA": str(source)}
            )
            self.assertFalse((chosen / "agent-efficiency.db").exists())


class CopyProtocolTests(unittest.TestCase):
    """A pending copy is finished or retried, and never replaced by a new store."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.source = self.home / CLAUDE_DATA
        host_store(self.source, "old-history")
        self.target = default_folder(self.home)
        self.environment = isolated_environment(self.home)
        self.environment["CLAUDE_PLUGIN_DATA"] = str(self.source)

    def hook(self, session_id: str) -> subprocess.CompletedProcess:
        return run_hook(self.environment, hook_payload(self.home, session_id))

    def assert_history(self, *sessions: str) -> None:
        store = Store(self.target)
        for session in ("old-history", *sessions):
            self.assertIsNotNone(store.get_session(session), session)

    def test_a_hook_waiting_on_a_copy_creates_no_store(self) -> None:
        handle = lock_copy(self.target)
        try:
            started = time.monotonic()
            completed = self.hook("while-copying")
            elapsed = time.monotonic() - started
        finally:
            handle.close()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertLess(elapsed, 2.0)
        self.assertFalse((self.target / "agent-efficiency.db").exists())
        self.assertEqual(self.hook("after-copy").returncode, 0)
        self.assert_history("after-copy")

    def test_a_killed_copier_is_finished_by_the_next_run(self) -> None:
        script = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "from agent_efficiency import host_data\n"
            "target = Path(sys.argv[1])\n"
            "with host_data.copy_lock(target, None):\n"
            "    (target / '.agent-efficiency.db.copy.tmp').write_bytes(b'partial')\n"
            "    (target / '.vault.json.copy.tmp').write_bytes(b'partial')\n"
            "    print('copying', flush=True)\n"
            "    time.sleep(60)\n"
        )
        self.target.mkdir(parents=True)
        copier = subprocess.Popen(
            [sys.executable, "-c", script, str(self.target)],
            env={**self.environment, "PYTHONPATH": str(PLUGIN_ROOT / "src")},
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(copier.stdout.readline().strip(), "copying")
            self.assertEqual(self.hook("during").returncode, 0)
            self.assertFalse((self.target / "agent-efficiency.db").exists())
        finally:
            copier.kill()
            copier.wait()
            copier.stdout.close()
        self.assertEqual(self.hook("after-kill").returncode, 0)
        self.assert_history("after-kill")
        self.assertEqual(leftovers(self.target), [])

    def test_a_locked_source_ends_the_hook_inside_its_budget(self) -> None:
        holder = lock_database(self.source / "agent-efficiency.db")
        try:
            started = time.monotonic()
            completed = self.hook("while-locked")
            elapsed = time.monotonic() - started
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertLess(elapsed, 2.0)
        self.assertFalse((self.target / "agent-efficiency.db").exists())
        self.assertEqual(leftovers(self.target), [])
        self.assertEqual(self.hook("unlocked").returncode, 0)
        self.assert_history("unlocked")

    def test_repeated_failures_stay_retryable(self) -> None:
        failing = mock.patch.object(
            host_data,
            "_copy_database",
            side_effect=sqlite3.DatabaseError("disk I/O error"),
        )
        with failing:
            for _ in range(3):
                with self.assertRaises(host_data.CopyPending) as raised:
                    host_data.settle_data_dir(
                        environ=self.environment, deadline=time.monotonic() + 5
                    )
                self.assertIn("disk I/O error", str(raised.exception))
                self.assertFalse((self.target / "agent-efficiency.db").exists())
        host_data.settle_data_dir(environ=self.environment)
        self.assert_history()

    def test_publishing_never_replaces_a_store_that_appeared(self) -> None:
        real_link = os.link

        def appear_then_link(source: str, destination: str) -> None:
            if str(destination).endswith("agent-efficiency.db"):
                Path(destination).write_bytes(b"someone else")
            real_link(source, destination)

        with mock.patch.object(host_data.os, "link", side_effect=appear_then_link):
            copied = host_data.adopt_host_store(
                self.target, {"CLAUDE_PLUGIN_DATA": str(self.source)}
            )
        self.assertIsNone(copied)
        self.assertEqual(
            (self.target / "agent-efficiency.db").read_bytes(), b"someone else"
        )
        self.assertEqual(leftovers(self.target), [])

    def test_without_hard_links_publishing_still_refuses_to_replace(self) -> None:
        def appear_then_fail(source: str, destination: str) -> None:
            if str(destination).endswith("agent-efficiency.db"):
                Path(destination).write_bytes(b"someone else")
            raise PermissionError("hard links are not supported here")

        with mock.patch.object(host_data.os, "link", side_effect=appear_then_fail):
            host_data.adopt_host_store(
                self.target, {"CLAUDE_PLUGIN_DATA": str(self.source)}
            )
        self.assertEqual(
            (self.target / "agent-efficiency.db").read_bytes(), b"someone else"
        )

    def test_without_hard_links_a_copy_still_lands(self) -> None:
        with mock.patch.object(
            host_data.os, "link", side_effect=PermissionError("no hard links")
        ):
            copied = host_data.adopt_host_store(
                self.target, {"CLAUDE_PLUGIN_DATA": str(self.source)}
            )
        self.assertEqual(copied, self.source.resolve())
        self.assert_history()

    def test_a_failed_record_does_not_hide_the_copy(self) -> None:
        with mock.patch.object(
            host_data, "_write_record", side_effect=OSError("disk full")
        ):
            host_data.settle_data_dir(environ=self.environment)
        self.assert_history()
        self.assertFalse((self.target / host_data.COPY_RECORD).exists())
        (unused,) = host_data.unused_host_stores(self.target, self.environment)
        self.assertTrue(unused["copied"])
        self.assertIn("copied into", unused["status"])

    def test_the_terminal_finishes_a_copy_without_a_deadline(self) -> None:
        holder = lock_database(self.source / "agent-efficiency.db")
        release = threading.Timer(2.5, lambda: (holder.rollback(), holder.close()))
        release.start()
        try:
            status = run_cli(self.environment, "status", "--json")
        finally:
            release.join()
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assert_history()

    def test_the_terminal_stops_when_the_copy_fails(self) -> None:
        (self.source / "agent-efficiency.db").write_bytes(b"not a database" * 100)
        for sidecar in ("-wal", "-shm"):
            Path(f"{self.source / 'agent-efficiency.db'}{sidecar}").unlink(
                missing_ok=True
            )
        status = run_cli(self.environment, "status")
        self.assertEqual(status.returncode, 2)
        self.assertIn("could not finish copying", status.stderr)
        self.assertIn(str(self.source.resolve()), status.stderr)
        self.assertFalse((self.target / "agent-efficiency.db").exists())
        doctor = run_cli(self.environment, "doctor", "--json")
        self.assertEqual(doctor.returncode, 1)
        result = json.loads(doctor.stdout)
        self.assertFalse(result["ok"])
        self.assertIn("could not finish copying", result["data_copy"])
        (unused,) = result["unused_data_dirs"]
        self.assertFalse(unused["copied"])
        self.assertIn("not copied yet", unused["status"])
        self.assertFalse((self.target / "agent-efficiency.db").exists())
        text = run_cli(self.environment, "doctor")
        self.assertEqual(text.returncode, 1)
        self.assertIn("data_copy: Agent Efficiency could not finish", text.stdout)

    def test_a_terminal_that_cannot_see_the_source_waits_for_the_copy(self) -> None:
        # The copier finds the old store through a custom CLAUDE_CONFIG_DIR
        # that the terminal does not have.
        config = self.home / "custom-claude"
        source = config / "plugins" / "data" / "agent-efficiency-agent-efficiency"
        source.parent.mkdir(parents=True)
        self.source.rename(source)
        copier_environment = isolated_environment(self.home)
        copier_environment["CLAUDE_CONFIG_DIR"] = str(config)
        copier_environment["PYTHONPATH"] = str(PLUGIN_ROOT / "src")
        script = (
            "import time\n"
            "from agent_efficiency import host_data\n"
            "original = host_data._copy_database\n"
            "def slow(*arguments):\n"
            "    print('copying', flush=True)\n"
            "    time.sleep(1.5)\n"
            "    return original(*arguments)\n"
            "host_data._copy_database = slow\n"
            "host_data.settle_data_dir()\n"
        )
        copier = subprocess.Popen(
            [sys.executable, "-c", script],
            env=copier_environment,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(copier.stdout.readline().strip(), "copying")
            status = run_cli(isolated_environment(self.home), "status", "--json")
        finally:
            copier.wait(timeout=30)
            copier.stdout.close()
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["session_id"], "old-history")
        self.assert_history()

    def test_without_hard_links_a_hook_writing_meanwhile_is_kept(self) -> None:
        hook_environment = isolated_environment(self.home)
        hook_environment["AGENT_EFFICIENCY_DATA"] = str(self.target)
        started: list[subprocess.Popen] = []

        def hook_meanwhile(source: str, destination: str) -> None:
            if str(destination).endswith("agent-efficiency.db"):
                started.append(
                    subprocess.Popen(
                        [sys.executable, "-S", str(HOOK)],
                        stdin=subprocess.PIPE,
                        text=True,
                        env=hook_environment,
                    )
                )
                started[0].stdin.write(
                    json.dumps(hook_payload(self.home, "concurrent"))
                )
                started[0].stdin.close()
                time.sleep(0.5)
            raise PermissionError("hard links are not supported here")

        with mock.patch.object(host_data.os, "link", side_effect=hook_meanwhile):
            copied = host_data.adopt_host_store(
                self.target, {"CLAUDE_PLUGIN_DATA": str(self.source)}
            )
        self.assertEqual(started[0].wait(timeout=30), 0)
        self.assertEqual(copied, self.source.resolve())
        self.assert_history("concurrent")

    def unreadable(self, folder: Path) -> None:
        if os.geteuid() == 0:
            self.skipTest("root reads folders without permission")
        folder.chmod(0)
        self.addCleanup(folder.chmod, 0o700)

    def test_an_unreadable_plugin_data_folder_is_not_an_empty_one(self) -> None:
        terminal = isolated_environment(self.home)
        for folder in (self.source.parent, self.source):
            with self.subTest(folder=folder.name):
                self.unreadable(folder)
                status = run_cli(terminal, "status", "--json")
                self.assertEqual(status.returncode, 2, status.stdout)
                self.assertIn("could not read", status.stderr)
                self.assertIn(str(folder), status.stderr)
                completed = run_hook(terminal, hook_payload(self.home, "hidden"))
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stdout, "")
                doctor = run_cli(terminal, "doctor", "--json")
                self.assertEqual(doctor.returncode, 1)
                self.assertIn(str(folder), json.loads(doctor.stdout)["data_copy"])
                self.assertFalse((self.target / "agent-efficiency.db").exists())
                folder.chmod(0o700)
        status = run_cli(terminal, "status", "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["session_id"], "old-history")
        self.assert_history()

    def test_a_missing_plugin_data_folder_allows_a_fresh_store(self) -> None:
        empty = isolated_environment(self.home / "elsewhere")
        (self.home / "elsewhere").mkdir()
        status = run_cli(empty, "status", "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIsNone(json.loads(status.stdout)["session"])

    def test_a_codex_hook_waits_for_the_claude_store_too(self) -> None:
        environment = isolated_environment(self.home)
        environment["PLUGIN_DATA"] = str(self.home / CODEX_DATA)
        (self.home / CODEX_DATA).mkdir(parents=True)
        completed = run_hook(environment, hook_payload(self.home, "codex-first"))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assert_history("codex-first")


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
        target = default_folder(home)
        for item in unused.values():
            self.assertFalse(item["copied"])
            self.assertIn("not copied", item["status"])
            steps = item["recovery"]
            self.assertIn("quit", steps)
            self.assertIn(str(target / "agent-efficiency.db"), steps)
            self.assertIn("agent-efficiency doctor", steps)
            self.assertIn("keep", steps)
        self.assertTrue(doctor["ok"])

    def test_the_recovery_steps_bring_the_old_store_over(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            target = default_folder(home)
            Store(target).ensure_session("own", host="claude", cwd="/work/app")
            host_store(home / CLAUDE_DATA, "left-behind")
            aside = home / "aside"
            aside.mkdir()
            for suffix in ("", "-wal", "-shm"):
                moved = Path(f"{target / 'agent-efficiency.db'}{suffix}")
                if moved.exists():
                    moved.rename(aside / moved.name)
            doctor = self._doctor(home)
            self.assertTrue(doctor["ok"])
            self.assertTrue(doctor["unused_data_dirs"][0]["copied"])
            self.assertIsNotNone(Store(target).get_session("left-behind"))
            self.assertIsNotNone(Store(aside).get_session("own"))

    def test_doctor_text_gives_the_recovery_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            Store(default_folder(home)).ensure_session(
                "own", host="claude", cwd="/work/app"
            )
            host_store(home / CLAUDE_DATA, "left-behind")
            with (
                isolated_home(home),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                main(["doctor"])
        self.assertIn("    to copy it:", output.getvalue())

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
