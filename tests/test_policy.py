from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from agent_efficiency.policy import PolicyPack, classify_task, classify_tool


class TaskClassificationTests(unittest.TestCase):
    def test_substantial_build_and_high_risk(self) -> None:
        facts = classify_task(
            "Implement production authentication with tests, rollback, and "
            "a migration for existing customer data."
        )
        self.assertEqual(facts.task_type, "migration")
        self.assertEqual(facts.risk, "high")
        self.assertTrue(facts.substantial)

    def test_trivial_typo_does_not_trigger_substantial_guidance(self) -> None:
        facts = classify_task("Fix the typo in one line.")
        self.assertEqual(facts.task_type, "fix")
        self.assertFalse(facts.substantial)

    def test_review_is_classified_before_generic_build_language(self) -> None:
        facts = classify_task(
            "Review the diff and assess whether it implements the spec."
        )
        self.assertEqual(facts.task_type, "review")

    def test_strong_reviewed_knowledge_match_refines_core_guidance(self) -> None:
        expires_at = (
            (datetime.now(UTC) + timedelta(days=30)).isoformat().replace("+00:00", "Z")
        )
        pack = PolicyPack(
            [
                {
                    "id": "core.work-packet",
                    "kind": "task_guidance",
                    "message": "Generic build guidance.",
                    "status": "reviewed",
                    "expires_at": expires_at,
                },
                {
                    "id": "knowledge.vertical-slice",
                    "kind": "knowledge_guidance",
                    "task_types": ["build", "design"],
                    "trigger_keywords": ["architecture", "scaffold"],
                    "message": "Prove a vertical slice first.",
                    "status": "reviewed",
                    "expires_at": expires_at,
                },
            ]
        )
        prompt = (
            "Build the new architecture and scaffold the full system with "
            "acceptance checks before expanding."
        )
        guidance = pack.task_guidance(classify_task(prompt), prompt)
        self.assertIsNotNone(guidance)
        assert guidance is not None
        self.assertEqual(guidance.policy_id, "knowledge.vertical-slice")


class ToolClassificationTests(unittest.TestCase):
    def test_raw_input_is_reduced_to_safe_metadata(self) -> None:
        private_marker = "private source body marker"
        facts = classify_tool(
            "Write",
            {"file_path": "/repo/src/app.py", "content": private_marker},
        )
        self.assertTrue(facts.is_edit)
        self.assertTrue(facts.is_code_edit)
        self.assertEqual(facts.file_extensions, (".py",))
        self.assertNotIn(private_marker, repr(facts.safe_details))
        self.assertNotIn(private_marker, facts.signature)

    def test_test_command_is_validation(self) -> None:
        facts = classify_tool("Bash", {"command": "python -m pytest tests/test_api.py"})
        self.assertEqual(facts.command_class, "test")
        self.assertTrue(facts.is_validation)

    def test_root_filesystem_scan_is_broad(self) -> None:
        facts = classify_tool("Bash", {"command": "find / -name '*.py'"})
        self.assertTrue(facts.is_broad_scan)

    def test_repository_search_is_not_broad(self) -> None:
        facts = classify_tool("Bash", {"command": "rg -n 'handler' src tests"})
        self.assertFalse(facts.is_broad_scan)

    def test_markdown_only_patch_is_not_code_edit(self) -> None:
        facts = classify_tool(
            "apply_patch",
            {"patch": "*** Update File: README.md\n@@\n-old\n+new\n"},
        )
        self.assertTrue(facts.is_edit)
        self.assertFalse(facts.is_code_edit)


if __name__ == "__main__":
    unittest.main()
