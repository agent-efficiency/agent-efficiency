from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_efficiency.hook import _guard_status, run_hook
from agent_efficiency.store import Store
from agent_efficiency.verification.config import ConfigMissing, load_project_config


CLAUDE_ENV = {"CLAUDE_PLUGIN_ROOT": "/plugin"}
MARKER = "repository-text-marker-7731"
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
