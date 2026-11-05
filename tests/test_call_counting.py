from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from itertools import permutations
from pathlib import Path

from agent_efficiency.hook import run_hook
from agent_efficiency.store import SCHEMA_VERSION, Store


CLAUDE_ENV = {"CLAUDE_PLUGIN_ROOT": "/plugin"}


def _event_columns(database: Path) -> set[str]:
    with closing(sqlite3.connect(database)) as conn:
        return {str(row[1]) for row in conn.execute("PRAGMA table_info(events)")}


class SchemaEightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "agent-efficiency.db"

    def test_new_store_is_schema_eight_with_resolves_column(self) -> None:
        store = Store(self.temp.name)
        self.assertEqual(SCHEMA_VERSION, "8")
        self.assertEqual(store.get_setting("schema_version"), "8")
        self.assertIn("resolves", _event_columns(self.database))

    def test_schema_seven_store_migrates_and_keeps_events(self) -> None:
        store = Store(self.temp.name)
        store.ensure_session("s", host="claude", cwd="/work/p", model=None)
        store.start_turn("s", "t", prompt_chars=0, task_type="unknown", risk="normal")
        store.record_event(
            "s",
            "t",
            event_name="PreToolUse",
            tool_name="Bash",
            signature="sig",
            outcome="pending",
        )
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute("ALTER TABLE events DROP COLUMN resolves")
            conn.execute("UPDATE settings SET value = '7' WHERE key = 'schema_version'")
            conn.commit()
        migrated = Store(self.temp.name)
        migrated.ensure_current_schema()
        self.assertEqual(migrated.get_setting("schema_version"), "8")
        self.assertIn("resolves", _event_columns(self.database))
        with closing(sqlite3.connect(self.database)) as conn:
            rows = conn.execute("SELECT resolves FROM events").fetchall()
        self.assertEqual(rows, [(0,)])


class StoreResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.store.ensure_session("s", host="claude", cwd="/work/p", model=None)
        self.store.start_turn(
            "s", "t", prompt_chars=0, task_type="unknown", risk="normal"
        )

    def start(self, signature: str = "sig") -> None:
        self.store.record_event(
            "s",
            "t",
            event_name="PreToolUse",
            tool_name="Bash",
            command_class="search",
            signature=signature,
            outcome="pending",
            count_tool=True,
        )

    def report(
        self, event_name: str, *, signature: str = "sig", failed: bool = False
    ) -> dict[str, int]:
        return self.store.record_tool_event(
            "s",
            "t",
            event_name=event_name,
            tool_name="Bash",
            command_class="search",
            signature=signature,
            outcome="failure" if failed else "success",
            duration_ms=None,
            safe_details={},
            count_failure=failed,
            count_edit=False,
            count_code_edit=False,
            count_validation=False,
            count_subagent=False,
            count_broad_scan=False,
        )

    def test_single_result_resolves_and_batch_duplicate_does_not(self) -> None:
        self.start()
        single = self.report("PostToolUse")
        batch = self.report("PostToolBatch")
        self.assertEqual(single["resolved"], 1)
        self.assertEqual(batch["resolved"], 0)
        self.assertEqual(batch["identical"], 1)
        self.assertEqual(self.store.get_session("s")["tool_count"], 1)

    def test_batch_first_then_single_result_counts_once(self) -> None:
        self.start()
        batch = self.report("PostToolBatch")
        single = self.report("PostToolUse")
        self.assertEqual(batch["resolved"], 1)
        self.assertEqual(single["resolved"], 0)
        self.assertEqual(self.store.get_session("s")["tool_count"], 1)

    def test_batch_only_host_counts_each_call_as_new(self) -> None:
        for _ in range(3):
            counts = self.report("PostToolBatch")
        self.assertEqual(counts["resolved"], 1)
        self.assertEqual(counts["identical"], 3)
        self.assertEqual(self.store.get_session("s")["tool_count"], 3)

    def test_failure_reported_twice_counts_once(self) -> None:
        self.start()
        self.report("PostToolUseFailure", failed=True)
        counts = self.report("PostToolBatch", failed=True)
        self.assertEqual(counts["failures"], 1)
        self.assertEqual(self.store.get_session("s")["failure_count"], 1)

    def test_runtime_counts_use_resolved_calls(self) -> None:
        for _ in range(2):
            self.start()
            self.report("PostToolUse")
            self.report("PostToolBatch")
        counts = self.store.runtime_counts(
            "s", "t", signature="sig", command_class="search"
        )
        self.assertEqual(counts["identical"], 2)
        self.assertEqual(counts["failures"], 0)
        self.assertEqual(counts["class_count"], 2)

    def test_event_count_can_filter_resolved_reports(self) -> None:
        self.start()
        self.report("PostToolUseFailure", failed=True)
        self.report("PostToolBatch", failed=True)
        self.assertEqual(
            self.store.event_count("s", "t", signature="sig", outcome="failure"), 2
        )
        self.assertEqual(
            self.store.event_count(
                "s", "t", signature="sig", outcome="failure", resolves=True
            ),
            1,
        )

    def test_reordered_reports_without_ids_preserve_failures(self) -> None:
        reports = (
            ("PostToolUse", False),
            ("PostToolUseFailure", True),
            ("PostToolBatch", False),
            ("PostToolBatch", True),
        )
        for index, order in enumerate(permutations(reports)):
            signature = f"order-{index}"
            with self.subTest(order=order):
                self.start(signature)
                self.start(signature)
                for event, failed in order:
                    counts = self.report(event, signature=signature, failed=failed)
                self.assertEqual(counts["identical"], 2)
                self.assertEqual(counts["failures"], 1)


SEARCH = {"command": "rg -n 'needle' src"}
FAILING = {"command": "pytest tests/integration"}


class HookCallCountingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.base = {
            "session_id": "session-1",
            "cwd": "/work/project",
            "prompt_id": "turn-1",
        }
        self.hook(
            "UserPromptSubmit",
            prompt="Implement a substantial feature with tests and documentation.",
        )

    def hook(self, event: str, **extra: object) -> object:
        return run_hook(
            {**self.base, "hook_event_name": event, **extra},
            store=self.store,
            environ=CLAUDE_ENV,
        )

    def call(self, tool_input: dict[str, str], *, failed: bool = False) -> list[object]:
        """One tool call as Claude Code reports it: pre, single result, batch."""

        outputs = [self.hook("PreToolUse", tool_name="Bash", tool_input=tool_input)]
        if failed:
            outputs.append(
                self.hook(
                    "PostToolUseFailure",
                    tool_name="Bash",
                    tool_input=tool_input,
                    error="failed",
                )
            )
            batch_response: object = {"is_error": True}
        else:
            outputs.append(
                self.hook(
                    "PostToolUse",
                    tool_name="Bash",
                    tool_input=tool_input,
                    tool_response={"stdout": "match"},
                )
            )
            batch_response = "match"
        outputs.append(
            self.hook(
                "PostToolBatch",
                tool_calls=[
                    {
                        "tool_name": "Bash",
                        "tool_input": tool_input,
                        "tool_response": batch_response,
                    }
                ],
            )
        )
        return outputs

    def session(self) -> dict[str, object]:
        return self.store.get_session("session-1")

    @staticmethod
    def mentions(outputs: list[object], phrase: str) -> int:
        return sum(1 for output in outputs if phrase in repr(output).casefold())

    def test_one_call_reported_three_times_counts_once(self) -> None:
        outputs = self.call(SEARCH)
        self.assertEqual(self.mentions(outputs, "same action"), 0)
        self.assertEqual(self.session()["tool_count"], 1)
        self.assertEqual(self.session()["repeated_action_count"], 0)

    def test_different_calls_never_get_repeat_nudge(self) -> None:
        outputs: list[object] = []
        for command in ("rg -n a src", "rg -n b src", "rg -n c src"):
            outputs += self.call({"command": command})
        self.assertEqual(self.mentions(outputs, "same action"), 0)
        self.assertEqual(self.session()["tool_count"], 3)

    def test_third_identical_call_gets_exactly_one_repeat_nudge(self) -> None:
        first = self.call(SEARCH)
        second = self.call(SEARCH)
        third = self.call(SEARCH)
        self.assertEqual(self.mentions(first + second, "same action"), 0)
        self.assertEqual(self.mentions(third, "same action"), 1)
        self.assertEqual(self.session()["repeated_action_count"], 1)
        self.assertEqual(self.session()["tool_count"], 3)

    def test_one_failure_reported_twice_gets_no_failure_loop_nudge(self) -> None:
        outputs = self.call(FAILING, failed=True)
        self.assertEqual(self.mentions(outputs, "failed repeatedly"), 0)
        self.assertEqual(self.session()["failure_count"], 1)

    def test_second_identical_failure_gets_failure_loop_nudge(self) -> None:
        self.call(FAILING, failed=True)
        second = self.call(FAILING, failed=True)
        self.assertGreaterEqual(self.mentions(second, "failed repeatedly"), 1)
        self.assertEqual(self.session()["failure_count"], 2)

    def test_duplicate_failure_report_is_not_a_repeat_after_the_nudge(self) -> None:
        self.call(FAILING, failed=True)
        self.call(FAILING, failed=True)
        with closing(sqlite3.connect(self.store.paths.database)) as conn:
            outcomes = conn.execute(
                """
                SELECT o.outcome
                  FROM intervention_outcomes o
                  JOIN interventions i ON i.id = o.intervention_id
                 WHERE i.policy_id = 'core.break-failure-loop'
                """
            ).fetchall()
        self.assertNotIn(("failure_repeated",), outcomes)

    def nudge_first(self) -> dict[str, object]:
        """Emit a verify-change nudge so later check results attach to it."""

        self.hook(
            "PostToolUse",
            tool_name="Edit",
            tool_input={"file_path": "/work/project/src/app.py"},
            tool_response={"success": True},
        )
        latest = self.store.latest_intervention("session-1")
        self.assertIsNotNone(latest)
        self.assertEqual(latest["policy_id"], "core.verify-change")
        return latest

    def check_outcomes(self, intervention_id: object, outcome: str) -> int:
        with closing(sqlite3.connect(self.store.paths.database)) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM intervention_outcomes "
                "WHERE intervention_id = ? AND outcome = ?",
                (intervention_id, outcome),
            ).fetchone()
        return int(row[0])

    def test_failed_check_reported_twice_records_one_outcome(self) -> None:
        nudge = self.nudge_first()
        self.call(FAILING, failed=True)
        self.assertEqual(self.check_outcomes(nudge["id"], "check_failed"), 1)

    def test_passed_check_reported_twice_records_one_outcome(self) -> None:
        nudge = self.nudge_first()
        self.call(FAILING)
        self.assertEqual(self.check_outcomes(nudge["id"], "check_passed"), 1)

    def policy_outcomes(self, policy_id: str) -> list[str]:
        with closing(sqlite3.connect(self.store.paths.database)) as conn:
            rows = conn.execute(
                """
                SELECT o.outcome
                  FROM intervention_outcomes o
                  JOIN interventions i ON i.id = o.intervention_id
                 WHERE i.policy_id = ?
                """,
                (policy_id,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def test_failed_check_duplicate_is_not_credited_to_a_later_nudge(self) -> None:
        self.call(FAILING, failed=True)
        self.call(FAILING, failed=True)
        # The second failure's own result emits the failure-loop nudge; its
        # batch duplicate must not record the same check against that nudge.
        self.assertNotIn(
            "check_failed", self.policy_outcomes("core.break-failure-loop")
        )

    def test_passed_check_duplicate_is_not_credited_to_a_later_nudge(self) -> None:
        passing = {"command": "pytest tests/unit"}
        self.call(FAILING, failed=True)
        # Two calls in parallel: the passing check finishes first, then the
        # repeated failure emits the failure-loop nudge, then one batch reports both.
        self.hook("PreToolUse", tool_name="Bash", tool_input=passing)
        self.hook("PreToolUse", tool_name="Bash", tool_input=FAILING)
        self.hook(
            "PostToolUse",
            tool_name="Bash",
            tool_input=passing,
            tool_response={"stdout": "ok"},
        )
        self.hook(
            "PostToolUseFailure",
            tool_name="Bash",
            tool_input=FAILING,
            error="failed",
        )
        self.assertIsNotNone(self.store.latest_intervention("session-1"))
        self.hook(
            "PostToolBatch",
            tool_calls=[
                {"tool_name": "Bash", "tool_input": passing, "tool_response": "ok"},
                {
                    "tool_name": "Bash",
                    "tool_input": FAILING,
                    "tool_response": {"is_error": True},
                },
            ],
        )
        self.assertNotIn(
            "check_passed", self.policy_outcomes("core.break-failure-loop")
        )

    def test_named_calls_can_use_different_result_families(self) -> None:
        for call_id in ("call-a", "call-b"):
            self.hook(
                "PreToolUse",
                tool_name="Bash",
                tool_input=FAILING,
                tool_use_id=call_id,
            )
        self.hook(
            "PostToolUse",
            tool_name="Bash",
            tool_input=FAILING,
            tool_use_id="call-a",
            tool_response="ok",
        )
        self.hook(
            "PostToolBatch",
            tool_calls=[
                {
                    "tool_name": "Bash",
                    "tool_input": FAILING,
                    "tool_use_id": "call-b",
                    "tool_response": {"is_error": True},
                }
            ],
        )
        self.assertEqual(self.session()["tool_count"], 2)
        self.assertEqual(self.session()["failure_count"], 1)
        self.assertEqual(self.session()["validation_count"], 1)

    def test_replayed_named_result_records_one_check_outcome(self) -> None:
        nudge = self.nudge_first()
        for _ in range(2):
            self.hook(
                "PostToolUse",
                tool_name="Bash",
                tool_input=FAILING,
                tool_use_id="private-call-identifier-6839",
                tool_response="ok",
            )
        self.assertEqual(self.session()["tool_count"], 2)
        self.assertEqual(self.session()["validation_count"], 1)
        self.assertEqual(self.check_outcomes(nudge["id"], "check_passed"), 1)
        with self.store.connect() as conn:
            dump = "\n".join(conn.iterdump())
        self.assertNotIn("private-call-identifier-6839", dump)

    def test_reordered_named_results_preserve_failure(self) -> None:
        for call_id in ("call-a", "call-b"):
            self.hook(
                "PreToolUse",
                tool_name="Bash",
                tool_input=FAILING,
                tool_use_id=call_id,
            )
        self.hook(
            "PostToolUse",
            tool_name="Bash",
            tool_input=FAILING,
            tool_use_id="call-b",
            tool_response="ok",
        )
        self.hook(
            "PostToolBatch",
            tool_calls=[
                {
                    "tool_name": "Bash",
                    "tool_input": FAILING,
                    "tool_use_id": "call-a",
                    "tool_response": {"is_error": True},
                },
                {
                    "tool_name": "Bash",
                    "tool_input": FAILING,
                    "tool_use_id": "call-b",
                    "tool_response": "ok",
                },
            ],
        )
        self.hook(
            "PostToolUseFailure",
            tool_name="Bash",
            tool_input=FAILING,
            tool_use_id="call-a",
            error="failed",
        )
        self.assertEqual(self.session()["tool_count"], 2)
        self.assertEqual(self.session()["failure_count"], 1)
        self.assertEqual(self.session()["validation_count"], 1)

    def test_batch_without_identifier_matches_a_named_result(self) -> None:
        self.hook(
            "PostToolBatch",
            tool_calls=[
                {
                    "tool_name": "Bash",
                    "tool_input": FAILING,
                    "tool_response": {"is_error": True},
                }
            ],
        )
        for _ in range(2):
            self.hook(
                "PostToolUseFailure",
                tool_name="Bash",
                tool_input=FAILING,
                tool_use_id="call-a",
                error="failed",
            )
        self.assertEqual(self.session()["tool_count"], 1)
        self.assertEqual(self.session()["failure_count"], 1)
