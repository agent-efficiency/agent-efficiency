"""Coverage reports must not promote custom-tool launch events into evidence."""

import tempfile
import unittest

from agent_efficiency.pi_bridge import dispatch
from agent_efficiency.store import Store


class PiCoverageTests(unittest.TestCase):
    def test_background_launch_and_shell_success_do_not_prove_terminal_verification(
        self,
    ):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            base = {"session_id": "pi:coverage", "cwd": root, "turn_id": "t"}
            dispatch({**base, "hook_event_name": "SessionStart"}, store)
            dispatch({**base, "hook_event_name": "UserPromptSubmit"}, store)
            for index, name in enumerate(("bg_run", "bash")):
                fields = {
                    **base,
                    "tool_name": name,
                    "tool_use_id": str(index),
                    "tool_input": {
                        "command": "python -m unittest PRIVATE_COVERAGE_CANARY"
                    },
                }
                dispatch({**fields, "hook_event_name": "PreToolUse"}, store)
                result = dispatch({**fields, "hook_event_name": "PostToolUse"}, store)
            coverage = result["coverage"]
            self.assertEqual(coverage["tool_call_outcomes"], "supported")
            self.assertIsNone(coverage["external_background_terminal_results"])
            self.assertIsNone(coverage["verified_acceptance"])
            self.assertEqual(coverage["verification_receipts"], "explicit_cli_only")
            self.assertEqual(result["delivered_interventions"], 0)
            self.assertEqual(result["tools"], 2)
            with store.connect() as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM verification_receipts"
                    ).fetchone()[0],
                    0,
                )
                self.assertNotIn("PRIVATE_COVERAGE_CANARY", "\n".join(conn.iterdump()))
