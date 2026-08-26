from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_efficiency.hook import run_hook
from agent_efficiency.store import Store
from agent_efficiency.verification.config import load_project_config
from agent_efficiency.verification.runner import run_checks


class GuardModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.project_temp.cleanup)
        self.data_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.data_temp.cleanup)
        self.root = Path(self.project_temp.name)
        self.store = Store(self.data_temp.name)
        (self.root / "app.py").write_text("value = 1\n")
        (self.root / "agent-efficiency.toml").write_text(
            """version = 1
mode = "guard"
[[checks]]
id = "unit"
command = ["python", "-c", "raise SystemExit(0)"]
applies_to = ["**/*.py"]
required_for = ["code"]
timeout_seconds = 30
"""
        )
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=self.root, check=True
        )
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.root, check=True)

    def _claude_or_codex(self, host: str) -> dict:
        session = f"{host}-session"
        env = (
            {"CLAUDE_PLUGIN_ROOT": "/plugin"}
            if host == "claude"
            else {"PLUGIN_ROOT": "/plugin"}
        )
        base = {
            "session_id": session,
            "cwd": str(self.root),
            "prompt_id": "turn-1",
        }
        run_hook(
            {
                **base,
                "hook_event_name": "UserPromptSubmit",
                "prompt": "Implement a substantial code change with tests.",
            },
            store=self.store,
            environ=env,
        )
        run_hook(
            {
                **base,
                "hook_event_name": "PostToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(self.root / "app.py")},
                "tool_response": {"success": True},
            },
            store=self.store,
            environ=env,
        )
        return run_hook(
            {**base, "hook_event_name": "Stop", "stop_hook_active": False},
            store=self.store,
            environ=env,
        )

    def test_claude_and_codex_continue_for_missing_required_receipt(self) -> None:
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                output = self._claude_or_codex(host)
                self.assertEqual(output["decision"], "block")
                self.assertIn("unit is missing", output["reason"])

    def test_cursor_uses_one_followup_for_missing_required_receipt(self) -> None:
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
            {**base, "hook_event_name": "stop", "loop_count": 0},
            store=self.store,
        )
        self.assertIn("followup_message", output)
        repeated = run_hook(
            {**base, "hook_event_name": "stop", "loop_count": 1},
            store=self.store,
        )
        self.assertIsNone(repeated)

    def test_unmatched_check_pattern_does_not_block_guard(self) -> None:
        (self.root / "README.md").write_text("documentation change\n")
        output = self._claude_or_codex("claude")
        self.assertIsNone(output)

    def test_current_receipt_allows_stop(self) -> None:
        config = load_project_config(self.root)
        receipts = run_checks(config, config.checks, self.store)
        self.assertEqual(receipts[0]["result"], "pass")
        output = self._claude_or_codex("claude")
        self.assertIsNone(output)


if __name__ == "__main__":
    unittest.main()
