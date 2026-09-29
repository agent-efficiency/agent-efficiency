from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest import mock

import agent_efficiency
from agent_efficiency.hook import (
    CURSOR_SESSION_CONTEXT,
    VAULT_CONFIG_NAME,
    _deferred_vault,
    run_hook,
)
from agent_efficiency.store import Store
from agent_efficiency.vault import prepare as prepare_module
from agent_efficiency.vault.config import CONFIG_NAME
from agent_efficiency.vault.index import write_index
from agent_efficiency.vault.prepare import prepare
from agent_efficiency.vault.render import (
    ALLOWANCE,
    HEADER,
    RESELECT_RESERVE,
    UNMAPPED,
    render,
)
from agent_efficiency.vault_delivery import (
    REPLACES,
    Delivery,
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

    def counts(self, session: str) -> list[tuple]:
        """Return each receipt with its counts, oldest first."""

        with closing(sqlite3.connect(self.store.paths.database)) as conn:
            return conn.execute(
                "SELECT cause, disposition, reason_code, notes_selected, "
                "head_chars, chars_emitted, chars_omitted FROM vault_receipts "
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

    def test_unexpected_failure_degrades_instead_of_raising(self) -> None:
        with mock.patch(
            "agent_efficiency.vault_delivery.prepare",
            side_effect=RuntimeError("unexpected"),
        ):
            delivery = deliver_vault_context(
                self.store,
                "s1",
                "claude",
                "SessionStart",
                self.project,
                cause="new",
                mode="advise",
            )
        self.assertIn(
            "Vault context is unavailable this session (parse_error)",
            delivery.output["hookSpecificOutput"]["additionalContext"],
        )
        self.assertEqual(self.receipts("s1"), [("new", "degraded", "parse_error")])

    def test_no_registered_tree_records_no_receipt(self) -> None:
        (self.store.paths.root / "vault.json").unlink()
        delivery = deliver_vault_context(
            self.store,
            "s1",
            "claude",
            "SessionStart",
            self.project,
            cause="new",
            mode="advise",
        )
        self.assertEqual(delivery, Delivery(None, "unavailable", "no_trees"))
        self.assertEqual(self.receipts("s1"), [])
        self.assertFalse(self.store.has_vault_receipt("s1"))

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
        self.assertEqual(
            self.counts("s-claude"),
            [("new", "delivered", None, 4, len(HEAD), len(text), 0)],
        )

    def test_over_allowance_context_is_truncated_and_counted(self) -> None:
        hook = "h" * 90
        core = make_vault_tree(
            self.base / "vault-core-large",
            "core",
            [
                note_text(
                    f"rule-{number:03d}",
                    note_type="feedback",
                    classification="core",
                    hook=hook,
                )
                for number in range(100)
            ],
        )
        register_trees(self.store.paths.root, core, self.private)
        output = self.claude("SessionStart", source="startup")
        text = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn(HEAD, text)
        self.assertIn("Omitted for space:", text)
        (row,) = self.counts("s-claude")
        self.assertEqual(row[:3], ("new", "truncated", "over_budget"))
        self.assertEqual(row[5], len(text))
        self.assertGreater(row[6], 0)

    def test_cut_project_head_counts_as_omitted(self) -> None:
        body = "z" * 5000
        note = self.private / "projects" / "catalog.md"
        note.write_text(
            note_text("catalog", repos=("example/catalog",), body=body),
            encoding="utf-8",
        )
        write_index(self.private)
        output = self.claude("SessionStart", source="startup")
        text = output["hookSpecificOutput"]["additionalContext"]
        (row,) = self.counts("s-claude")
        self.assertEqual(row[:3], ("new", "truncated", "cap_exceeded"))
        self.assertEqual(row[5], len(text))
        self.assertGreaterEqual(row[6], 2000)

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
        rows = self.counts("s-claude")
        self.assertEqual(
            [row[:2] for row in rows],
            [("new", "delivered"), ("resume", "skipped"), ("resume", "delivered")],
        )
        self.assertEqual(rows[1][5], 0)

    def test_resume_redelivers_an_uncommitted_note_edit(self) -> None:
        self.claude("SessionStart", source="startup")
        note = self.private / "projects" / "catalog.md"
        note.write_text(
            note.read_text(encoding="utf-8").replace(HEAD, "Next: docs slice."),
            encoding="utf-8",
        )
        write_index(self.private)
        output = self.claude("SessionStart", source="resume")
        self.assertIn(
            "Next: docs slice.", output["hookSpecificOutput"]["additionalContext"]
        )
        self.assertEqual(self.receipts("s-claude")[-1], ("resume", "delivered", None))

    def test_explicit_request_reselects_and_delivers(self) -> None:
        self.claude("SessionStart", source="startup")
        output = self.claude(
            "UserPromptSubmit", prompt="$agent-efficiency vault", prompt_id="turn-1"
        )
        context = output["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "UserPromptSubmit")
        self.assertIn(HEAD, context["additionalContext"])
        self.assertEqual(self.receipts("s-claude")[-1], ("request", "delivered", None))

    def test_reselection_says_it_replaces_earlier_context(self) -> None:
        other = make_repo(
            self.base / "other-project",
            remotes={"origin": "https://git.example.com/o/other.git"},
        )
        work = make_vault_tree(
            self.base / "vault-work",
            "work",
            [
                note_text(
                    "other-work",
                    classification="work",
                    repos=("o/other",),
                    body="OTHER-PROJECT-HEAD",
                )
            ],
        )
        register_trees(self.store.paths.root, self.core, self.private, work)
        first = self.claude("SessionStart", session="s-scope", source="startup")
        self.assertNotIn(REPLACES, first["hookSpecificOutput"]["additionalContext"])
        output = self.claude(
            "UserPromptSubmit",
            session="s-scope",
            cwd=other,
            prompt="$agent-efficiency vault",
        )
        text = output["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(text.startswith(f"{HEADER}\n{REPLACES}\n\n"))
        self.assertIn("OTHER-PROJECT-HEAD", text)
        self.assertNotIn(HEAD, text)
        self.assertEqual(
            self.receipts("s-scope"),
            [("new", "delivered", None), ("request", "delivered", None)],
        )
        self.assertEqual(
            self.store.latest_vault_delivery("s-scope")["payload_digest"],
            prepare(other, self.store.paths.root).digest,
        )

    def full_length(self) -> int:
        """Return the length of the vault text with no allowance at all."""

        def unbounded(selection, trees, indexes, **_: object):
            return render(selection, trees, indexes, allowance=10**6)

        with mock.patch.object(prepare_module, "render", unbounded):
            return len(
                prepare(self.project / "src", self.store.paths.root).rendered.text
            )

    def fill_to_allowance(self) -> None:
        """Register a core tree whose whole rendering is exactly the allowance."""

        root = self.base / "vault-core-fill"
        lengths = [90] * 75

        def write(number: int) -> str:
            return note_text(
                f"rule-{number:03d}",
                note_type="feedback",
                classification="core",
                hook="h" * lengths[number],
            )

        core = make_vault_tree(root, "core", [write(n) for n in range(len(lengths))])
        register_trees(self.store.paths.root, core, self.private)
        missing = ALLOWANCE - self.full_length()
        self.assertTrue(0 <= missing <= 10 * len(lengths))
        for number in range(len(lengths)):
            added = min(10, missing)
            if not added:
                break
            lengths[number] += added
            missing -= added
            path = root / "feedback" / f"rule-{number:03d}.md"
            path.write_text(write(number), encoding="utf-8")
        write_index(root)
        self.assertEqual(self.full_length(), ALLOWANCE)

    def test_reselection_line_fits_inside_the_allowance(self) -> None:
        self.assertGreater(RESELECT_RESERVE, len(REPLACES) + 1)
        self.fill_to_allowance()
        self.claude("SessionStart", source="startup")
        output = self.claude("UserPromptSubmit", prompt="$agent-efficiency vault")
        text = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn(REPLACES, text)
        self.assertIn(HEAD, text)
        self.assertLessEqual(len(text), ALLOWANCE)
        rows = self.counts("s-claude")
        self.assertEqual(rows[-1][:2], ("request", "truncated"))
        self.assertEqual(rows[-1][5], len(text))

    def test_explicit_request_on_cursor_reloads_at_the_next_tool_result(
        self,
    ) -> None:
        self.cursor("sessionStart")
        output = self.cursor("beforeSubmitPrompt", prompt="$agent-efficiency vault")
        self.assertFalse(output["continue"])
        self.assertIn("next successful tool result", output["user_message"])
        self.assertEqual(
            self.receipts("s-cursor")[-1],
            ("request", "unavailable", "host_unsupported"),
        )
        reloaded = self.tool_result("s-cursor")["additional_context"]
        self.assertIn(HEAD, reloaded)
        self.assertIn(REPLACES, reloaded)
        again = self.tool_result("s-cursor") or {}
        self.assertNotIn(HEAD, again.get("additional_context", ""))
        self.assertEqual(
            [row[:2] for row in self.receipts("s-cursor")],
            [
                ("new", "delivered"),
                ("request", "unavailable"),
                ("request", "delivered"),
            ],
        )

    def test_concurrent_tool_results_reload_the_cursor_request_once(self) -> None:
        self.cursor("sessionStart", session="s-reload")
        self.cursor(
            "beforeSubmitPrompt", session="s-reload", prompt="$agent-efficiency vault"
        )
        with mock.patch.object(Store, "vault_request_pending", return_value=True):
            results = [
                _deferred_vault(self.store, "s-reload", str(self.project), "advise")
                for _ in range(2)
            ]
        delivered = [
            result
            for result in results
            if result and HEAD in result.get("additional_context", "")
        ]
        self.assertEqual(len(delivered), 1)

    def test_explicit_request_in_off_mode_records_nothing(self) -> None:
        self.claude("UserPromptSubmit", prompt="$agent-efficiency off")
        output = self.claude("UserPromptSubmit", prompt="$agent-efficiency vault")
        self.assertIn(
            "Agent Efficiency is off for this session. Turn it on to load vault "
            "context.",
            output["stopReason"],
        )
        self.assertEqual(self.receipts("s-claude"), [])

    def test_explicit_request_without_a_vault_says_how_to_register(self) -> None:
        (self.store.paths.root / "vault.json").unlink()
        output = self.claude("UserPromptSubmit", prompt="$agent-efficiency vault")
        self.assertIn("vault register", output["stopReason"])

    def tool_result(self, session: str):
        return self.cursor(
            "postToolUse",
            session=session,
            tool_name="Read",
            tool_input={"file_path": str(self.project / "README")},
            tool_output='{"success":true}',
            duration=5,
        )

    def test_cursor_without_session_start_gets_the_vault_at_the_first_tool_result(
        self,
    ) -> None:
        output = self.tool_result("s-cloud")
        self.assertIn(HEAD, output["additional_context"])
        self.assertEqual(self.receipts("s-cloud"), [("deferred", "deferred", None)])
        again = self.tool_result("s-cloud") or {}
        self.assertNotIn(HEAD, again.get("additional_context", ""))

    def test_concurrent_tool_results_deliver_the_deferred_vault_once(self) -> None:
        self.cursor(
            "beforeSubmitPrompt", session="s-race", prompt="Explain the parser."
        )
        barrier = threading.Barrier(4)

        def worker(_: int):
            barrier.wait(timeout=10)
            return _deferred_vault(self.store, "s-race", str(self.project), "advise")

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(worker, range(4)))
        delivered = [
            result
            for result in results
            if result and HEAD in result.get("additional_context", "")
        ]
        self.assertEqual(len(delivered), 1)
        self.assertEqual(self.receipts("s-race"), [("deferred", "deferred", None)])

    def test_deferred_vault_is_not_sent_when_its_claim_fails(self) -> None:
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.object(Store, "record_vault_receipt_once", side_effect=locked):
            output = self.tool_result("s-locked") or {}
        self.assertNotIn(HEAD, output.get("additional_context", ""))
        self.assertEqual(self.receipts("s-locked"), [])
        retry = self.tool_result("s-locked")
        self.assertIn(HEAD, retry["additional_context"])
        again = self.tool_result("s-locked") or {}
        self.assertNotIn(HEAD, again.get("additional_context", ""))
        self.assertEqual(self.receipts("s-locked"), [("deferred", "deferred", None)])

    def test_cursor_tool_result_surfaces_an_unreadable_tree_list(self) -> None:
        config = self.store.paths.root / VAULT_CONFIG_NAME
        original = Path.is_file

        def is_file(path: Path) -> bool:
            if path == config:
                raise PermissionError(13, "Permission denied", str(path))
            return original(path)

        with mock.patch.object(Path, "is_file", is_file):
            output = self.tool_result("s-denied")
        self.assertIn(
            "Vault context is unavailable this session (parse_error)",
            output["additional_context"],
        )
        self.assertEqual(
            self.receipts("s-denied"), [("deferred", "degraded", "parse_error")]
        )

    def test_cursor_vault_request_does_not_block_the_deferred_delivery(
        self,
    ) -> None:
        request = self.cursor(
            "beforeSubmitPrompt", session="s-cloud", prompt="$agent-efficiency vault"
        )
        self.assertIn("next successful tool result", request["user_message"])
        output = self.tool_result("s-cloud")
        self.assertIn(HEAD, output["additional_context"])
        again = self.tool_result("s-cloud") or {}
        self.assertNotIn(HEAD, again.get("additional_context", ""))
        self.assertEqual(
            self.receipts("s-cloud"),
            [
                ("request", "unavailable", "host_unsupported"),
                ("deferred", "deferred", None),
            ],
        )

    def test_cursor_code_edit_merges_the_checkpoint_and_the_vault(self) -> None:
        self.cursor(
            "beforeSubmitPrompt",
            session="s-edit",
            prompt="Implement a substantial feature with tests.",
        )
        output = self.cursor(
            "postToolUse",
            session="s-edit",
            tool_name="Write",
            tool_input={"file_path": str(self.project / "app.py"), "content": "x"},
            tool_output='{"success":true}',
            duration=8,
        )
        text = output["additional_context"]
        self.assertTrue(text.startswith("Agent Efficiency: Quality checkpoint:"))
        self.assertIn("\n\nVault context:", text)
        self.assertIn(HEAD, text)
        self.assertEqual(self.receipts("s-edit"), [("deferred", "deferred", None)])

    def test_cursor_with_session_start_is_not_delivered_twice(self) -> None:
        self.cursor("sessionStart", session="s-local")
        output = self.tool_result("s-local") or {}
        self.assertNotIn(HEAD, output.get("additional_context", ""))

    def test_cursor_restores_the_vault_at_the_tool_result_after_compaction(
        self,
    ) -> None:
        self.cursor("sessionStart", session="s-local")
        self.cursor("preCompact", session="s-local")
        output = self.tool_result("s-local") or {}
        text = output.get("additional_context", "")
        self.assertIn(HEAD, text)
        self.assertIn(REPLACES, text)
        again = self.tool_result("s-local") or {}
        self.assertNotIn(HEAD, again.get("additional_context", ""))
        self.assertEqual(
            [row[:2] for row in self.receipts("s-local")],
            [("new", "delivered"), ("compact", "delivered")],
        )
        self.assertFalse(self.store.vault_compaction_due("s-local"))

    def test_cursor_restores_once_per_compaction_without_session_start(
        self,
    ) -> None:
        self.assertIn(HEAD, self.tool_result("s-cloud")["additional_context"])
        for _ in range(2):
            self.cursor("preCompact", session="s-cloud")
            self.assertIn(HEAD, self.tool_result("s-cloud")["additional_context"])
            again = self.tool_result("s-cloud") or {}
            self.assertNotIn(HEAD, again.get("additional_context", ""))
        self.assertEqual(
            [row[:2] for row in self.receipts("s-cloud")],
            [
                ("deferred", "deferred"),
                ("compact", "delivered"),
                ("compact", "delivered"),
            ],
        )

    def test_compaction_always_redelivers(self) -> None:
        self.claude("SessionStart", source="startup")
        output = self.claude("SessionStart", source="compact")
        self.assertIn(HEAD, output["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.receipts("s-claude")[-1], ("compact", "delivered", None))

    def test_claude_compaction_delivers_once_at_session_start(self) -> None:
        self.claude("SessionStart", source="startup")
        self.assertIsNone(self.claude("PreCompact"))
        output = self.claude("SessionStart", source="compact")
        self.assertIn(HEAD, output["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.claude("PostCompact"))
        self.assertEqual(
            self.receipts("s-claude"),
            [("new", "delivered", None), ("compact", "delivered", None)],
        )

    def test_compaction_delivers_once_when_post_compact_comes_first(self) -> None:
        self.claude("SessionStart", source="startup")
        self.claude("PreCompact")
        output = self.claude("PostCompact")
        context = output["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "PostCompact")
        self.assertIn(HEAD, context["additionalContext"])
        self.assertIsNone(self.claude("SessionStart", source="compact"))
        self.assertEqual(
            self.receipts("s-claude"),
            [
                ("new", "delivered", None),
                ("compact", "delivered", None),
                ("compact", "skipped", "unchanged"),
            ],
        )

    def test_each_compaction_delivers_again(self) -> None:
        self.claude("SessionStart", source="startup")
        for _ in range(2):
            self.claude("PreCompact")
            output = self.claude("SessionStart", source="compact")
            self.assertIn(HEAD, output["hookSpecificOutput"]["additionalContext"])
            self.assertIsNone(self.claude("PostCompact"))
        self.assertEqual(
            [row[:2] for row in self.receipts("s-claude")],
            [("new", "delivered"), ("compact", "delivered"), ("compact", "delivered")],
        )

    def test_racing_compaction_deliveries_emit_once(self) -> None:
        """Two deliveries that both passed the early check still emit once.

        The session start and the PostCompact event of one compaction can run
        at the same time. Only one of them may record the compact receipt, or
        the receipt count overtakes the compaction count and the next
        compaction is treated as already handled.
        """

        self.claude("SessionStart", source="startup")
        self.claude("PreCompact")
        with mock.patch.object(Store, "vault_compaction_due", return_value=True):
            first = self.claude("SessionStart", source="compact")
            second = self.claude("PostCompact")
        emitted = [
            output
            for output in (first, second)
            if output and HEAD in output["hookSpecificOutput"]["additionalContext"]
        ]
        self.assertEqual(len(emitted), 1)
        self.assertEqual(
            [row[:2] for row in self.receipts("s-claude")],
            [("new", "delivered"), ("compact", "delivered")],
        )
        self.claude("PreCompact")
        output = self.claude("SessionStart", source="compact")
        self.assertIn(HEAD, output["hookSpecificOutput"]["additionalContext"])

    def test_codex_compaction_delivers_at_post_compact(self) -> None:
        for session, pre_compact in (("s-codex", False), ("s-codex-pre", True)):
            with self.subTest(pre_compact=pre_compact):
                self.claude(
                    "SessionStart", session=session, env=CODEX_ENV, source="startup"
                )
                if pre_compact:
                    self.claude("PreCompact", session=session, env=CODEX_ENV)
                output = self.claude("PostCompact", session=session, env=CODEX_ENV)
                text = output["hookSpecificOutput"]["additionalContext"]
                self.assertTrue(text.startswith(f"{HEADER}\n{REPLACES}\n\n"))
                self.assertIn(HEAD, text)
                self.assertEqual(
                    self.receipts(session),
                    [("new", "delivered", None), ("compact", "delivered", None)],
                )

    def test_post_compact_in_off_mode_delivers_nothing(self) -> None:
        self.claude("SessionStart", source="startup")
        self.claude("UserPromptSubmit", prompt="$agent-efficiency off")
        self.assertIsNone(self.claude("PostCompact"))
        self.assertEqual(self.receipts("s-claude"), [("new", "delivered", None)])

    def test_observe_mode_records_but_withholds(self) -> None:
        self.store.set_default_mode("observe")
        self.assertIsNone(self.claude("SessionStart", source="startup"))
        (row,) = self.counts("s-claude")
        self.assertEqual(row[:3], ("new", "withheld", "observe_mode"))
        self.assertEqual(row[5], 0)

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
        self.assertEqual(self.receipts("s-claude"), [])
        self.assertEqual(
            self.cursor("sessionStart"), {"additional_context": CURSOR_SESSION_CONTEXT}
        )


LAZY_IMPORT_PROBE = """
import json
import sys
import tempfile
from pathlib import Path

from agent_efficiency.hook import run_hook
from agent_efficiency.store import Store

payload, environ = json.loads(sys.argv[1])
with tempfile.TemporaryDirectory() as temp:
    store = Store(Path(temp) / "data")
    payload = json.loads(json.dumps(payload).replace("@TEMP@", temp))
    run_hook(payload, store=store, environ=environ)
loaded = sorted(
    name
    for name in sys.modules
    if name in {"agent_efficiency.vault", "agent_efficiency.vault_delivery"}
    or name.startswith("agent_efficiency.vault.")
)
print(",".join(loaded))
"""
LAZY_IMPORT_CASES = {
    "claude prompt": (
        {
            "session_id": "s-lazy",
            "cwd": "@TEMP@",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Explain the parser.",
            "prompt_id": "t1",
        },
        {"CLAUDE_PLUGIN_ROOT": "/plugin"},
    ),
    "claude compaction without a vault": (
        {
            "session_id": "s-lazy",
            "cwd": "@TEMP@",
            "hook_event_name": "PostCompact",
        },
        {"CLAUDE_PLUGIN_ROOT": "/plugin"},
    ),
    "cursor tool result without a vault": (
        {
            "conversation_id": "s-lazy",
            "generation_id": "g-1",
            "cursor_version": "1.7.2",
            "workspace_roots": ["@TEMP@"],
            "hook_event_name": "postToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "@TEMP@/README"},
            "tool_output": '{"success":true}',
            "duration": 5,
        },
        {},
    ),
}


class HookImportTests(unittest.TestCase):
    def test_an_ordinary_hook_event_does_not_load_vault_delivery(self) -> None:
        source = Path(agent_efficiency.__file__).resolve().parents[1]
        for name, case in LAZY_IMPORT_CASES.items():
            with self.subTest(case=name):
                result = subprocess.run(
                    [sys.executable, "-c", LAZY_IMPORT_PROBE, json.dumps(case)],
                    env={"PYTHONPATH": str(source), "PATH": "/usr/bin:/bin"},
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(result.stdout.strip(), "")

    def test_config_name_matches_the_vault_config(self) -> None:
        self.assertEqual(VAULT_CONFIG_NAME, CONFIG_NAME)


if __name__ == "__main__":
    unittest.main()
