from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from agent_efficiency.capability_pack import (
    capability_pack_digest,
    load_bundled_capability_pack,
)
from agent_efficiency.capability_retrieval import (
    MAX_ENVELOPE_CHARS,
    capability_status,
    ensure_session_capability_pack,
    explain_capability,
    select_capability,
)
from agent_efficiency.cli import main
from agent_efficiency.hook import run_hook
from agent_efficiency.models import TaskFacts
from agent_efficiency.policy import classify_task
from agent_efficiency.store import Store


CLAUDE_ENV = {"CLAUDE_PLUGIN_ROOT": "/plugin"}


class CapabilityRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.store.ensure_session(
            "session-1",
            host="claude",
            cwd="/work/project",
            model="claude-sonnet",
        )
        self.pack = load_bundled_capability_pack()

    def test_compiled_golden_vectors_select_the_expected_card(self) -> None:
        for vector in self.pack["retrieval_tests"]:
            prompt = " ".join(vector["signals"])
            facts = TaskFacts(
                str(vector["task"]),
                "normal",
                True,
                len(prompt),
            )
            decision = select_capability(
                self.pack,
                prompt,
                facts,
                host="claude",
                event_name=str(vector["event"]),
            )
            self.assertIsNotNone(decision, vector["id"])
            assert decision is not None
            self.assertIsNone(decision.suppression_reason, vector["id"])
            self.assertEqual([decision.card_id], vector["expected_card_ids"])

    def test_irrelevant_and_negative_prompts_stay_silent(self) -> None:
        irrelevant = "Please tell me hello."
        self.assertIsNone(
            select_capability(
                self.pack,
                irrelevant,
                classify_task(irrelevant),
                host="claude",
                event_name="UserPromptSubmit",
            )
        )
        negative = "Build this documentation only update with no code changes."
        decision = select_capability(
            self.pack,
            negative,
            classify_task(negative),
            host="claude",
            event_name="UserPromptSubmit",
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.suppression_reason, "negative-trigger")
        self.assertIsNone(decision.envelope)

    def test_non_substantial_build_is_suppressed(self) -> None:
        prompt = "Build it."
        decision = select_capability(
            self.pack,
            prompt,
            classify_task(prompt),
            host="claude",
            event_name="UserPromptSubmit",
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.suppression_reason, "not-substantial")

    def test_embedded_constitutional_card_expiry_is_soft_revalidation(self) -> None:
        prompt = "Review the exact diff against the specification."
        decision = select_capability(
            self.pack,
            prompt,
            classify_task(prompt),
            host="claude",
            event_name="UserPromptSubmit",
            now=datetime(2030, 1, 1, tzinfo=UTC),
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertIsNone(decision.suppression_reason)
        self.assertIsNotNone(decision.envelope)

    def test_envelope_is_advisory_grounded_and_bounded(self) -> None:
        prompt = (
            "Implement a substantial authentication feature with tests, "
            "rollback, and observable acceptance behavior."
        )
        decision = select_capability(
            self.pack,
            prompt,
            classify_task(prompt),
            host="claude",
            event_name="UserPromptSubmit",
        )
        self.assertIsNotNone(decision)
        assert decision is not None and decision.envelope is not None
        self.assertLessEqual(len(decision.envelope), MAX_ENVELOPE_CHARS)
        self.assertIn("Agent Efficiency guidance: AE.", decision.envelope)
        self.assertIn("Why selected:", decision.envelope)
        self.assertIn("rules remain authoritative", decision.envelope)

    def test_session_pin_loads_retained_bytes_without_current_pack(self) -> None:
        first_pack, first_pin = ensure_session_capability_pack(self.store, "session-1")
        retained = list(self.store.paths.capability_packs.glob("*.json"))
        self.assertEqual(len(retained), 1)
        with patch(
            "agent_efficiency.capability_retrieval.load_bundled_capability_pack",
            side_effect=AssertionError("current pack must not replace a session pin"),
        ):
            second_pack, second_pin = ensure_session_capability_pack(
                self.store, "session-1"
            )
        self.assertEqual(first_pin, second_pin)
        self.assertEqual(
            capability_pack_digest(first_pack),
            capability_pack_digest(second_pack),
        )

    def test_corrupt_retained_base_pack_recovers_from_embedded_bytes(self) -> None:
        first_pack, first_pin = ensure_session_capability_pack(self.store, "session-1")
        retained = next(self.store.paths.capability_packs.glob("*.json"))
        retained.write_text("{}\n", encoding="utf-8")
        recovered, recovered_pin = ensure_session_capability_pack(
            self.store, "session-1"
        )
        self.assertEqual(first_pin, recovered_pin)
        self.assertEqual(
            capability_pack_digest(first_pack),
            capability_pack_digest(recovered),
        )

    def test_hook_pins_at_start_without_context_then_records_selection(self) -> None:
        output = run_hook(
            {
                "session_id": "hook-session",
                "hook_event_name": "SessionStart",
                "cwd": "/work/project",
            },
            store=self.store,
            environ=CLAUDE_ENV,
        )
        self.assertIsNone(output)
        self.assertIsNotNone(self.store.get_session_capability_pack("hook-session"))
        prompt_output = run_hook(
            {
                "session_id": "hook-session",
                "hook_event_name": "UserPromptSubmit",
                "prompt_id": "turn-1",
                "cwd": "/work/project",
                "prompt": (
                    "Review the exact diff against the security specification "
                    "and report evidence-backed findings."
                ),
            },
            store=self.store,
            environ=CLAUDE_ENV,
        )
        self.assertIsNotNone(prompt_output)
        assert prompt_output is not None
        envelope = prompt_output["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(envelope), MAX_ENVELOPE_CHARS)
        summary = self.store.knowledge_receipt_summary("hook-session")
        self.assertEqual(summary["emitted"], 1)
        self.assertEqual(summary["emitted_chars"], len(envelope))
        duplicate = run_hook(
            {
                "session_id": "hook-session",
                "hook_event_name": "UserPromptSubmit",
                "prompt_id": "turn-2",
                "cwd": "/work/project",
                "prompt": (
                    "Review the next exact diff against the governing "
                    "specification and report concrete findings."
                ),
            },
            store=self.store,
            environ=CLAUDE_ENV,
        )
        self.assertIsNone(duplicate)
        summary = self.store.knowledge_receipt_summary("hook-session")
        self.assertEqual(summary["emitted"], 1)
        self.assertEqual(summary["suppression_reasons"], {"dedupe": 1})

    def test_runtime_retrieval_never_uses_network_or_subprocess(self) -> None:
        payload = {
            "session_id": "offline-session",
            "hook_event_name": "UserPromptSubmit",
            "prompt_id": "turn-1",
            "cwd": "/work/project",
            "prompt": (
                "Implement a substantial local feature with observable "
                "acceptance checks, a rollback boundary, explicit failure "
                "behavior, integration coverage, and release evidence."
            ),
        }
        with (
            patch("socket.socket", side_effect=AssertionError("network forbidden")),
            patch(
                "subprocess.run",
                side_effect=AssertionError("runtime subprocess forbidden"),
            ),
        ):
            output = run_hook(
                payload,
                store=self.store,
                environ=CLAUDE_ENV,
            )
        self.assertIsNotNone(output)

    def test_local_reviewed_guidance_remains_a_fallback(self) -> None:
        self.store.paths.active_pack.write_text(
            json.dumps(
                {
                    "policies": [
                        {
                            "id": "knowledge.local-profile",
                            "kind": "knowledge_guidance",
                            "task_types": ["any"],
                            "trigger_keywords": ["latency", "profiling"],
                            "message": "Measure the narrow local path first.",
                            "status": "reviewed",
                            "expires_at": "2099-01-01T00:00:00Z",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        prompt = (
            "Use latency profiling to understand this narrow local behavior "
            "before changing anything. Keep the measurement bounded and "
            "repeatable across the same controlled fixture."
        )
        output = run_hook(
            {
                "session_id": "local-session",
                "hook_event_name": "UserPromptSubmit",
                "prompt_id": "turn-1",
                "cwd": "/work/project",
                "prompt": prompt,
            },
            store=self.store,
            environ=CLAUDE_ENV,
        )
        self.assertIsNotNone(output)
        self.assertIn("narrow local path", repr(output))

    def test_knowledge_budgets_are_enforced_atomically(self) -> None:
        digest = "sha256:" + "0" * 64
        for index in range(5):
            turn_key = f"turn-{index}"
            self.store.start_turn(
                "session-1",
                turn_key,
                prompt_chars=200,
                task_type="build",
                risk="normal",
            )
            emitted, reason = self.store.record_knowledge_advisory(
                "session-1",
                turn_key,
                event_name="UserPromptSubmit",
                pack_id="test-pack",
                pack_digest=digest,
                card_id=f"card-{index}",
                score=10,
                match_reason="controlled test signal",
                emitted_chars=500,
            )
            if index < 4:
                self.assertTrue(emitted)
                self.assertIsNone(reason)
            else:
                self.assertFalse(emitted)
                self.assertEqual(reason, "knowledge-envelope-budget")
        summary = self.store.knowledge_receipt_summary("session-1")
        self.assertEqual(summary["emitted"], 4)
        self.assertEqual(summary["emitted_chars"], 2000)
        self.assertEqual(summary["suppressed"], 1)

    def test_knowledge_and_operational_nudges_share_the_turn_budget(self) -> None:
        self.store.start_turn(
            "session-1",
            "combined-turn",
            prompt_chars=200,
            task_type="build",
            risk="normal",
        )
        self.assertTrue(
            self.store.record_nudge("session-1", "combined-turn", "core.first")
        )
        self.assertTrue(
            self.store.record_nudge("session-1", "combined-turn", "core.second")
        )
        emitted, reason = self.store.record_knowledge_advisory(
            "session-1",
            "combined-turn",
            event_name="UserPromptSubmit",
            pack_id="test-pack",
            pack_digest="sha256:" + "0" * 64,
            card_id="cap.third",
            score=10,
            match_reason="controlled test signal",
            emitted_chars=200,
        )
        self.assertTrue(emitted)
        self.assertIsNone(reason)
        self.assertFalse(
            self.store.record_nudge("session-1", "combined-turn", "core.fourth")
        )

    def test_status_and_explain_are_offline_and_receipt_backed(self) -> None:
        ensure_session_capability_pack(self.store, "session-1")
        status = capability_status(self.store, "session-1")
        self.assertEqual(status["channel_state"], "bundled")
        self.assertEqual(status["session_pin"]["pack_id"], status["pack_id"])
        explanation = explain_capability(
            self.store,
            "core.work-packet",
            session_id="session-1",
        )
        self.assertEqual(explanation["authority"], "advisory")
        self.assertTrue(explanation["principles"])
        self.assertEqual(
            explanation["publisher"],
            ["agent-efficiency maintainers"],
        )

    def test_existing_schema_migrates_without_losing_sessions(self) -> None:
        database = Path(self.temp.name) / "agent-efficiency.db"
        with contextlib.closing(sqlite3.connect(database)) as conn:
            conn.execute("DROP TABLE knowledge_receipts")
            conn.execute("DROP TABLE session_capability_packs")
            conn.execute("UPDATE settings SET value = '1' WHERE key = 'schema_version'")
            conn.commit()
        migrated = Store(self.temp.name)
        self.assertIsNotNone(migrated.get_session("session-1"))
        self.assertIsNone(migrated.get_session_capability_pack("session-1"))
        with migrated.connect() as conn:
            version = conn.execute(
                "SELECT value FROM settings WHERE key = 'schema_version'"
            ).fetchone()[0]
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertEqual(version, "6")
        self.assertIn("knowledge_receipts", tables)
        self.assertIn("session_capability_packs", tables)
        self.assertIn("interventions", tables)
        self.assertIn("intervention_outcomes", tables)
        self.assertIn("runtime_health", tables)

    def test_cli_status_and_explain_support_json(self) -> None:
        ensure_session_capability_pack(self.store, "session-1")
        with contextlib.redirect_stdout(io.StringIO()) as status_output:
            self.assertEqual(
                main(
                    [
                        "--data-dir",
                        self.temp.name,
                        "knowledge",
                        "status",
                        "--session",
                        "session-1",
                        "--json",
                    ]
                ),
                0,
            )
        self.assertEqual(
            json.loads(status_output.getvalue())["pack_id"],
            "agent-efficiency-guidance",
        )
        with contextlib.redirect_stdout(io.StringIO()) as explain_output:
            self.assertEqual(
                main(
                    [
                        "--data-dir",
                        self.temp.name,
                        "knowledge",
                        "explain",
                        "core.work-packet",
                        "--session",
                        "session-1",
                        "--json",
                    ]
                ),
                0,
            )
        self.assertEqual(
            json.loads(explain_output.getvalue())["card_id"],
            "core.work-packet",
        )


if __name__ == "__main__":
    unittest.main()
