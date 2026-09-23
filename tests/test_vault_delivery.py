from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from agent_efficiency.hook import CURSOR_SESSION_CONTEXT, run_hook
from agent_efficiency.store import Store
from agent_efficiency.vault.render import UNMAPPED
from agent_efficiency.vault_delivery import (
    deliver_vault_context,
    session_cause,
    strip_agent_prefix,
)
from vault_fixtures import (
    make_repo,
    make_vault_tree,
    note_text,
    recommit,
    register_trees,
)

HEAD = "Next: finish the API slice."


class VaultDeliveryBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.store = Store(self.base / "data")
        self.project = make_repo(
            self.base / "catalog",
            remotes={"origin": "https://git.example.com/example/catalog.git"},
        )
        (self.project / "src").mkdir()
        self.core = make_vault_tree(
            self.base / "vault-core",
            "core",
            [
                note_text(
                    "writing",
                    note_type="feedback",
                    classification="core",
                    hook="Plain English, short sentences.",
                )
            ],
        )
        self.private = make_vault_tree(
            self.base / "vault-private",
            "private",
            [
                note_text("catalog", repos=("example/catalog",), body=HEAD),
                note_text("calendar", hook="calendar next step"),
                note_text(
                    "style", note_type="feedback", hook="Show the working thing."
                ),
            ],
        )
        register_trees(self.store.paths.root, self.core, self.private)

    def receipts(self, session: str) -> list[tuple[str, str, str | None]]:
        with closing(sqlite3.connect(self.store.paths.database)) as conn:
            return conn.execute(
                "SELECT cause, disposition, reason_code FROM vault_receipts "
                "WHERE session_id = ? ORDER BY id",
                (session,),
            ).fetchall()


class DeliveryUnitTests(VaultDeliveryBase):
    def setUp(self) -> None:
        super().setUp()
        self.store.ensure_session("s1", host="claude", cwd=str(self.project))

    def test_session_cause_maps_host_sources(self) -> None:
        for source, cause in (
            ("startup", "new"),
            ("clear", "new"),
            ("resume", "resume"),
            ("compact", "compact"),
            ("", "new"),
            ("something-new", "new"),
        ):
            with self.subTest(source=source):
                self.assertEqual(session_cause({"source": source}), cause)

    def test_strip_agent_prefix_handles_both_output_shapes(self) -> None:
        self.assertEqual(
            strip_agent_prefix(
                {"hookSpecificOutput": {"additionalContext": "Agent Efficiency: x"}}
            ),
            {"hookSpecificOutput": {"additionalContext": "x"}},
        )
        self.assertEqual(
            strip_agent_prefix({"additional_context": "Agent Efficiency: y"}),
            {"additional_context": "y"},
        )
        self.assertIsNone(strip_agent_prefix(None))

    def test_delivers_without_the_guidance_label(self) -> None:
        delivery = deliver_vault_context(
            self.store,
            "s1",
            "claude",
            "SessionStart",
            self.project,
            cause="new",
            mode="advise",
        )
        text = delivery.output["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(text.startswith("Vault context:"))
        self.assertIn(HEAD, text)
        self.assertEqual(self.receipts("s1"), [("new", "delivered", None)])

    def test_host_without_add_context_is_unavailable(self) -> None:
        delivery = deliver_vault_context(
            self.store,
            "s1",
            "claude",
            "Stop",
            self.project,
            cause="request",
            mode="advise",
        )
        self.assertIsNone(delivery.output)
        self.assertEqual(
            self.receipts("s1"), [("request", "unavailable", "host_unsupported")]
        )

    def test_vault_context_is_not_recorded_as_guidance(self) -> None:
        deliver_vault_context(
            self.store,
            "s1",
            "claude",
            "SessionStart",
            self.project,
            cause="new",
            mode="advise",
        )
        with closing(sqlite3.connect(self.store.paths.database)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM interventions").fetchone()[0]
        self.assertEqual(count, 0)


CLAUDE_ENV = {"CLAUDE_PLUGIN_ROOT": "/plugin"}
CODEX_ENV = {"PLUGIN_ROOT": "/plugin", "CLAUDE_PLUGIN_ROOT": "/plugin"}


class SessionStartDeliveryTests(VaultDeliveryBase):
    def claude(
        self,
        event: str,
        session: str = "s-claude",
        env: dict[str, str] = CLAUDE_ENV,
        cwd: Path | None = None,
        **extra: object,
    ):
        payload = {
            "session_id": session,
            "cwd": str(cwd or self.project / "src"),
            "hook_event_name": event,
            **extra,
        }
        return run_hook(payload, store=self.store, environ=env)

    def cursor(self, event: str, session: str = "s-cursor", **extra: object):
        payload = {
            "conversation_id": session,
            "generation_id": "g-1",
            "cursor_version": "1.7.2",
            "workspace_roots": [str(self.project)],
            "hook_event_name": event,
            **extra,
        }
        return run_hook(payload, store=self.store)

    def test_claude_session_start_injects_the_head_and_index_lines(self) -> None:
        output = self.claude("SessionStart", source="startup")
        context = output["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "SessionStart")
        text = context["additionalContext"]
        self.assertTrue(text.startswith("Vault context:"))
        for expected in (
            HEAD,
            "- feedback/writing: Plain English, short sentences.",
            "- feedback/style: Show the working thing.",
            "- projects/calendar: calendar next step",
        ):
            self.assertIn(expected, text)
        self.assertEqual(self.receipts("s-claude"), [("new", "delivered", None)])

    def test_codex_session_start_uses_the_same_contract(self) -> None:
        output = self.claude(
            "SessionStart", session="s-codex", env=CODEX_ENV, source="startup"
        )
        self.assertIn(HEAD, output["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.store.get_session("s-codex")["host"], "codex")

    def test_cursor_session_start_keeps_the_quality_contract(self) -> None:
        text = self.cursor("sessionStart")["additional_context"]
        self.assertTrue(text.startswith(CURSOR_SESSION_CONTEXT))
        self.assertIn(HEAD, text)

    def test_resume_skips_unchanged_context_and_redelivers_changes(self) -> None:
        self.claude("SessionStart", source="startup")
        self.assertIsNone(self.claude("SessionStart", source="resume"))
        note = self.private / "projects" / "catalog.md"
        note.write_text(
            note.read_text(encoding="utf-8").replace(HEAD, "Next: CLI slice."),
            encoding="utf-8",
        )
        recommit(self.private)
        output = self.claude("SessionStart", source="resume")
        self.assertIn(
            "Next: CLI slice.", output["hookSpecificOutput"]["additionalContext"]
        )
        self.assertEqual(
            [row[:2] for row in self.receipts("s-claude")],
            [("new", "delivered"), ("resume", "skipped"), ("resume", "delivered")],
        )

    def test_compaction_always_redelivers(self) -> None:
        self.claude("SessionStart", source="startup")
        output = self.claude("SessionStart", source="compact")
        self.assertIn(HEAD, output["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.receipts("s-claude")[-1], ("compact", "delivered", None))

    def test_observe_mode_records_but_withholds(self) -> None:
        self.store.set_default_mode("observe")
        self.assertIsNone(self.claude("SessionStart", source="startup"))
        self.assertEqual(
            self.receipts("s-claude"), [("new", "withheld", "observe_mode")]
        )

    def test_unmapped_directory_gets_core_only(self) -> None:
        other = self.base / "other"
        other.mkdir()
        output = self.claude("SessionStart", session="s-other", cwd=other)
        text = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn(UNMAPPED, text)
        self.assertIn("feedback/writing", text)
        self.assertNotIn(HEAD, text)
        self.assertNotIn("feedback/style", text)
        self.assertEqual(self.receipts("s-other"), [("new", "delivered", "unmapped")])

    def test_broken_index_degrades_visibly(self) -> None:
        (self.private / "index.json").write_text("{", encoding="utf-8")
        output = self.claude("SessionStart", source="startup")
        self.assertIn(
            "Vault context is unavailable this session (parse_error)",
            output["hookSpecificOutput"]["additionalContext"],
        )
        self.assertEqual(
            self.receipts("s-claude"), [("new", "degraded", "parse_error")]
        )

    def test_no_registered_tree_changes_nothing(self) -> None:
        (self.store.paths.root / "vault.json").unlink()
        self.assertIsNone(self.claude("SessionStart", source="startup"))
        self.assertEqual(
            self.receipts("s-claude"), [("new", "unavailable", "no_trees")]
        )
        self.assertEqual(
            self.cursor("sessionStart"), {"additional_context": CURSOR_SESSION_CONTEXT}
        )


if __name__ == "__main__":
    unittest.main()
