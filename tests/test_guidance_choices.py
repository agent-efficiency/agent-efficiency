from __future__ import annotations

import json
import io
import re
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
from datetime import date
from pathlib import Path
from unittest.mock import patch

from agent_efficiency.adapters import adapter_for
from agent_efficiency.contracts.effects import CanonicalEffect
from agent_efficiency.hook import run_hook
from agent_efficiency.store import Store


class GuidanceChoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        (self.project / "src/auth").mkdir(parents=True)
        self.store = Store(self.root / "data")
        self.proposal = {
            "kind": "search-start",
            "target": "src/auth",
            "terms": ["auth"],
            "title": "Start auth searches in its component",
            "reason": "The saved project note identifies this auth component.",
            "source": "Project note: auth layout",
            "expires": "2099-01-01",
        }
        self.write()

    def write(self, proposals=None) -> None:
        (self.project / "agent-efficiency-guidance.json").write_text(
            json.dumps({"schema": 1, "suggestions": proposals or [self.proposal]}),
            encoding="utf-8",
        )

    def prompt(self, prompt="Find the auth handler", turn="t1", session="s1", **kw):
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": session,
            "prompt_id": turn,
            "cwd": str(self.project),
            "prompt": prompt,
            **kw,
        }
        return run_hook(payload, store=self.store, environ={}) or {}

    def offer(self, **kw):
        output = self.prompt(**kw)
        notice = output["systemMessage"]
        token = re.search(r"guidance ([0-9a-f]{16}) once", notice)[1]
        return token, output

    def choose(self, token, choice, **kw):
        return self.prompt(f"agent-efficiency guidance {token} {choice}", **kw)

    @staticmethod
    def context(output):
        return output.get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_notice_is_attributed_visible_nonblocking_and_has_no_active_tip(self):
        _, output = self.offer()
        notice = output["systemMessage"]
        for text in (
            "Agent Efficiency suggestion",
            "Not active",
            self.proposal["source"],
            self.proposal["reason"],
            "src/auth",
            "Savings have not been measured",
            "Use once",
            "Use for similar tasks",
            "Not now",
            "Don't suggest again",
        ):
            self.assertIn(text, notice)
        self.assertNotIn("continue", output)
        self.assertNotIn("src/auth", self.context(output))

    def test_silence_and_duplicate_hook_do_not_activate_or_repeat(self):
        self.offer()
        self.assertNotIn("systemMessage", self.prompt())
        output = self.prompt(turn="t2")
        self.assertNotIn("systemMessage", output)
        self.assertNotIn("src/auth", self.context(output))

    def test_once_applies_only_to_the_offered_request(self):
        token, _ = self.offer()
        result = self.choose(token, "once", turn="choice")
        self.assertIn("src/auth", self.context(result))
        self.assertIn("current request only", self.context(result))
        self.assertLessEqual(len(self.context(result)), 500)
        self.assertNotIn("src/auth", self.context(self.choose(token, "once")))
        self.assertNotIn("src/auth", self.context(self.prompt(turn="t2")))

    def test_choices_after_stop_cannot_restart_the_completed_request(self):
        token, _ = self.offer()
        self.store.finish_turn("s1", "t1")
        once = self.choose(token, "once")
        self.assertNotIn("src/auth", self.context(once))
        self.assertIn("ended", once["stopReason"])
        similar = self.choose(token, "similar")
        self.assertNotIn("src/auth", self.context(similar))
        self.assertIn("future", similar["stopReason"])
        self.assertIn("src/auth", self.context(self.prompt(turn="t2")))

    def test_similar_applies_only_to_matching_requests_in_same_project(self):
        token, _ = self.offer()
        self.choose(token, "similar")
        self.assertIn("src/auth", self.context(self.prompt(turn="t2")))
        self.assertNotIn("systemMessage", self.prompt(turn="t3"))
        self.assertNotIn(
            "src/auth", self.context(self.prompt("Find the billing handler", "t4"))
        )
        other = self.root / "other"
        (other / "src/auth").mkdir(parents=True)
        (other / "agent-efficiency-guidance.json").write_text(
            (self.project / "agent-efficiency-guidance.json").read_text()
        )
        output = self.prompt(turn="t1", session="other", cwd=str(other))
        self.assertIn("systemMessage", output)
        self.assertNotIn("src/auth", self.context(output))

    def test_editing_accepted_version_requires_a_new_choice(self):
        token, _ = self.offer()
        self.choose(token, "similar")
        self.proposal["reason"] = "A revised explanation from the project note."
        self.write()
        with patch(
            "agent_efficiency.guidance_state.time.time",
            return_value=time.time() + 86401,
        ):
            output = self.prompt(turn="t2")
        self.assertIn("systemMessage", output)
        self.assertNotIn("src/auth", self.context(output))

    def test_rejection_survives_cosmetic_edits_and_scope_changes(self):
        token, _ = self.offer()
        self.choose(token, "never")
        self.proposal.update(
            title="A new title", reason="A new explanation", terms=["handler"]
        )
        self.write()
        with patch(
            "agent_efficiency.guidance_state.time.time",
            return_value=time.time() + 86401,
        ):
            output = self.prompt(turn="t2")
        self.assertNotIn("systemMessage", output)
        self.assertNotIn("src/auth", self.context(output))

    def test_later_has_a_cooldown_and_never_applies(self):
        token, _ = self.offer()
        self.choose(token, "later")
        self.assertNotIn("systemMessage", self.prompt(turn="t2"))
        with patch(
            "agent_efficiency.guidance_state.time.time",
            return_value=time.time() + 86401,
        ):
            output = self.prompt(turn="t3")
        self.assertIn("systemMessage", output)
        self.assertNotIn("src/auth", self.context(output))

    def test_choice_rejects_wrong_session_old_request_or_changed_content(self):
        token, _ = self.offer()
        self.assertNotIn(
            "src/auth", self.context(self.choose(token, "similar", session="s2"))
        )
        self.proposal["reason"] = "Changed before choice."
        self.write()
        self.assertNotIn("src/auth", self.context(self.choose(token, "similar")))
        self.proposal["reason"] = (
            "The saved project note identifies this auth component."
        )
        self.write()
        self.prompt(turn="t2")
        self.assertNotIn("src/auth", self.context(self.choose(token, "once")))

    def test_reset_revokes_adoption_and_retains_receipts(self):
        token, _ = self.offer()
        self.choose(token, "similar")
        self.prompt("agent-efficiency guidance reset")
        self.assertNotIn("src/auth", self.context(self.prompt(turn="t2")))
        with self.store.connect() as conn:
            codes = {
                row[0] for row in conn.execute("SELECT code FROM guidance_events_v1")
            }
        self.assertTrue({"similar", "reset"} <= codes)

    def test_off_observe_cursor_and_tool_payloads_cannot_activate(self):
        for mode in ("off", "observe"):
            self.store.set_default_mode(mode)
            self.assertNotIn("systemMessage", self.prompt(session=mode))
        self.store.set_default_mode("advise")
        result = (
            run_hook(
                {
                    "hook_event_name": "beforeSubmitPrompt",
                    "conversation_id": "cursor",
                    "generation_id": "t1",
                    "workspace_roots": [str(self.project)],
                    "prompt": "Find the auth handler",
                },
                store=self.store,
            )
            or {}
        )
        self.assertNotIn("user_message", result)
        token, _ = self.offer()
        result = (
            run_hook(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": "s1",
                    "cwd": str(self.project),
                    "tool_name": "Bash",
                    "tool_input": {
                        "command": f"agent-efficiency guidance {token} similar"
                    },
                },
                store=self.store,
            )
            or {}
        )
        self.assertNotIn("src/auth", self.context(result))
        self.assertNotIn("src/auth", self.context(self.prompt(turn="t2")))

    def test_guidance_text_and_prompt_never_enter_database(self):
        token, _ = self.offer(prompt="Find the auth handler secret-marker-742")
        self.choose(token, "similar")
        with self.store.connect() as conn:
            dump = "\n".join(conn.iterdump())
        for private in (
            "src/auth",
            "secret-marker-742",
            self.proposal["source"],
            self.proposal["title"],
        ):
            self.assertNotIn(private, dump)

    def test_concurrent_delivery_and_choice_are_at_most_once(self):
        # Initialize the session before testing independent hook processes.
        self.prompt("Hello", "setup")
        with ThreadPoolExecutor(max_workers=4) as pool:
            outputs = list(pool.map(lambda _: self.prompt(turn="t1"), range(4)))
        notices = [out["systemMessage"] for out in outputs if "systemMessage" in out]
        self.assertEqual(len(notices), 1)
        token = re.search(r"guidance ([0-9a-f]{16}) once", notices[0])[1]
        with ThreadPoolExecutor(max_workers=4) as pool:
            outputs = list(pool.map(lambda _: self.choose(token, "once"), range(4)))
        self.assertEqual(sum("src/auth" in self.context(out) for out in outputs), 1)

    def test_invalid_or_expired_proposal_fails_open(self):
        for change in (
            {"expires": "2000-01-01"},
            {"target": "../outside"},
            {"approved": True},
        ):
            proposal = {**self.proposal, **change}
            self.write([proposal])
            self.assertNotIn("systemMessage", self.prompt(session=str(change)))

    def test_missing_file_preserves_existing_behavior_and_schema(self):
        (self.project / "agent-efficiency-guidance.json").unlink()
        self.assertNotIn("systemMessage", self.prompt())
        self.assertEqual(self.store.get_setting("schema_version"), "8")
        with self.store.connect() as conn:
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master")]
        self.assertNotIn("guidance_offers_v1", tables)

    def test_loader_checks_bounds_and_matches_whole_words(self):
        from agent_efficiency.guidance import load_candidates

        candidate = load_candidates(self.project, today=date(2026, 11, 7))[0]
        self.assertTrue(candidate.matches("Find AUTH handler"))
        self.assertFalse(candidate.matches("Find the author"))
        self.proposal["terms"] = []
        self.write()
        with self.assertRaises(ValueError):
            load_candidates(self.project)

    def test_codex_uses_the_nonblocking_notice_and_choice_path(self):
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "codex",
            "turn_id": "t1",
            "cwd": str(self.project),
            "prompt": "Find auth",
        }
        output = run_hook(payload, store=self.store, environ={"PLUGIN_ROOT": "/plugin"})
        self.assertIn("systemMessage", output)
        self.assertNotIn("continue", output)
        token = re.search(r"guidance ([0-9a-f]{16}) once", output["systemMessage"])[1]
        payload["prompt"] = f"agent-efficiency guidance {token} once"
        output = run_hook(payload, store=self.store, environ={"PLUGIN_ROOT": "/plugin"})
        self.assertIn("src/auth", self.context(output))

    def test_a_single_request_never_gets_two_proposals(self):
        (self.project / "src/other").mkdir()
        self.write([self.proposal, {**self.proposal, "target": "src/other"}])
        self.offer()
        self.assertNotIn("systemMessage", self.prompt())

    def test_late_prompt_event_cannot_attach_a_suggestion_to_a_newer_request(self):
        self.prompt("Hello", "t1")
        self.prompt("Hello", "t2")
        self.assertNotIn("systemMessage", self.prompt("Find auth", "t1"))

    def test_foreign_symlinks_duplicate_actions_and_oversize_files_are_rejected(self):
        from agent_efficiency.guidance import load_candidates

        (self.project / "escape").symlink_to(self.root, target_is_directory=True)
        self.write([{**self.proposal, "target": "escape"}])
        with self.assertRaises(ValueError):
            load_candidates(self.project)
        self.write([self.proposal, self.proposal])
        with self.assertRaises(ValueError):
            load_candidates(self.project)
        (self.project / "agent-efficiency-guidance.json").write_text(" " * 32769)
        with self.assertRaises(ValueError):
            load_candidates(self.project)

    def test_guidance_failure_preserves_operational_output(self):
        expected = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "Required verification guidance.",
            }
        }
        with (
            patch("agent_efficiency.guidance.on_prompt", side_effect=OSError),
            patch("agent_efficiency.hook._on_prompt", return_value=expected),
        ):
            output = self.prompt("Implement a substantial auth feature with tests")
        self.assertEqual(output, expected)

    def test_cli_validates_but_cannot_accept_a_proposal(self):
        from agent_efficiency.cli import main

        with redirect_stdout(io.StringIO()) as output:
            code = main(["guidance", "validate", "--cwd", str(self.project)])
        self.assertEqual(code, 0)
        self.assertIn("1 current suggestion", output.getvalue())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            main(["guidance", "accept"])
        self.assertEqual(result.exception.code, 2)

    def test_full_vault_allowance_prevents_tip_and_records_reason(self):
        token, _ = self.offer()
        self.store.record_vault_receipt(
            "s1",
            cause="new",
            vault_revision="a" * 16,
            payload_digest="b" * 16,
            notes_selected=1,
            head_chars=8900,
            chars_emitted=8900,
            chars_omitted=0,
            disposition="delivered",
            reason_code=None,
        )
        output = self.choose(token, "once")
        self.assertNotIn("src/auth", self.context(output))
        self.assertIn("not applied", output["stopReason"])
        with self.store.connect() as conn:
            codes = [
                row[0] for row in conn.execute("SELECT code FROM guidance_events_v1")
            ]
        self.assertIn("budget_exceeded", codes)
        self.assertNotIn("context_prepared", codes)

    def test_vault_reload_keeps_space_for_an_earlier_tip(self):
        from agent_efficiency.vault.prepare import stopped
        from agent_efficiency.vault_delivery import deliver_vault_context

        token, _ = self.offer()
        self.choose(token, "once")
        (self.project / "agent-efficiency-guidance.json").unlink()
        with patch(
            "agent_efficiency.vault_delivery.prepare",
            return_value=stopped("unavailable", "no_trees"),
        ) as prepare:
            deliver_vault_context(
                self.store,
                "s1",
                "claude",
                "SessionStart",
                self.project,
                cause="resume",
                mode="advise",
            )
        reserve = prepare.call_args.kwargs["reserve"]
        self.assertGreater(reserve, 0)
        self.assertLessEqual(reserve, 500)

    def test_inactive_or_invalid_suggestions_do_not_reduce_vault_budget(self):
        from agent_efficiency.vault.prepare import stopped
        from agent_efficiency.vault_delivery import deliver_vault_context

        for host, mode in (("claude", "observe"), ("cursor", "advise")):
            with patch(
                "agent_efficiency.vault_delivery.prepare",
                return_value=stopped("unavailable", "no_trees"),
            ) as prepare:
                deliver_vault_context(
                    self.store,
                    "s1",
                    host,
                    "SessionStart",
                    self.project,
                    cause="new",
                    mode=mode,
                )
            self.assertEqual(prepare.call_args.kwargs["reserve"], 0)
        (self.project / "agent-efficiency-guidance.json").write_text("invalid")
        with patch(
            "agent_efficiency.vault_delivery.prepare",
            return_value=stopped("unavailable", "no_trees"),
        ) as prepare:
            deliver_vault_context(
                self.store,
                "s1",
                "claude",
                "SessionStart",
                self.project,
                cause="new",
                mode="advise",
            )
        self.assertEqual(prepare.call_args.kwargs["reserve"], 0)

    def test_no_proposals_or_choices_during_an_enrolled_experiment(self):
        self.prompt("Hello", "setup")
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO experiment_enrollments (session_id, experiment_id, cohort, task_class, "
                "task_set_digest, agent_profile, blinded, enrolled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "s1",
                    "trial",
                    "advise",
                    "edit",
                    "sha256:" + "a" * 64,
                    "fixture",
                    0,
                    "2026-11-07",
                ),
            )
        self.assertNotIn("systemMessage", self.prompt())

    def test_user_can_revoke_while_mode_is_off(self):
        token, _ = self.offer()
        self.choose(token, "similar")
        self.prompt("agent-efficiency off")
        self.prompt("agent-efficiency guidance reset")
        self.prompt("agent-efficiency on")
        self.assertNotIn("src/auth", self.context(self.prompt(turn="t2")))

    def test_notice_effect_is_separate_from_blocking_control(self):
        effect = CanonicalEffect(
            effect="notice",
            policy_id="guidance.suggestion",
            policy_revision="1",
            observed_fact="scope_match",
            agent_message="",
            user_message="Agent Efficiency suggestion",
        )
        for host in ("claude", "codex"):
            self.assertEqual(
                adapter_for(host).render(effect, native_event="UserPromptSubmit"),
                {"systemMessage": "Agent Efficiency suggestion"},
            )
        self.assertIsNone(
            adapter_for("cursor").render(effect, native_event="beforeSubmitPrompt")
        )


if __name__ == "__main__":
    unittest.main()
