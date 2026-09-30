from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from agent_efficiency.cli import main
from agent_efficiency.contracts.effects import CanonicalEffect
from agent_efficiency.hook import run_hook
from agent_efficiency.interventions import render_intervention
from agent_efficiency.report import build_explanation, build_report
from agent_efficiency.store import Store


class InterventionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.store.ensure_session(
            "session-1", host="codex", cwd="/work/project", model="gpt-5"
        )
        self.store.start_turn(
            "session-1",
            "turn-1",
            prompt_chars=0,
            task_type="unknown",
            risk="normal",
        )

    def test_rendered_effect_has_a_privacy_safe_disposition(self) -> None:
        marker = "private_guidance_marker_4821"
        output = render_intervention(
            self.store,
            "session-1",
            "turn-1",
            "codex",
            "PostToolUse",
            CanonicalEffect(
                effect="add_context",
                policy_id="core.test",
                policy_revision="2",
                observed_fact="controlled_fact",
                agent_message=marker,
                required_capability="add_context",
            ),
        )
        self.assertIn("hookSpecificOutput", output)
        item = self.store.latest_intervention("session-1")
        self.assertEqual(item["disposition"], "emitted")
        self.assertEqual(item["message_chars"], len(marker))
        self.assertNotIn(marker.encode(), Path(self.store.paths.database).read_bytes())
        unsupported = render_intervention(
            self.store,
            "session-1",
            "turn-1",
            "cursor",
            "beforeSubmitPrompt",
            CanonicalEffect(
                effect="add_context",
                policy_id="core.unsupported-test",
                policy_revision="1",
                observed_fact="controlled_fact",
                agent_message="Use context.",
            ),
        )
        self.assertIsNone(unsupported)
        self.assertEqual(
            self.store.latest_intervention("session-1")["disposition"],
            "unsupported",
        )

    def test_observed_results_use_a_bounded_event_window(self) -> None:
        self.store.record_intervention(
            "session-1",
            "turn-1",
            policy_id="core.test",
            policy_revision="1",
            triggering_fact="controlled_fact",
            requested_effect="add_context",
            rendered_effect="add_context",
            host_capability="add_context",
            disposition="emitted",
            message_chars=10,
        )
        self.store.ensure_session(
            "session-2", host="claude", cwd="/work/other", model="test"
        )
        self.store.start_turn(
            "session-2",
            "turn-1",
            prompt_chars=0,
            task_type="unknown",
            risk="normal",
        )
        for _ in range(20):
            self.store.record_event(
                "session-2", "turn-1", event_name="PostToolUse", outcome="success"
            )
        self.assertIsNotNone(
            self.store.record_intervention_outcome(
                "session-1", "turn-1", "check_started"
            )
        )
        for _ in range(9):
            self.store.record_event(
                "session-1", "turn-1", event_name="PostToolUse", outcome="success"
            )
        self.assertIsNone(
            self.store.record_intervention_outcome(
                "session-1", "turn-1", "check_passed"
            )
        )

    def test_cursor_verification_is_associated_with_the_last_intervention(self) -> None:
        payload = {
            "conversation_id": "cursor-1",
            "generation_id": "turn-1",
            "cursor_version": "1.7.2",
            "workspace_roots": ["/work/project"],
        }
        run_hook(
            {
                **payload,
                "hook_event_name": "beforeSubmitPrompt",
                "prompt": "Make a small code change.",
            },
            store=self.store,
        )
        run_hook(
            {
                **payload,
                "hook_event_name": "afterFileEdit",
                "file_path": "/work/project/app.py",
            },
            store=self.store,
        )
        run_hook(
            {
                **payload,
                "hook_event_name": "beforeShellExecution",
                "command": "python -m pytest",
            },
            store=self.store,
        )
        run_hook(
            {
                **payload,
                "hook_event_name": "afterShellExecution",
                "command": "python -m pytest",
                "output": "private output",
            },
            store=self.store,
        )
        item = self.store.latest_intervention("cursor-1")
        with self.store.connect() as conn:
            runtime_samples = conn.execute(
                "SELECT COUNT(*) AS count FROM runtime_health WHERE session_id = ?",
                ("cursor-1",),
            ).fetchone()
        self.assertEqual(runtime_samples["count"], 4)
        outcomes = {row["outcome"] for row in item["outcomes"]}
        self.assertIn("check_started", outcomes)
        self.assertIn("check_passed", outcomes)
        self.assertIn("suggested_action_observed", outcomes)

    def test_explain_last_cli_supports_json_and_rating(self) -> None:
        render_intervention(
            self.store,
            "session-1",
            "turn-1",
            "codex",
            "PostToolUse",
            CanonicalEffect(
                effect="add_context",
                policy_id="core.test",
                policy_revision="1",
                observed_fact="controlled_fact",
                agent_message="Run a focused check.",
            ),
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                [
                    "--data-dir",
                    self.temp.name,
                    "explain",
                    "last",
                    "--session",
                    "session-1",
                    "--rate",
                    "useful",
                    "--json",
                ]
            )
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["intervention"]["feedback"]["judgment"], "useful")
        self.assertNotIn("Run a focused check", output.getvalue())

    def explain(self, *arguments: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["--data-dir", self.temp.name, "explain", "last", *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_explain_last_with_nothing_recorded_is_not_an_error(self) -> None:
        code, output, _ = self.explain()
        self.assertEqual(code, 0)
        self.assertIn("no recorded intervention", output)
        code, output, _ = self.explain("--json")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output)["found"])

    def test_explain_last_still_fails_for_real_errors(self) -> None:
        code, output, _ = self.explain("--rate", "useful")
        self.assertEqual(code, 1)
        self.assertIn("no intervention is available", output)
        code, output, _ = self.explain("--session", "no-such-session")
        self.assertEqual(code, 1)
        self.assertIn("no-such-session", output)

    def test_reports_explain_association_and_immutable_utility(self) -> None:
        render_intervention(
            self.store,
            "session-1",
            "turn-1",
            "codex",
            "PostToolUse",
            CanonicalEffect(
                effect="add_context",
                policy_id="core.test",
                policy_revision="1",
                observed_fact="controlled_fact",
                agent_message="Run a focused check.",
            ),
        )
        item = self.store.latest_intervention("session-1")
        self.store.record_intervention_feedback(item["id"], "useful")
        same = self.store.record_intervention_feedback(item["id"], "useful")
        self.assertEqual(same["judgment"], "useful")
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.store.record_intervention_feedback(item["id"], "false_or_unnecessary")
        report = build_report(self.store, session_id="session-1")
        self.assertEqual(report["scope"], "session")
        self.assertEqual(report["capability_layer"]["scope"], "session")
        self.assertEqual(report["runtime_health"]["scope"], "session")
        self.assertIn(
            "do not establish causation", report["interventions"]["claim_boundary"]
        )
        explanation = build_explanation(
            self.store, session_id="session-1", cwd="/work/project"
        )
        self.assertEqual(explanation["intervention"]["policy_id"], "core.test")
        self.assertEqual(explanation["evidence"][0]["state"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
