"""Rated rework stays supplemental to matched-cohort accounting, never additive."""

import unittest

import test_verification
from agent_efficiency.experiments import build_experiment_evaluation, enroll_session
from agent_efficiency.verification.workflow import record_rework, workflow_report


class WorkflowExperimentTests(unittest.TestCase):
    setUp = test_verification.VerificationTests.setUp

    def test_rated_overhead_of_unaccepted_work_remains_in_enrolled_cohort(self):
        self.store.ensure_session(
            "fixture", host="pi", cwd=str(self.root), model="fixture-model"
        )
        enroll_session(
            self.store,
            "fixture",
            experiment_id="field",
            cohort="observe",
            task_class="verification",
            task_set_digest="sha256:" + "a" * 64,
            agent_profile="fixture",
            blinded=False,
        )
        record_rework(
            self.store,
            self.root,
            kind="correction",
            cause="plan_order",
            session_id="fixture",
            phase="tooling",
            rating="useful",
            cost_usd=0.25,
            cost_basis="estimate",
        )
        report = workflow_report(self.store, self.root)
        self.assertEqual(report["rework"]["ratings"], {"useful": 1})
        self.assertEqual(report["rework"]["phases"], {"tooling": 1})
        evaluation = build_experiment_evaluation(self.store, experiment_id="field")
        cohort = evaluation["strata"][0]["cohorts"]["observe"]
        self.assertEqual(cohort["verified_accepted"], 0)
        self.assertIsNone(cohort["metrics_per_verified_accepted_task"])
        annotations = cohort["workflow_annotations"]
        self.assertEqual(annotations["events"], 1)
        self.assertEqual(annotations["cost_estimates_usd"], 0.25)
        self.assertFalse(annotations["added_to_comparison_cost"])
        self.assertIsNone(annotations["already_included_in_host_cost"])

    def test_session_binding_cannot_cross_projects_or_invent_sessions(self):
        self.store.ensure_session(
            "elsewhere", host="pi", cwd="/another/project", model=None
        )
        for session_id in ("elsewhere", "missing"):
            with self.subTest(session_id=session_id), self.assertRaises(ValueError):
                record_rework(
                    self.store,
                    self.root,
                    kind="retry",
                    cause="unknown",
                    session_id=session_id,
                )

    def test_arbitrary_rating_or_phase_text_is_rejected(self):
        for options in (
            {"rating": "private correction text"},
            {"phase": "private command text"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                record_rework(
                    self.store, self.root, kind="correction", cause="unknown", **options
                )
