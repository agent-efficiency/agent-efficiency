from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_efficiency.hook import _guard_status, run_hook
from agent_efficiency.paths import PLUGIN_ROOT
from agent_efficiency.store import Store
from agent_efficiency.verification.config import (
    MAX_CONFIG_BYTES,
    ConfigMissing,
    load_project_config,
)


CLAUDE_ENV = {"CLAUDE_PLUGIN_ROOT": "/plugin"}
MARKER = "repository-text-marker-7731"
# Nesting this deep makes the TOML parser run out of recursion depth.
DEEP_CONFIG = "version = 1\nx = " + "[" * 600 + "]" * 600 + "\n"
# Keep the machine's own git settings, such as global hooks, out of the tests.
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def guard_config(*, mode: str = "guard", timeout: int = 30) -> str:
    return f"""version = 1
mode = "{mode}"
[[checks]]
id = "unit"
command = ["python", "-c", "raise SystemExit(0)"]
applies_to = ["**/*.py"]
required_for = ["code"]
timeout_seconds = {timeout}
"""


class ConfigLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_missing_config_raises_config_missing(self) -> None:
        with self.assertRaises(ConfigMissing) as caught:
            load_project_config(self.root)
        self.assertIsInstance(caught.exception, ValueError)

    def test_invalid_config_raises_a_plain_value_error(self) -> None:
        (self.root / "agent-efficiency.toml").write_text("version = [\n")
        with self.assertRaises(ValueError) as caught:
            load_project_config(self.root)
        self.assertNotIsInstance(caught.exception, ConfigMissing)

    def test_a_config_that_fails_validation_is_not_missing(self) -> None:
        (self.root / "agent-efficiency.toml").write_text("version = 2\n")
        with self.assertRaises(ValueError) as caught:
            load_project_config(self.root)
        self.assertNotIsInstance(caught.exception, ConfigMissing)

    def assert_invalid(self) -> ValueError:
        with self.assertRaises(ValueError) as caught:
            load_project_config(self.root)
        self.assertNotIsInstance(caught.exception, ConfigMissing)
        self.assertNotIn(MARKER, str(caught.exception))
        return caught.exception

    def test_a_deeply_nested_config_is_invalid(self) -> None:
        (self.root / "agent-efficiency.toml").write_text(DEEP_CONFIG)
        self.assert_invalid()

    def test_a_config_over_the_size_limit_is_invalid(self) -> None:
        padding = f"# {MARKER}\n" * (MAX_CONFIG_BYTES // 20)
        (self.root / "agent-efficiency.toml").write_text(guard_config() + padding)
        self.assertGreater(
            (self.root / "agent-efficiency.toml").stat().st_size, MAX_CONFIG_BYTES
        )
        self.assert_invalid()

    def test_a_config_at_the_size_limit_loads(self) -> None:
        text = guard_config()
        text += "#" * (MAX_CONFIG_BYTES - len(text) - 1) + "\n"
        (self.root / "agent-efficiency.toml").write_text(text)
        self.assertEqual(load_project_config(self.root).mode, "guard")

    def test_a_directory_at_the_config_path_is_invalid(self) -> None:
        (self.root / "agent-efficiency.toml").mkdir()
        self.assert_invalid()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs named pipes")
    def test_a_named_pipe_at_the_config_path_is_invalid_without_waiting(
        self,
    ) -> None:
        os.mkfifo(self.root / "agent-efficiency.toml")
        script = (
            "import sys\n"
            "from agent_efficiency.verification.config import (\n"
            "    ConfigMissing, load_project_config)\n"
            "try:\n"
            "    load_project_config(sys.argv[1])\n"
            "except ConfigMissing:\n"
            "    print('missing')\n"
            "except ValueError:\n"
            "    print('invalid')\n"
            "else:\n"
            "    print('loaded')\n"
        )
        environment = {**os.environ, "PYTHONPATH": str(PLUGIN_ROOT / "src")}
        try:
            result = subprocess.run(
                [sys.executable, "-c", script, str(self.root)],
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            self.fail("loading a named pipe as the config did not return")
        self.assertEqual(result.stdout.strip(), "invalid", result.stderr)


class GuardStatusTests(unittest.TestCase):
    """The effective mode for each project config state and session pin."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "agent-efficiency.toml"

    def status(
        self, pinned: str | None, session_mode: str = "advise"
    ) -> tuple[str, str | None]:
        mode, state, _ = _guard_status(session_mode, str(self.root), pinned)
        return mode, state

    def digest(self) -> str:
        return load_project_config(self.root).digest

    def test_off_and_observe_sessions_keep_their_mode(self) -> None:
        self.config.write_text("version = [\n")
        for session_mode in ("off", "observe"):
            with self.subTest(session_mode=session_mode):
                self.assertEqual(
                    self.status("sha256:old", session_mode), (session_mode, None)
                )

    def test_missing_config_without_a_pin_is_advise(self) -> None:
        self.assertEqual(self.status(None), ("advise", None))
        self.assertEqual(self.status(None, "guard"), ("advise", None))

    def test_missing_config_with_a_pin_stays_guard(self) -> None:
        self.assertEqual(self.status("sha256:old"), ("guard", "config-missing"))

    def test_invalid_config_is_guard_with_or_without_a_pin(self) -> None:
        self.config.write_text(f'mode = "advise" {MARKER}\n')
        self.assertEqual(self.status(None), ("guard", "config-unreadable"))
        self.assertEqual(self.status("sha256:old"), ("guard", "config-unreadable"))

    def test_guard_config_without_a_pin_is_guard(self) -> None:
        self.config.write_text(guard_config())
        self.assertEqual(self.status(None), ("guard", None))

    def test_guard_config_matching_the_pin_is_guard(self) -> None:
        self.config.write_text(guard_config())
        self.assertEqual(self.status(self.digest()), ("guard", None))

    def test_changed_guard_config_stays_guard(self) -> None:
        self.config.write_text(guard_config())
        pinned = self.digest()
        self.config.write_text(guard_config(timeout=60))
        self.assertEqual(self.status(pinned), ("guard", "config-changed"))

    def test_config_changed_to_advise_stays_guard(self) -> None:
        self.config.write_text(guard_config())
        pinned = self.digest()
        self.config.write_text(guard_config(mode="advise"))
        self.assertEqual(self.status(pinned), ("guard", "config-changed"))

    def test_advise_config_without_a_pin_is_advise(self) -> None:
        self.config.write_text(guard_config(mode="advise"))
        self.assertEqual(self.status(None), ("advise", None))


class GuardSessionCase(unittest.TestCase):
    """A git project whose config asks for guard, and one Claude Code session."""

    def setUp(self) -> None:
        self.project_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.project_temp.cleanup)
        self.data_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.data_temp.cleanup)
        self.root = Path(self.project_temp.name)
        self.config = self.root / "agent-efficiency.toml"
        self.store = Store(self.data_temp.name)
        (self.root / "app.py").write_text("value = 1\n")
        self.config.write_text(guard_config())
        for arguments in (
            ["init", "-q"],
            ["add", "."],
            ["-c", "user.name=Test", "-c", "user.email=test@example.com"]
            + ["commit", "-qm", "base"],
        ):
            subprocess.run(["git", *arguments], cwd=self.root, check=True, env=GIT_ENV)

    def hook(self, event: str, turn: str = "turn-1", **extra: object) -> object:
        return run_hook(
            {
                "session_id": "session-1",
                "cwd": str(self.root),
                "prompt_id": turn,
                "hook_event_name": event,
                **extra,
            },
            store=self.store,
            environ=CLAUDE_ENV,
        )

    def prompt(self, text: str, turn: str = "turn-1") -> object:
        return self.hook("UserPromptSubmit", turn, prompt=text)

    def pin(self) -> object:
        return self.store.get_session("session-1")["guard_policy_digest"]


class GuardSessionTests(GuardSessionCase):
    def test_first_guard_event_pins_the_config(self) -> None:
        self.prompt("Implement a substantial code change with tests.")
        self.assertEqual(self.pin(), load_project_config(self.root).digest)

    def test_a_changed_config_is_not_pinned_again(self) -> None:
        self.prompt("Implement a substantial code change with tests.")
        first = self.pin()
        self.config.write_text(guard_config(timeout=60))
        self.hook("PreToolUse", tool_name="Read", tool_input={"file_path": "a.py"})
        self.assertEqual(self.pin(), first)

    def test_a_removed_config_keeps_the_pin(self) -> None:
        self.prompt("Implement a substantial code change with tests.")
        first = self.pin()
        self.config.unlink()
        self.hook("PreToolUse", tool_name="Read", tool_input={"file_path": "a.py"})
        self.assertEqual(self.pin(), first)


class GuardStopTests(GuardSessionCase):
    """Stop after a code edit when the project config is no longer the pinned one."""

    def edit_and_stop(self, turn: str = "turn-1", cwd: Path | None = None) -> object:
        where = {"cwd": str(cwd)} if cwd else {}
        (self.root / "app.py").write_text(f"value = {turn!r}\n")
        self.hook(
            "PostToolUse",
            turn,
            tool_name="Write",
            tool_input={"file_path": str(self.root / "app.py")},
            tool_response={"success": True},
            **where,
        )
        return self.hook("Stop", turn, stop_hook_active=False, **where)

    def start(self) -> None:
        self.prompt("Implement a substantial code change with tests.")

    def policy_ids(self) -> list[str]:
        with self.store.connect() as conn:
            rows = conn.execute("SELECT policy_id FROM interventions").fetchall()
        return [str(row["policy_id"]) for row in rows]

    def assert_blocked(self, output: object, policy_id: str, phrase: str) -> None:
        assert isinstance(output, dict)
        self.assertEqual(output["decision"], "block")
        self.assertIn(phrase, output["reason"])
        self.assertNotIn(MARKER, repr(output))
        self.assertIn(policy_id, self.policy_ids())

    def test_changed_config_asks_once_per_turn(self) -> None:
        self.start()
        self.config.write_text(guard_config(timeout=60).replace("unit", MARKER))
        output = self.edit_and_stop()
        self.assert_blocked(
            output, "guard.config-changed", "differs from the one this session pinned"
        )
        self.assertIsNone(self.hook("Stop", stop_hook_active=False))

    def test_config_changed_to_advise_still_asks(self) -> None:
        self.start()
        self.config.write_text(guard_config(mode="advise"))
        output = self.edit_and_stop()
        self.assert_blocked(output, "guard.config-changed", "$agent-efficiency advise")

    def test_unreadable_config_asks(self) -> None:
        self.start()
        self.config.write_text(f'version = 1\nmode = "advise" {MARKER}\n')
        output = self.edit_and_stop()
        self.assert_blocked(output, "guard.config-unreadable", "not a valid config")

    def test_a_deeply_nested_config_asks(self) -> None:
        self.start()
        self.config.write_text(DEEP_CONFIG)
        output = self.edit_and_stop()
        self.assert_blocked(output, "guard.config-unreadable", "not a valid config")

    def test_unreadable_config_asks_in_a_session_that_never_pinned(self) -> None:
        self.config.write_text(f"{MARKER} = [\n")
        self.start()
        self.assertIsNone(self.pin())
        output = self.edit_and_stop()
        self.assert_blocked(output, "guard.config-unreadable", "not a valid config")

    def test_removed_config_asks(self) -> None:
        self.start()
        self.config.unlink()
        output = self.edit_and_stop()
        self.assert_blocked(
            output, "guard.config-missing", "not in the working directory"
        )

    def test_an_event_from_a_subfolder_keeps_the_pin(self) -> None:
        self.start()
        first = self.pin()
        subfolder = self.root / "pkg"
        subfolder.mkdir()
        output = self.edit_and_stop(cwd=subfolder)
        self.assert_blocked(
            output, "guard.config-missing", "If the working directory changed"
        )
        self.assertEqual(self.pin(), first)

    def test_a_turn_without_code_edits_is_not_asked(self) -> None:
        self.start()
        self.config.write_text(guard_config(timeout=60))
        self.assertIsNone(self.hook("Stop", stop_hook_active=False))

    def test_restoring_the_file_returns_to_the_required_check(self) -> None:
        self.start()
        original = self.config.read_text()
        self.config.write_text(guard_config(timeout=60))
        self.config.write_text(original)
        output = self.edit_and_stop()
        assert isinstance(output, dict)
        self.assertIn("unit is missing", output["reason"])

    def test_the_user_guard_control_accepts_the_changed_file(self) -> None:
        self.start()
        self.config.write_text(guard_config(timeout=60))
        self.assert_blocked(
            self.edit_and_stop(),
            "guard.config-changed",
            "differs from the one this session pinned",
        )
        self.prompt("$agent-efficiency guard", turn="turn-2")
        self.assertEqual(self.pin(), load_project_config(self.root).digest)
        output = self.edit_and_stop(turn="turn-2")
        assert isinstance(output, dict)
        self.assertIn("unit is missing", output["reason"])

    def test_the_user_advise_control_accepts_a_file_that_left_guard(self) -> None:
        self.start()
        self.config.write_text(guard_config(mode="advise"))
        self.prompt("$agent-efficiency advise", turn="turn-2")
        self.assertIsNone(self.pin())
        output = self.edit_and_stop(turn="turn-2")
        self.assertNotIn("agent-efficiency.toml", repr(output))
        self.assertNotIn("guard.config-changed", self.policy_ids())

    def test_cursor_gets_one_followup_for_a_changed_config(self) -> None:
        base = {
            "conversation_id": "cursor-session",
            "generation_id": "turn-1",
            "cursor_version": "1.7.2",
            "workspace_roots": [str(self.root)],
        }
        run_hook(
            {
                **base,
                "hook_event_name": "beforeSubmitPrompt",
                "prompt": "Implement a substantial code change with tests.",
            },
            store=self.store,
        )
        self.config.write_text(guard_config(timeout=60))
        run_hook(
            {
                **base,
                "hook_event_name": "afterFileEdit",
                "file_path": str(self.root / "app.py"),
                "edits": [],
            },
            store=self.store,
        )
        output = run_hook(
            {**base, "hook_event_name": "stop", "loop_count": 0}, store=self.store
        )
        assert isinstance(output, dict)
        self.assertIn(
            "differs from the one this session pinned", output["followup_message"]
        )
