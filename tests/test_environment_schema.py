"""Schema-8 receipts survive the closed schema-9 reason migration and rollback."""

import sqlite3
import unittest

import test_verification
from agent_efficiency.store import RECEIPT_REASON_COLUMN, _sql_choices
from agent_efficiency.verification.config import load_project_config
from agent_efficiency.verification.runner import run_checks


class EnvironmentSchemaTests(unittest.TestCase):
    setUp = test_verification.VerificationTests.setUp

    def legacy(self):
        config = load_project_config(self.root)
        (receipt,) = run_checks(config, config.checks, self.store)
        with self.store.connect() as conn:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='verification_receipts'"
            ).fetchone()[0]
            old = (
                "reason_code TEXT CHECK(reason_code IS NULL OR reason_code IN ("
                + _sql_choices(
                    (
                        "exit_nonzero",
                        "timeout",
                        "not_started",
                        "workspace_unknown",
                        "workspace_changed",
                        "workspace_unreadable",
                    )
                )
                + "))"
            )
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ALTER TABLE verification_receipts RENAME TO current_receipts")
            conn.execute(sql.replace(RECEIPT_REASON_COLUMN, old))
            conn.execute(
                "INSERT INTO verification_receipts SELECT * FROM current_receipts"
            )
            conn.execute("DROP TABLE current_receipts")
            conn.execute(
                "CREATE INDEX verification_receipts_project ON verification_receipts(project_key,check_id,id DESC)"
            )
            conn.execute("UPDATE settings SET value='8' WHERE key='schema_version'")
        return receipt

    def test_preserves_receipts_and_keeps_private_schema8_backup(self):
        old = self.legacy()
        self.store.ensure_current_schema()
        self.assertEqual(self.store.get_setting("schema_version"), "9")
        new = self.store.verification_receipt(old["receipt_id"])
        for key, value in old.items():
            if key != "receipt_id":
                self.assertEqual(new[key], value, key)
        backup = self.store.paths.database.with_name(
            self.store.paths.database.name + ".schema-8.bak"
        )
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.stat().st_mode & 0o077, 0)
        with sqlite3.connect(backup) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM settings WHERE key='schema_version'"
                ).fetchone()[0],
                "8",
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM verification_receipts").fetchone()[
                    0
                ],
                1,
            )
        before = backup.read_bytes()
        self.store.ensure_current_schema()
        self.assertEqual(before, backup.read_bytes())

    def test_rejects_arbitrary_reason_after_upgrade(self):
        self.legacy()
        self.store.ensure_current_schema()
        with self.assertRaises(sqlite3.IntegrityError), self.store.connect() as conn:
            conn.execute(
                "UPDATE verification_receipts SET reason_code='private-secret'"
            )
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE verification_receipts SET result='inconclusive', reason_code='environment_unknown'"
            )

    def test_newer_schema_is_not_silently_downgraded(self):
        self.store.set_setting("schema_version", "999")
        with self.assertRaises(ValueError):
            self.store.ensure_current_schema()
        self.assertEqual(self.store.get_setting("schema_version"), "999")
