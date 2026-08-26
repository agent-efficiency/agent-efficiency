from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from agent_efficiency.hook import detect_host, parse_control, run_hook
from agent_efficiency.store import Store


CLAUDE_ENV = {"CLAUDE_PLUGIN_ROOT": "/plugin"}
CODEX_ENV = {"PLUGIN_ROOT": "/plugin", "CLAUDE_PLUGIN_ROOT": "/plugin"}
CURSOR_PAYLOAD = {
    "conversation_id": "cursor-conversation",
    "generation_id": "cursor-generation",
    "cursor_version": "1.7.2",
    "workspace_roots": ["/work/cursor-project"],
}


class HookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.base = {
            "session_id": "session-1",
            "cwd": "/work/project",
            "prompt_id": "turn-1",
        }

    def payload(self, event: str, **extra: object) -> dict[str, object]:
        return {**self.base, "hook_event_name": event, **extra}

    def test_cursor_payload_is_detected_and_uses_cursor_control_schema(self) -> None:
        payload = {
            **CURSOR_PAYLOAD,
            "hook_event_name": "beforeSubmitPrompt",
            "prompt": "$agent-efficiency off",
        }
        self.assertEqual(detect_host(payload), "cursor")
        output = run_hook(payload, store=self.store)
        self.assertEqual(output["continue"], False)
        self.assertIn("user_message", output)
        self.assertEqual(
            self.store.get_session("cursor-conversation")["host"], "cursor"
        )
        self.assertEqual(
            self.store.get_session("cursor-conversation")["project_name"],
            "cursor-project",
        )

    def test_cursor_session_start_injects_quality_contract(self) -> None:
        output = run_hook(
            {**CURSOR_PAYLOAD, "hook_event_name": "sessionStart"},
            store=self.store,
        )
        self.assertIn("additional_context", output)
        self.assertIn("acceptance checks", output["additional_context"])
        self.assertIn("not stored", output["additional_context"])

    def test_cursor_code_edit_gets_post_tool_checkpoint(self) -> None:
        run_hook(
            {
                **CURSOR_PAYLOAD,
                "hook_event_name": "beforeSubmitPrompt",
                "prompt": "Implement a substantial feature with tests.",
            },
            store=self.store,
        )
        output = run_hook(
            {
                **CURSOR_PAYLOAD,
                "hook_event_name": "postToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": "/work/project/app.py", "content": "x"},
                "tool_output": '{"success":true}',
                "duration": 8,
            },
            store=self.store,
        )
        self.assertIn("additional_context", output)
        self.assertIn("validation", output["additional_context"].casefold())

    def test_cursor_native_file_and_shell_events_update_turn_facts(self) -> None:
        run_hook(
            {
                **CURSOR_PAYLOAD,
                "hook_event_name": "beforeSubmitPrompt",
                "prompt": "Implement a substantial parser change with tests.",
            },
            store=self.store,
        )
        run_hook(
            {
                **CURSOR_PAYLOAD,
                "hook_event_name": "afterFileEdit",
                "file_path": "/work/cursor-project/parser.py",
                "edits": [{"old_string": "x", "new_string": "y"}],
            },
            store=self.store,
        )
        run_hook(
            {
                **CURSOR_PAYLOAD,
                "hook_event_name": "afterShellExecution",
                "command": "python -m pytest tests/test_parser.py",
                "output": "private test output",
                "duration": 12,
            },
            store=self.store,
        )
        turn = self.store.get_turn(
            "cursor-conversation",
            self.store.latest_turn_key("cursor-conversation"),
        )
        self.assertEqual(turn["code_edit_count"], 1)
        self.assertEqual(turn["validation_count"], 1)
        self.assertNotIn(
            b"private test output",
            Path(self.store.paths.database).read_bytes(),
        )

    def test_cursor_help_control_uses_cursor_schema(self) -> None:
        output = run_hook(
            {
                **CURSOR_PAYLOAD,
                "hook_event_name": "beforeSubmitPrompt",
                "prompt": "$agent-efficiency help",
            },
            store=self.store,
        )
        self.assertEqual(output["continue"], False)
        self.assertIn("user_message", output)
        self.assertNotIn("stopReason", output)

    def test_cursor_capability_guidance_is_deferred_to_post_tool(self) -> None:
        prompt_output = run_hook(
            {
                **CURSOR_PAYLOAD,
                "hook_event_name": "beforeSubmitPrompt",
                "prompt": "Implement a complete authentication flow with tests, error handling, documentation, and explicit rollback behavior.",
            },
            store=self.store,
        )
        self.assertIsNone(prompt_output)
        output = run_hook(
            {
                **CURSOR_PAYLOAD,
                "hook_event_name": "postToolUse",
                "tool_name": "Shell",
                "tool_input": {"command": "python -m pytest tests/test_app.py"},
                "tool_output": '{"exitCode":0}',
                "duration": 12,
            },
            store=self.store,
        )
        self.assertIn("additional_context", output)
        self.assertIn("success", output["additional_context"].casefold())
        self.assertEqual(
            self.store.knowledge_receipt_summary("cursor-conversation")["emitted"],
            1,
        )

    def test_cursor_post_tool_nudge_uses_additional_context(self) -> None:
        prompt = {
            **CURSOR_PAYLOAD,
            "hook_event_name": "beforeSubmitPrompt",
            "prompt": "Implement a substantial feature with tests and documentation.",
        }
        run_hook(prompt, store=self.store)
        tool = {
            **CURSOR_PAYLOAD,
            "hook_event_name": "preToolUse",
            "tool_name": "Shell",
            "tool_input": {"command": "pip install package"},
        }
        output = None
        for _ in range(2):
            output = run_hook(
                {
                    **tool,
                    "hook_event_name": "postToolUse",
                    "tool_output": '{"exitCode":0}',
                    "duration": 4,
                },
                store=self.store,
            )
        self.assertIn("additional_context", output)
        self.assertNotIn("systemMessage", output)

    def test_control_phrases_are_exact_and_cross_surface(self) -> None:
        self.assertEqual(parse_control("$agent-efficiency on"), "on")
        self.assertEqual(
            parse_control("/agent-efficiency:agent-efficiency observe"), "observe"
        )
        self.assertIsNone(parse_control("$agent-efficiency on and build this"))

    def test_guard_control_requires_project_consent(self) -> None:
        output = run_hook(
            self.payload("UserPromptSubmit", prompt="$agent-efficiency guard"),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        self.assertIn("requires", output["stopReason"])
        self.assertEqual(self.store.get_session("session-1")["mode"], "advise")

    def test_toggle_stops_before_model_and_changes_session_mode(self) -> None:
        output = run_hook(
            self.payload("UserPromptSubmit", prompt="$agent-efficiency off"),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        self.assertEqual(output["continue"], False)
        self.assertIn("off", output["stopReason"])
        self.assertEqual(self.store.get_session("session-1")["mode"], "off")

    def test_off_mode_records_no_tool_events(self) -> None:
        run_hook(
            self.payload("UserPromptSubmit", prompt="$agent-efficiency off"),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        run_hook(
            self.payload(
                "PostToolUse",
                tool_name="Bash",
                tool_input={"command": "pytest"},
                tool_response={"success": True},
            ),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        session = self.store.get_session("session-1")
        self.assertEqual(session["tool_count"], 0)

    def test_observe_records_without_guidance(self) -> None:
        run_hook(
            self.payload("UserPromptSubmit", prompt="$agent-efficiency observe"),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        output = run_hook(
            self.payload(
                "UserPromptSubmit",
                prompt="Implement a substantial API feature with acceptance tests and docs.",
            ),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        self.assertIsNone(output)
        self.assertEqual(self.store.get_session("session-1")["turn_count"], 1)

    def test_substantial_task_gets_one_compact_claude_nudge(self) -> None:
        output = run_hook(
            self.payload(
                "UserPromptSubmit",
                prompt=(
                    "Implement a complete authentication flow with tests, error "
                    "handling, documentation, and explicit rollback behavior."
                ),
            ),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        self.assertIn("hookSpecificOutput", output)
        self.assertIn(
            "success",
            output["hookSpecificOutput"]["additionalContext"].casefold(),
        )
        second = run_hook(
            self.payload(
                "UserPromptSubmit",
                prompt=(
                    "Now implement the next substantial authentication slice "
                    "with its behavior-level tests and documentation."
                ),
            ),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        self.assertIsNone(second)

    def test_codex_guidance_uses_supported_additional_context(self) -> None:
        payload = self.payload(
            "UserPromptSubmit",
            prompt=(
                "Review the exact implementation diff against the complete "
                "security specification and report concrete findings."
            ),
            model="gpt-5.6",
            turn_id="turn-1",
        )
        output = run_hook(
            payload,
            store=self.store,
            environ=CODEX_ENV,
        )
        self.assertIn("hookSpecificOutput", output)
        self.assertIn(
            "guidance:",
            output["hookSpecificOutput"]["additionalContext"].casefold(),
        )
        self.assertNotIn("systemMessage", output)

    def test_third_identical_batch_tool_call_gets_repeat_nudge(self) -> None:
        run_hook(
            self.payload(
                "UserPromptSubmit",
                prompt="Implement a substantial feature with tests and documentation.",
            ),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        outputs = []
        for _ in range(3):
            outputs.append(
                run_hook(
                    self.payload(
                        "PostToolBatch",
                        tool_calls=[
                            {
                                "tool_name": "Bash",
                                "tool_input": {"command": "rg -n 'needle' src"},
                                "tool_response": "match",
                            }
                        ],
                    ),
                    store=self.store,
                    environ=CLAUDE_ENV,
                )
            )
        self.assertIsNone(outputs[0])
        self.assertIsNone(outputs[1])
        self.assertIn("same action", repr(outputs[2]).casefold())
        self.assertEqual(
            self.store.get_session("session-1")["repeated_action_count"], 1
        )

    def test_second_matching_failure_gets_root_cause_nudge(self) -> None:
        run_hook(
            self.payload(
                "UserPromptSubmit",
                prompt="Diagnose why the full integration test fails and fix the root cause.",
            ),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        first = run_hook(
            self.payload(
                "PostToolUseFailure",
                tool_name="Bash",
                tool_input={"command": "pytest tests/integration"},
                error="failed",
            ),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        second = run_hook(
            self.payload(
                "PostToolUseFailure",
                tool_name="Bash",
                tool_input={"command": "pytest tests/integration"},
                error="failed",
            ),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        self.assertIsNone(first)
        self.assertIn("failed repeatedly", repr(second).casefold())

    def test_branch_creation_without_observed_fetch_gets_advice(self) -> None:
        run_hook(
            self.payload(
                "UserPromptSubmit",
                prompt="Create a branch and implement a focused change.",
            ),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        output = run_hook(
            self.payload(
                "PreToolUse",
                tool_name="Bash",
                tool_input={"command": "git switch -c feature/test"},
            ),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        self.assertIn("no fetch was observed", repr(output).casefold())
        self.assertNotIn(b"feature/test", Path(self.store.paths.database).read_bytes())

    def test_observed_fetch_suppresses_branch_creation_advice(self) -> None:
        run_hook(
            self.payload(
                "UserPromptSubmit",
                prompt="Fetch and then create a focused branch.",
            ),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        run_hook(
            self.payload(
                "PreToolUse",
                tool_name="Bash",
                tool_input={"command": "git fetch origin"},
            ),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        output = run_hook(
            self.payload(
                "PreToolUse",
                tool_name="Bash",
                tool_input={"command": "git switch -c feature/test"},
            ),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        self.assertIsNone(output)

    def test_pre_compaction_preserves_repeated_failure_state(self) -> None:
        run_hook(
            self.payload(
                "UserPromptSubmit",
                prompt="Diagnose the failing integration test.",
            ),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        for command in ("pytest tests/a", "pytest tests/b"):
            run_hook(
                self.payload(
                    "PostToolUseFailure",
                    tool_name="Bash",
                    tool_input={"command": command},
                ),
                store=self.store,
                environ=CLAUDE_ENV,
            )
        output = run_hook(
            self.payload("PreCompact"),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        self.assertIn("before compaction", repr(output).casefold())

    def test_host_capabilities_are_stored_without_raw_payload(self) -> None:
        marker = "private_capability_payload_8217"
        run_hook(
            self.payload(
                "UserPromptSubmit",
                prompt=marker,
                host_version="2.1.190",
            ),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM host_capabilities WHERE session_id = ?",
                ("session-1",),
            ).fetchone()
        self.assertEqual(row["host"], "claude")
        self.assertIn("add_context", row["capabilities"])
        self.assertNotIn(marker.encode(), self.store.paths.database.read_bytes())

    def test_code_edit_requests_validation_before_stop(self) -> None:
        run_hook(
            self.payload(
                "UserPromptSubmit",
                prompt="Implement a complete parser change with behavior-level acceptance criteria.",
            ),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        edit = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/work/project/parser.py", "content": "x = 1"},
            "tool_response": {"success": True},
        }
        output = run_hook(
            self.payload("PostToolUse", **edit),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        self.assertIn("validation", repr(output).casefold())
        self.assertIsNone(
            run_hook(
                self.payload("Stop", stop_hook_active=False),
                store=self.store,
                environ=CLAUDE_ENV,
            )
        )
        second = run_hook(
            self.payload("Stop", stop_hook_active=True),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        self.assertIsNone(second)

    def test_validation_prevents_stop_nudge(self) -> None:
        run_hook(
            self.payload(
                "UserPromptSubmit",
                prompt="Implement a complete parser change with behavior-level acceptance criteria.",
            ),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        edit = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/work/project/parser.py", "content": "x = 1"},
            "tool_response": {"success": True},
        }
        run_hook(
            self.payload("PostToolUse", **edit),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        run_hook(
            self.payload(
                "PostToolUse",
                tool_name="Bash",
                tool_input={"command": "pytest tests/test_parser.py"},
                tool_response={"success": True},
            ),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        output = run_hook(
            self.payload("Stop", stop_hook_active=False),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        self.assertIsNone(output)

    def test_prompt_bodies_are_not_persisted(self) -> None:
        private_marker = "private prompt body marker"
        run_hook(
            self.payload(
                "UserPromptSubmit",
                prompt=(
                    "Implement a substantial private feature "
                    f"{private_marker} with tests."
                ),
            ),
            store=self.store,
            environ=CLAUDE_ENV,
        )
        database_bytes = Path(self.store.paths.database).read_bytes()
        self.assertNotIn(private_marker.encode(), database_bytes)
        with closing(sqlite3.connect(self.store.paths.database)) as conn:
            prompt_chars = conn.execute(
                "SELECT prompt_chars FROM turns WHERE session_id = 'session-1'"
            ).fetchone()[0]
        self.assertGreater(prompt_chars, 0)


if __name__ == "__main__":
    unittest.main()
