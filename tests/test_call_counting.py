from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from agent_efficiency.store import SCHEMA_VERSION, Store


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
