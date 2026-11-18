"""Explicit milestone continuity and metadata-only rework reports."""

import unittest

import test_verification
from agent_efficiency.verification.config import load_project_config
from agent_efficiency.verification.runner import run_checks


class WorkflowReportingTests(unittest.TestCase):
    setUp = test_verification.VerificationTests.setUp

    def test_milestone_is_ready_only_with_current_evidence(self):
        from agent_efficiency.verification.workflow import workflow_status

        path = self.root / "agent-efficiency.toml"
        path.write_text(
            path.read_text()
            + '\n[[milestones]]\nid = "validation"\nchecks = ["unit"]\n'
        )
        config = load_project_config(self.root)
        self.assertEqual(
            workflow_status(config, self.store)["milestones"][0]["state"], "pending"
        )
        run_checks(config, config.checks, self.store)
        self.assertEqual(
            workflow_status(config, self.store)["milestones"][0]["state"], "ready"
        )
        (self.root / "sample.py").write_text("changed = True\n")
        self.assertEqual(
            workflow_status(config, self.store)["milestones"][0]["state"], "pending"
        )

    def test_unknown_milestone_checks_are_rejected(self):
        path = self.root / "agent-efficiency.toml"
        path.write_text(
            path.read_text()
            + '\n[[milestones]]\nid = "validation"\nchecks = ["absent"]\n'
        )
        with self.assertRaises(ValueError):
            load_project_config(self.root)

    def test_no_milestones_is_unavailable_not_release_ready(self):
        from agent_efficiency.verification.workflow import workflow_status

        self.assertEqual(
            workflow_status(load_project_config(self.root), self.store)["plan"],
            "unavailable",
        )

    def test_annotations_reject_work_content_and_separate_cost_bases(self):
        from agent_efficiency.verification.workflow import (
            record_rework,
            workflow_report,
        )

        record_rework(
            self.store,
            self.root,
            kind="retry",
            cause="missing_prerequisite",
            duration_ms=100,
            cost_usd=0.1,
            cost_basis="estimate",
        )
        record_rework(
            self.store,
            self.root,
            kind="correction",
            cause="plan_order",
            duration_ms=0,
            cost_usd=0.2,
            cost_basis="reported",
        )
        with self.assertRaises(ValueError):
            record_rework(
                self.store, self.root, kind="retry", cause="private-command-output"
            )
        report = workflow_report(self.store, self.root)
        self.assertEqual(report["rework"]["events"], 2)
        self.assertEqual(report["rework"]["cost_estimates_usd"], 0.1)
        self.assertEqual(report["rework"]["reported_cost_usd"], 0.2)
        self.assertFalse(report["savings_claim_supported"])
        self.assertIsNone(report["coverage"]["external_background_results"])

    def test_opt_in_reuse_preserves_receipt_and_reruns_after_changes(self):
        from agent_efficiency.store import project_identity

        config = load_project_config(self.root)
        (first,) = run_checks(config, config.checks, self.store)
        (reused,) = run_checks(config, config.checks, self.store, reuse_current=True)
        self.assertTrue(reused["reused"])
        self.assertEqual(first["receipt_id"], reused["receipt_id"])
        (self.root / "sample.py").write_text("changed = True\n")
        (fresh,) = run_checks(config, config.checks, self.store, reuse_current=True)
        self.assertNotEqual(fresh["receipt_id"], first["receipt_id"])
        key, _ = project_identity(str(self.root))
        self.assertEqual(len(self.store.verification_receipts(key)), 2)

    def test_explanations_never_change_or_upgrade_evidence(self):
        from agent_efficiency.verification.receipts import explain_receipt

        args = {"check_digest": "check", "workspace_digest": "workspace"}
        self.assertEqual(explain_receipt(None, **args)["reason"], "missing_receipt")
        passed = {
            "result": "pass",
            "check_digest": "check",
            "workspace_digest": "workspace",
        }
        self.assertTrue(explain_receipt(passed, **args)["reusable"])
        self.assertEqual(
            explain_receipt(passed, **{**args, "workspace_digest": "new"})["reason"],
            "workspace_changed",
        )
        self.assertFalse(
            explain_receipt({**passed, "result": "fail"}, **args)["reusable"]
        )
