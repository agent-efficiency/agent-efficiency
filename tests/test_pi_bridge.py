from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_efficiency.pi_bridge import dispatch
from agent_efficiency.store import Store


class PiBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.base = {"session_id": "pi:test", "cwd": self.temp.name}

    def send(self, event, **extra):
        return dispatch({**self.base, "hook_event_name": event, **extra}, self.store)

    def test_observe_default_and_host(self):
        self.send("SessionStart")
        session = self.store.get_session("pi:test")
        self.assertEqual(session["host"], "pi")
        self.assertEqual(session["mode"], "observe")

    def test_tool_counts_and_privacy(self):
        self.send("SessionStart")
        self.send("UserPromptSubmit", turn_id="t")
        fields = dict(
            turn_id="t",
            tool_name="bash",
            tool_use_id="a",
            tool_input={"command": "echo SECRET_SENTINEL"},
        )
        self.send("PreToolUse", **fields)
        self.send("PostToolUseFailure", **fields)
        turn = self.store.get_turn("pi:test", "t")
        self.assertEqual(turn["tool_count"], 1)
        self.assertEqual(turn["failure_count"], 1)
        with self.store.connect() as conn:
            dump = "\n".join(conn.iterdump())
        self.assertNotIn("SECRET_SENTINEL", dump)

    def test_off_records_no_tool(self):
        self.send("SessionStart")
        dispatch({**self.base, "action": "off"}, self.store)
        self.send("PreToolUse", turn_id="t", tool_name="read")
        self.assertIsNone(self.store.get_turn("pi:test", "t"))

    def test_guard_and_unknown_events_rejected(self):
        for request in ({"action": "guard"}, {"hook_event_name": "invented"}):
            with self.assertRaises(ValueError):
                dispatch({**self.base, **request}, self.store)

    def test_project_guard_does_not_enable_guard(self):
        Path(self.temp.name, "agent-efficiency.toml").write_text('mode = "guard"\n')
        self.send("SessionStart")
        self.send("Stop")
        self.assertEqual(self.store.get_session("pi:test")["mode"], "observe")

    def test_repeat_action_preview_is_not_an_intervention(self):
        self.send("SessionStart")
        self.send("UserPromptSubmit", turn_id="t")
        result = None
        for index in range(3):
            fields = dict(
                turn_id="t",
                tool_name="bash",
                tool_use_id=str(index),
                tool_input={"command": "echo SECRET_PREVIEW"},
            )
            self.send("PreToolUse", **fields)
            result = self.send("PostToolUse", **fields)
        self.assertGreaterEqual(result["preview_count"], 1)
        preview = result["previews"][0]
        self.assertEqual(preview["policy_id"], "core.repeat-action")
        self.assertEqual(preview["reason"], "repeated_action")
        self.assertTrue(preview["message"])
        self.assertEqual(self.store.get_session("pi:test")["mode"], "observe")
        with self.store.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM interventions").fetchone()[0], 0
            )
            self.assertNotIn("SECRET_PREVIEW", "\n".join(conn.iterdump()))

    def test_edit_without_validation_previews_advice_not_guard(self):
        self.send("SessionStart")
        self.send("UserPromptSubmit", turn_id="t")
        fields = dict(
            turn_id="t",
            tool_name="edit",
            tool_use_id="a",
            tool_input={"path": "example.py"},
        )
        self.send("PreToolUse", **fields)
        result = self.send("PostToolUse", **fields)
        self.assertEqual(result["previews"][0]["policy_id"], "core.verify-change")
        self.assertEqual(result["previews"][0]["effect"], "would_advise")
        result = self.send("Stop", turn_id="t")
        self.assertEqual(result["preview_count"], 1)

    def test_failure_loop_preview_and_distinct_action_negative_control(self):
        self.send("SessionStart")
        self.send("UserPromptSubmit", turn_id="t")
        for index in range(3):
            fields = dict(
                turn_id="t",
                tool_name="read",
                tool_use_id=str(index),
                tool_input={"path": f"file-{index}.py"},
            )
            self.send("PreToolUse", **fields)
            result = self.send("PostToolUse", **fields)
        self.assertEqual(result["preview_count"], 0)
        for index in range(3, 6):
            fields = dict(
                turn_id="t",
                tool_name="bash",
                tool_use_id=str(index),
                tool_input={"command": "python missing.py"},
            )
            self.send("PreToolUse", **fields)
            result = self.send("PostToolUseFailure", **fields)
        self.assertIn(
            "core.break-failure-loop", {p["policy_id"] for p in result["previews"]}
        )

    def test_resume_preserves_off(self):
        self.send("SessionStart")
        dispatch({**self.base, "action": "off"}, self.store)
        self.send("SessionStart")
        self.assertEqual(self.store.get_session("pi:test")["mode"], "off")
