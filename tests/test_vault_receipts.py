from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing

from agent_efficiency.report import build_report, format_report
from agent_efficiency.store import Store


class VaultReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.store.ensure_session("s1", host="claude", cwd="/work/project")

    def record(self, **overrides: object) -> int:
        values: dict[str, object] = {
            "cause": "new",
            "vault_revision": "a" * 16,
            "payload_digest": "b" * 16,
            "notes_selected": 3,
            "head_chars": 120,
            "chars_emitted": 900,
            "chars_omitted": 0,
            "disposition": "delivered",
            "reason_code": None,
        }
        values.update(overrides)
        return self.store.record_vault_receipt("s1", **values)

    def test_records_and_returns_the_latest_delivery(self) -> None:
        self.record()
        self.record(
            disposition="skipped",
            reason_code="unchanged",
            payload_digest="c" * 16,
            chars_emitted=0,
        )
        self.assertEqual(
            self.store.latest_vault_delivery("s1")["payload_digest"], "b" * 16
        )
        self.assertTrue(self.store.has_vault_receipt("s1"))
        self.assertFalse(self.store.has_vault_receipt("s2"))
        self.assertIsNone(self.store.latest_vault_delivery("s2"))

    def test_report_includes_the_vault_line(self) -> None:
        self.record()
        data = build_report(self.store, 30)
        self.assertEqual(data["vault"]["emitted"], 1)
        self.assertIn(
            "Vault context: selected 1 | emitted 1 (900 characters, project "
            "heads 120) | deferred 0 | unavailable 0 | skipped 0 | withheld 0 | "
            "degraded 0\n",
            format_report(data),
        )

    def test_report_omits_the_vault_line_without_receipts(self) -> None:
        data = build_report(self.store, 30)
        self.assertEqual(data["vault"]["receipts"], 0)
        self.assertNotIn("Vault context", format_report(data))

    def test_rejects_free_text(self) -> None:
        for override in (
            {"cause": "boot"},
            {"disposition": "sent"},
            {"reason_code": "catalog missing"},
            {"vault_revision": "catalog"},
            {"payload_digest": "B" * 16},
            {"notes_selected": -1},
            {"chars_emitted": True},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.record(**override)

    def test_summary_counts_each_quantity_separately(self) -> None:
        self.record()
        self.record(
            cause="deferred", disposition="deferred", chars_emitted=400, head_chars=0
        )
        self.record(
            disposition="unavailable", reason_code="host_unsupported", chars_emitted=0
        )
        self.record(disposition="withheld", reason_code="observe_mode", chars_emitted=0)
        summary = self.store.vault_summary(30)
        self.assertEqual(
            (
                summary["receipts"],
                summary["selected"],
                summary["emitted"],
                summary["chars_emitted"],
                summary["head_chars"],
                summary["deferred"],
                summary["unavailable"],
                summary["withheld"],
            ),
            (4, 4, 2, 1300, 120, 1, 1, 1),
        )
        self.assertEqual(summary["reasons"], {"host_unsupported": 1, "observe_mode": 1})

    def test_migrates_a_schema_6_database(self) -> None:
        with closing(sqlite3.connect(self.store.paths.database)) as conn:
            conn.execute("DROP TABLE vault_receipts")
            conn.execute("UPDATE settings SET value = '6' WHERE key = 'schema_version'")
            conn.commit()
        migrated = Store(self.temp.name)
        migrated.ensure_current_schema()
        self.assertEqual(migrated.get_setting("schema_version"), "7")
        backup = migrated.paths.database.with_name(
            f"{migrated.paths.database.name}.schema-6.bak"
        )
        self.assertTrue(backup.exists())
        self.record()


if __name__ == "__main__":
    unittest.main()
