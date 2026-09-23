from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_efficiency.cli import main
from agent_efficiency.experiments import (
    ExperimentError,
    build_experiment_evaluation,
    enroll_session,
    experiment_session_status,
    get_enrollment,
    invalidate_enrollment,
    local_capability_measurement,
    record_card_feedback,
    record_outcome,
    guidance_expansion_gate,
)
from agent_efficiency.hook import run_hook
from agent_efficiency.report import build_report
from agent_efficiency.store import Store


CLAUDE_ENV = {"CLAUDE_PLUGIN_ROOT": "/plugin"}
DIGEST = "sha256:" + "0" * 64
PROMPT = (
    "Review the exact implementation diff against the security specification "
    "and report evidence-backed findings with validation."
)


def evidence_digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


TASK_SET_DIGEST = evidence_digest("first-cards-task-set")


class ExperimentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)

    def _session(self, session_id: str, *, cohort: str = "observe") -> None:
        self.store.ensure_session(
            session_id,
            host="claude",
            cwd="/work/matched-project",
            model="claude-sonnet",
        )
        enroll_session(
            self.store,
            session_id,
            experiment_id="team-pilot",
            cohort=cohort,
            task_class="security-review",
            task_set_digest=TASK_SET_DIGEST,
            agent_profile="sonnet-high-default-tools",
            blinded=True,
        )

    def test_observe_runs_same_selector_without_injecting_context(self) -> None:
        self._session("observe-1")
        output = run_hook(
            {
                "session_id": "observe-1",
                "hook_event_name": "UserPromptSubmit",
                "prompt_id": "turn-1",
                "cwd": "/work/matched-project",
                "prompt": PROMPT,
            },
            store=self.store,
            environ=CLAUDE_ENV,
        )
        self.assertIsNone(output)
        receipt = self.store.latest_knowledge_receipt("observe-1", "core.narrow-review")
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(receipt["disposition"], "observed")
        summary = self.store.knowledge_receipt_summary("observe-1")
        self.assertEqual(summary["observed"], 1)
        self.assertEqual(summary["emitted"], 0)
        status = experiment_session_status(self.store, "observe-1")
        self.assertEqual(
            [card["card_id"] for card in status["rateable_cards"]],
            ["core.narrow-review"],
        )
        measurement = local_capability_measurement(self.store)
        self.assertEqual(measurement["runtime"]["selection"]["samples"], 1)
        self.assertEqual(measurement["runtime"]["selection"]["context_chars"], 0)

    def test_enrollment_after_the_first_task_turn_is_rejected(self) -> None:
        self.store.ensure_session(
            "late-session",
            host="claude",
            cwd="/work/matched-project",
            model="claude-sonnet",
        )
        self.store.start_turn(
            "late-session",
            "turn-1",
            prompt_chars=100,
            task_type="review",
            risk="normal",
        )
        with self.assertRaisesRegex(ExperimentError, "before"):
            enroll_session(
                self.store,
                "late-session",
                experiment_id="team-pilot",
                cohort="observe",
                task_class="security-review",
                task_set_digest=TASK_SET_DIGEST,
                agent_profile="sonnet-high-default-tools",
                blinded=True,
            )

    def test_manual_invalidation_is_append_only_and_idempotent(self) -> None:
        self._session("invalidated-1")
        first = invalidate_enrollment(
            self.store,
            "invalidated-1",
            reason="evidence-withdrawn",
        )
        self.assertEqual(first["invalidation_reason"], "evidence-withdrawn")
        same = invalidate_enrollment(
            self.store,
            "invalidated-1",
            reason="evidence-withdrawn",
        )
        self.assertEqual(same["invalidated_at"], first["invalidated_at"])
        with self.assertRaisesRegex(ExperimentError, "immutable"):
            invalidate_enrollment(
                self.store,
                "invalidated-1",
                reason="data-entry-error",
            )

    def test_enrollment_and_card_judgment_are_immutable(self) -> None:
        self._session("immutable-1", cohort="advise")
        run_hook(
            {
                "session_id": "immutable-1",
                "hook_event_name": "UserPromptSubmit",
                "prompt_id": "turn-1",
                "cwd": "/work/matched-project",
                "prompt": PROMPT,
            },
            store=self.store,
            environ=CLAUDE_ENV,
        )
        run_hook(
            {
                "session_id": "immutable-1",
                "hook_event_name": "UserPromptSubmit",
                "prompt_id": "turn-2",
                "cwd": "/work/matched-project",
                "prompt": PROMPT,
            },
            store=self.store,
            environ=CLAUDE_ENV,
        )
        result = record_card_feedback(
            self.store,
            "immutable-1",
            "core.narrow-review",
            rating="useful",
            changed_next_action=True,
        )
        self.assertEqual(result["rating"], "useful")
        same = enroll_session(
            self.store,
            "immutable-1",
            experiment_id="team-pilot",
            cohort="advise",
            task_class="security-review",
            task_set_digest=TASK_SET_DIGEST,
            agent_profile="sonnet-high-default-tools",
            blinded=True,
        )
        self.assertIsNone(same["invalidated_at"])
        with self.assertRaisesRegex(ExperimentError, "immutable"):
            record_card_feedback(
                self.store,
                "immutable-1",
                "core.narrow-review",
                rating="neutral",
                changed_next_action=False,
            )
        with self.assertRaisesRegex(ExperimentError, "fresh session"):
            enroll_session(
                self.store,
                "immutable-1",
                experiment_id="team-pilot",
                cohort="observe",
                task_class="security-review",
                task_set_digest=TASK_SET_DIGEST,
                agent_profile="sonnet-high-default-tools",
                blinded=True,
            )
        self.assertTrue(self.store.set_mode("immutable-1", "observe"))
        invalidated = get_enrollment(self.store, "immutable-1")
        assert invalidated is not None
        self.assertEqual(invalidated["invalidation_reason"], "session-mode-changed")
        record_outcome(
            self.store,
            "immutable-1",
            accepted=True,
            acceptance_evidence="test",
            acceptance_evidence_digest=evidence_digest("immutable-1"),
            wall_time_minutes=10,
        )
        evaluation = build_experiment_evaluation(
            self.store, experiment_id="team-pilot"
        )
        advise = evaluation["strata"][0]["cohorts"]["advise"]
        self.assertEqual(advise["invalidated"], 1)
        self.assertEqual(advise["verified_accepted"], 0)
        with self.assertRaisesRegex(ExperimentError, "receipt"):
            record_card_feedback(
                self.store,
                "immutable-1",
                "core.work-packet",
                rating="useful",
                changed_next_action=True,
            )

    def test_unverified_outcome_is_recorded_but_never_comparable(self) -> None:
        self._session("unverified-1")
        with self.assertRaisesRegex(ExperimentError, "sha256"):
            record_outcome(
                self.store,
                "unverified-1",
                accepted=True,
                acceptance_evidence="test",
                acceptance_evidence_digest=None,
                wall_time_minutes=20,
            )
        outcome = record_outcome(
            self.store,
            "unverified-1",
            accepted=True,
            acceptance_evidence="none",
            acceptance_evidence_digest=None,
            wall_time_minutes=20,
        )
        self.assertEqual(outcome["accepted"], 1)
        with self.assertRaisesRegex(ExperimentError, "immutable"):
            record_outcome(
                self.store,
                "unverified-1",
                accepted=True,
                acceptance_evidence="test",
                acceptance_evidence_digest=evidence_digest("unverified-1"),
                wall_time_minutes=20,
            )
        evaluation = build_experiment_evaluation(
            self.store, experiment_id="team-pilot"
        )
        self.assertFalse(evaluation["cost_claim_gate"]["eligible"])
        cohort = evaluation["strata"][0]["cohorts"]["observe"]
        self.assertEqual(cohort["verified_accepted"], 0)
        self.assertEqual(cohort["excluded_from_comparison"], 1)

    def test_different_agent_profiles_never_share_a_matched_stratum(self) -> None:
        for cohort, profile in (
            ("observe", "sonnet-high-default-tools"),
            ("advise", "sonnet-medium-default-tools"),
        ):
            session_id = f"profile-{cohort}"
            self.store.ensure_session(
                session_id,
                host="claude",
                cwd="/work/matched-project",
                model="claude-sonnet",
            )
            enroll_session(
                self.store,
                session_id,
                experiment_id="profile-check",
                cohort=cohort,
                task_class="security-review",
                task_set_digest=TASK_SET_DIGEST,
                agent_profile=profile,
                blinded=True,
            )
            self.store.record_status_sample(session_id, {"cost_usd": 1.0})
            record_outcome(
                self.store,
                session_id,
                accepted=True,
                acceptance_evidence="test",
                acceptance_evidence_digest=evidence_digest(session_id),
                wall_time_minutes=10,
            )
        evaluation = build_experiment_evaluation(
            self.store, experiment_id="profile-check"
        )
        self.assertEqual(evaluation["matched_strata"], 2)
        self.assertTrue(all(not row["comparable"] for row in evaluation["strata"]))
        self.assertEqual(
            {row["match"]["agent_profile"] for row in evaluation["strata"]},
            {
                "sonnet-high-default-tools",
                "sonnet-medium-default-tools",
            },
        )

    def test_reused_acceptance_evidence_never_opens_the_cost_claim_gate(
        self,
    ) -> None:
        shared_digest = evidence_digest("shared-evidence")
        for cohort in ("observe", "advise"):
            for index in range(5):
                session_id = f"reused-{cohort}-{index}"
                self.store.ensure_session(
                    session_id,
                    host="claude",
                    cwd="/work/matched-project",
                    model="claude-sonnet",
                )
                enroll_session(
                    self.store,
                    session_id,
                    experiment_id="reused-proof",
                    cohort=cohort,
                    task_class="security-review",
                    task_set_digest=TASK_SET_DIGEST,
                    agent_profile="sonnet-high-default-tools",
                    blinded=True,
                )
                self.store.record_status_sample(session_id, {"cost_usd": 1.0})
                record_outcome(
                    self.store,
                    session_id,
                    accepted=True,
                    acceptance_evidence="test",
                    acceptance_evidence_digest=shared_digest,
                    wall_time_minutes=10,
                )
        evaluation = build_experiment_evaluation(
            self.store, experiment_id="reused-proof"
        )
        stratum = evaluation["strata"][0]
        self.assertFalse(stratum["comparable"])
        self.assertFalse(stratum["evidence_digests_unique"])
        self.assertIn("reused", stratum["cost_claim_reason"])
        self.assertFalse(evaluation["cost_claim_gate"]["eligible"])

    def test_unblinded_feedback_cannot_advance_guidance_expansion(self) -> None:
        session_id = "unblinded-1"
        self.store.ensure_session(
            session_id,
            host="claude",
            cwd="/work/matched-project",
            model="claude-sonnet",
        )
        enroll_session(
            self.store,
            session_id,
            experiment_id="live-feedback",
            cohort="observe",
            task_class="security-review",
            task_set_digest=TASK_SET_DIGEST,
            agent_profile="sonnet-high-default-tools",
            blinded=False,
        )
        self.store.record_knowledge_observation(
            session_id,
            "turn-1",
            event_name="UserPromptSubmit",
            pack_id="test-pack",
            pack_digest=DIGEST,
            card_id="core.narrow-review",
            score=10,
            match_reason="matched experiment fixture",
            prompt_chars=300,
            task_type="review",
            risk="high",
        )
        record_card_feedback(
            self.store,
            session_id,
            "core.narrow-review",
            rating="useful",
            changed_next_action=True,
        )
        gate = guidance_expansion_gate(self.store)
        self.assertEqual(gate["ratings"], 0)
        self.assertFalse(gate["eligible"])

    def test_matched_evaluation_includes_overhead_and_opens_only_evidence_gates(
        self,
    ) -> None:
        ratings = ["useful"] * 16 + ["neutral"] * 3 + ["distracting"]
        rating_index = 0
        for cohort in ("observe", "advise"):
            for index in range(5):
                session_id = f"{cohort}-{index}"
                self._session(session_id, cohort=cohort)
                self.store.record_status_sample(
                    session_id,
                    {
                        "cost_usd": (10 + index)
                        if cohort == "observe"
                        else (8 + index),
                        "input_tokens": 1000,
                        "output_tokens": 100,
                    },
                )
                for card_index, card_id in enumerate(
                    ("core.work-packet", "core.narrow-review")
                ):
                    turn_key = f"turn-{card_index}"
                    self.store.start_turn(
                        session_id,
                        turn_key,
                        prompt_chars=300,
                        task_type="review",
                        risk="high",
                    )
                    if cohort == "observe":
                        self.store.record_knowledge_observation(
                            session_id,
                            turn_key,
                            event_name="UserPromptSubmit",
                            pack_id="test-pack",
                            pack_digest=DIGEST,
                            card_id=card_id,
                            score=10,
                            match_reason="matched experiment fixture",
                            prompt_chars=300,
                            task_type="review",
                            risk="high",
                        )
                    else:
                        emitted, reason = self.store.record_knowledge_advisory(
                            session_id,
                            turn_key,
                            event_name="UserPromptSubmit",
                            pack_id="test-pack",
                            pack_digest=DIGEST,
                            card_id=card_id,
                            score=10,
                            match_reason="matched experiment fixture",
                            emitted_chars=100,
                        )
                        self.assertTrue(emitted, reason)
                    self.store.record_capability_runtime(
                        session_id,
                        turn_key,
                        event_name="UserPromptSubmit",
                        operation="selection",
                        duration_us=1000 + index,
                        context_chars=100 if cohort == "advise" else 0,
                    )
                    record_card_feedback(
                        self.store,
                        session_id,
                        card_id,
                        rating=ratings[rating_index],
                        changed_next_action=ratings[rating_index] == "useful",
                    )
                    rating_index += 1
                record_outcome(
                    self.store,
                    session_id,
                    accepted=True,
                    acceptance_evidence="test",
                    acceptance_evidence_digest=evidence_digest(session_id),
                    wall_time_minutes=60 if cohort == "observe" else 50,
                    correction_turns=2 if cohort == "observe" else 1,
                    human_review_minutes=10,
                    escaped_defects=0,
                )

        evaluation = build_experiment_evaluation(
            self.store, experiment_id="team-pilot"
        )
        self.assertTrue(evaluation["cost_claim_gate"]["eligible"])
        self.assertEqual(evaluation["cost_claim_gate"]["eligible_strata"], 1)
        stratum = evaluation["strata"][0]
        self.assertTrue(stratum["comparable"])
        self.assertTrue(stratum["evidence_digests_unique"])
        self.assertTrue(stratum["cost_claim_eligible"])
        differences = stratum["observed_difference"]["median_component_differences"]
        self.assertEqual(differences["cohort_host_cost_usd_per_accepted"], -2.0)
        self.assertEqual(differences["cohort_wall_time_minutes_per_accepted"], -10.0)
        self.assertGreater(differences["cohort_guidance_chars_per_accepted"], 0)
        self.assertTrue(evaluation["guidance_expansion_gate"]["eligible"])
        self.assertEqual(evaluation["guidance_expansion_gate"]["ratings"], 20)
        report = build_report(self.store)
        self.assertIn("capability_layer", report)
        self.assertEqual(
            report["capability_layer"]["runtime"]["selection"]["samples"], 20
        )
        self.assertTrue(
            report["capability_layer"]["runtime"]["selection"]["within_p95_gate"]
        )
        self.assertEqual(evaluation["pilot"]["status"], "insufficient_data")
        self.assertEqual(evaluation["pilot"]["matched_pairs"], 5)
        self.store.ensure_session(
            "advise-missing-outcome",
            host="claude",
            cwd="/work/matched-project",
            model="claude-sonnet",
        )
        enroll_session(
            self.store,
            "advise-missing-outcome",
            experiment_id="team-pilot",
            cohort="advise",
            task_class="security-review",
            task_set_digest=TASK_SET_DIGEST,
            agent_profile="sonnet-high-default-tools",
            blinded=True,
        )
        self.store.record_status_sample("advise-missing-outcome", {"cost_usd": 1.0})
        incomplete = build_experiment_evaluation(
            self.store, experiment_id="team-pilot"
        )
        self.assertFalse(incomplete["cost_claim_gate"]["eligible"])
        self.assertFalse(incomplete["strata"][0]["outcomes_complete"])
        self.assertIn("lack an outcome", incomplete["strata"][0]["cost_claim_reason"])

    def test_pilot_gate_passes_with_24_rated_cross_host_tasks(self) -> None:
        hosts = ("claude", "cursor", "codex")
        for host in hosts:
            for cohort in ("observe", "advise"):
                for index in range(5):
                    session_id = f"pilot-{host}-{cohort}-{index}"
                    self.store.ensure_session(
                        session_id,
                        host=host,
                        cwd="/work/matched-project",
                        model="matched-model",
                    )
                    enroll_session(
                        self.store,
                        session_id,
                        experiment_id="cross-host-pilot",
                        cohort=cohort,
                        task_class="implementation",
                        task_set_digest=TASK_SET_DIGEST,
                        agent_profile="matched-profile",
                        blinded=True,
                    )
                    with self.store.connect() as conn:
                        conn.execute(
                            "UPDATE sessions SET tool_count = ? WHERE session_id = ?",
                            (20 if cohort == "observe" else 18, session_id),
                        )
                    for operation in ("selection", "session-start"):
                        self.store.record_capability_runtime(
                            session_id,
                            "turn-1",
                            event_name="UserPromptSubmit",
                            operation=operation,
                            duration_us=1_000,
                        )
                    for operation in ("normal-hook", "session-start"):
                        self.store.record_runtime_health(
                            session_id,
                            "turn-1",
                            event_name="UserPromptSubmit",
                            operation=operation,
                            duration_us=1_000,
                            outcome="success",
                        )
                    if cohort == "advise":
                        intervention_id = self.store.record_intervention(
                            session_id,
                            "turn-1",
                            policy_id="core.verify-change",
                            policy_revision="1",
                            triggering_fact="unverified_change",
                            requested_effect="add_context",
                            rendered_effect="add_context",
                            host_capability="add_context",
                            disposition="emitted",
                            message_chars=20,
                        )
                        self.store.record_intervention_feedback(
                            intervention_id, "useful"
                        )
                    record_outcome(
                        self.store,
                        session_id,
                        accepted=True,
                        acceptance_evidence="test",
                        acceptance_evidence_digest=evidence_digest(session_id),
                        completion_evidence_state=(
                            "missing" if cohort == "observe" else "current"
                        ),
                        wall_time_minutes=10,
                    )

        evaluation = build_experiment_evaluation(self.store, experiment_id="cross-host-pilot")
        pilot = evaluation["pilot"]
        self.assertEqual(pilot["status"], "pass")
        self.assertEqual(pilot["matched_tasks"], 30)
        self.assertEqual(pilot["matched_pairs"], 15)
        self.assertEqual(set(pilot["hosts"]), set(hosts))
        self.assertTrue(all(pilot["criteria"].values()))
        self.assertFalse(pilot["stop_guidance_expansion"])

    def test_schema_two_migrates_without_losing_sessions(self) -> None:
        self.store.ensure_session(
            "existing-session",
            host="claude",
            cwd="/work/matched-project",
            model="claude-sonnet",
        )
        database = Path(self.temp.name) / "agent-efficiency.db"
        with contextlib.closing(sqlite3.connect(database)) as conn:
            conn.execute("DROP TABLE capability_runtime_samples")
            conn.execute("DROP TABLE capability_feedback")
            conn.execute("DROP TABLE session_outcomes")
            conn.execute("DROP TABLE experiment_enrollments")
            conn.execute("UPDATE settings SET value = '2' WHERE key = 'schema_version'")
            conn.commit()
        migrated = Store(self.temp.name)
        self.assertIsNone(get_enrollment(migrated, "existing-session"))
        self.assertIsNotNone(migrated.get_session("existing-session"))
        self.assertEqual(migrated.get_setting("schema_version"), "7")

    def test_cli_enrollment_outcome_and_evaluation_are_machine_readable(self) -> None:
        self.store.ensure_session(
            "cli-session",
            host="claude",
            cwd="/work/matched-project",
            model="claude-sonnet",
        )
        with contextlib.redirect_stdout(io.StringIO()) as enrollment_output:
            self.assertEqual(
                main(
                    [
                        "--data-dir",
                        self.temp.name,
                        "experiment",
                        "enroll",
                        "--id",
                        "team-pilot",
                        "--cohort",
                        "observe",
                        "--task-class",
                        "security-review",
                        "--task-set-digest",
                        TASK_SET_DIGEST,
                        "--profile",
                        "sonnet-high-default-tools",
                        "--blinded",
                        "--session",
                        "cli-session",
                        "--json",
                    ]
                ),
                0,
            )
        self.assertTrue(json.loads(enrollment_output.getvalue())["ok"])
        with contextlib.redirect_stdout(io.StringIO()) as outcome_output:
            self.assertEqual(
                main(
                    [
                        "--data-dir",
                        self.temp.name,
                        "experiment",
                        "outcome",
                        "--session",
                        "cli-session",
                        "--accepted",
                        "--evidence",
                        "review",
                        "--evidence-digest",
                        DIGEST,
                        "--wall-minutes",
                        "15",
                        "--json",
                    ]
                ),
                0,
            )
        self.assertEqual(
            json.loads(outcome_output.getvalue())["acceptance_evidence"],
            "review",
        )
        with contextlib.redirect_stdout(io.StringIO()) as evaluation_output:
            self.assertEqual(
                main(
                    [
                        "--data-dir",
                        self.temp.name,
                        "experiment",
                        "evaluate",
                        "--id",
                        "team-pilot",
                        "--json",
                    ]
                ),
                0,
            )
        self.assertFalse(
            json.loads(evaluation_output.getvalue())["cost_claim_gate"]["eligible"]
        )


if __name__ == "__main__":
    unittest.main()
