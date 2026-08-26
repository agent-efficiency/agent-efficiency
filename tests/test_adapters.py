from __future__ import annotations

import unittest

from agent_efficiency.adapters import adapter_for
from agent_efficiency.contracts.effects import CanonicalEffect


class AdapterContractTests(unittest.TestCase):
    def effect(self, name: str = "add_context") -> CanonicalEffect:
        return CanonicalEffect(
            effect=name,
            policy_id="test.policy",
            policy_revision="1",
            observed_fact="test fact",
            agent_message="Run the configured check.",
            measurement_label="test",
        )

    def test_each_host_normalizes_prompt_to_the_same_event(self) -> None:
        fixtures = (
            (
                "claude",
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "claude-session",
                    "prompt_id": "turn-1",
                    "cwd": "/work/project",
                    "prompt": "private claude prompt",
                },
            ),
            (
                "cursor",
                {
                    "hook_event_name": "beforeSubmitPrompt",
                    "conversation_id": "cursor-session",
                    "generation_id": "turn-1",
                    "workspace_roots": ["/work/project"],
                    "prompt": "private cursor prompt",
                },
            ),
            (
                "codex",
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "codex-session",
                    "turn_id": "turn-1",
                    "cwd": "/work/project",
                    "prompt": "private codex prompt",
                },
            ),
        )
        events = [adapter_for(host).to_event(payload) for host, payload in fixtures]
        self.assertEqual(
            {event.name for event in events if event}, {"turn.prompted"}
        )
        self.assertEqual(len({event.project_id for event in events if event}), 1)
        for event in events:
            self.assertIsNotNone(event)
            self.assertNotIn("prompt", event.facts)
            self.assertNotIn("private", repr(event).casefold())

    def test_cursor_maps_native_file_and_shell_events(self) -> None:
        adapter = adapter_for("cursor")
        base = {
            "conversation_id": "cursor-session",
            "workspace_roots": ["/work/project"],
        }
        changed = adapter.to_event({**base, "hook_event_name": "afterFileEdit"})
        shell = adapter.to_event(
            {**base, "hook_event_name": "afterShellExecution", "duration": 12}
        )
        self.assertEqual(changed.name, "file.changed")
        self.assertEqual(shell.name, "action.completed")
        self.assertEqual(shell.facts["duration_ms"], 12)

    def test_add_context_uses_each_native_output_contract(self) -> None:
        claude = adapter_for("claude").render(
            self.effect(), native_event="UserPromptSubmit"
        )
        cursor = adapter_for("cursor").render(
            self.effect(), native_event="postToolUse"
        )
        codex = adapter_for("codex").render(
            self.effect(), native_event="UserPromptSubmit"
        )
        self.assertIn("additionalContext", claude["hookSpecificOutput"])
        self.assertIn("additional_context", cursor)
        self.assertIn("additionalContext", codex["hookSpecificOutput"])

    def test_continue_turn_uses_each_native_output_contract(self) -> None:
        claude = adapter_for("claude").render(
            self.effect("continue_turn"), native_event="Stop"
        )
        cursor = adapter_for("cursor").render(
            self.effect("continue_turn"), native_event="stop"
        )
        codex = adapter_for("codex").render(
            self.effect("continue_turn"), native_event="Stop"
        )
        self.assertEqual(claude["decision"], "block")
        self.assertIn("followup_message", cursor)
        self.assertEqual(codex["decision"], "block")

    def test_custom_tool_name_is_reduced_to_a_fixed_class(self) -> None:
        event = adapter_for("codex").to_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "session-1",
                "cwd": "/work/project",
                "tool_name": "private_customer_tool_8472",
            }
        )
        self.assertEqual(event.facts["tool_class"], "other")
        self.assertNotIn("private_customer", repr(event))

    def test_canonical_event_drops_raw_action_data(self) -> None:
        event = adapter_for("codex").to_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "session-1",
                "cwd": "/work/project",
                "tool_name": "Shell",
                "tool_input": {"command": "private command"},
            }
        )
        self.assertEqual(event.facts["tool_class"], "shell")
        self.assertNotIn("command", repr(event).casefold())


if __name__ == "__main__":
    unittest.main()
