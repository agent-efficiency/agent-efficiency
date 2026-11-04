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
