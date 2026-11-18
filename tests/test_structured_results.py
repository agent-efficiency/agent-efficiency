"""Explicit result summaries classify failures without retaining raw test output."""

import json
import sys
import unittest

import test_verification


class StructuredResultTests(unittest.TestCase):
    setUp = test_verification.VerificationTests.setUp

    def report(self, **changes):
        value = {
            "version": 1,
            "planned": 10,
            "passed": 10,
            "failed": 0,
            "baseline_failed": 0,
            "setup_errors": 0,
            "survived": 0,
            "timed_out": 0,
            "not_run": 0,
        }
        value.update(changes)
        return value

    def classify(self, **changes):
        from agent_efficiency.verification.results import classify_summary

        return classify_summary(self.report(**changes))

    def test_only_full_success_is_pass(self):
        self.assertEqual(self.classify(), ("pass", None))

    def test_nonzero_mutation_and_setup_failures_are_distinct(self):
        for field, result, reason in [
            ("failed", "fail", "structured_failed"),
            ("baseline_failed", "inconclusive", "structured_baseline_failed"),
            ("setup_errors", "inconclusive", "structured_setup_error"),
            ("survived", "fail", "structured_survived"),
            ("timed_out", "blocked", "structured_timeout"),
            ("not_run", "inconclusive", "structured_incomplete"),
        ]:
            with self.subTest(field=field):
                self.assertEqual(
                    self.classify(passed=9, **{field: 1}), (result, reason)
                )

    def test_unknown_fields_and_invalid_counts_are_refused(self):
        from agent_efficiency.verification.results import classify_summary

        for changes in (
            {"output": "private text"},
            {"passed": True},
            {"passed": -1},
            {"planned": 0, "passed": 0},
            {"planned": 11},
            {"version": 2},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                classify_summary(self.report(**changes))

    def test_runner_persists_only_closed_counts_and_respects_nonzero_exit(self):
        from agent_efficiency.verification.config import load_project_config
        from agent_efficiency.verification.runner import run_checks

        (self.root / ".gitignore").write_text(".result.json\n")
        for changes, code, result, reason in [
            ({}, 0, "pass", None),
            ({}, 1, "fail", "exit_nonzero"),
            ({"passed": 9, "survived": 1}, 0, "fail", "structured_survived"),
            (
                {"passed": 9, "setup_errors": 1},
                1,
                "inconclusive",
                "structured_setup_error",
            ),
        ]:
            with self.subTest(changes=changes, code=code):
                summary = self.report(**changes)
                command = [
                    sys.executable,
                    "-c",
                    'from pathlib import Path; Path(".result.json").write_text('
                    + repr(json.dumps(summary))
                    + "); raise SystemExit("
                    + str(code)
                    + ")",
                ]
                (self.root / "agent-efficiency.toml").write_text(
                    'version = 1\n[[checks]]\nid = "unit"\nresult_file = ".result.json"\ncommand = '
                    + json.dumps(command)
                    + "\n"
                )
                config = load_project_config(self.root)
                (receipt,) = run_checks(config, config.checks, self.store)
                self.assertEqual(receipt["result"], result)
                self.assertEqual(receipt["reason_code"], reason)
                saved = self.store.verification_receipt(receipt["receipt_id"])
                self.assertEqual(saved["summary"], summary)

    def test_stale_result_file_cannot_reuse_old_pass(self):
        from agent_efficiency.verification.results import file_signature, read_summary

        path = self.root / "result.json"
        path.write_text(json.dumps(self.report()))
        before = file_signature(path)
        with self.assertRaises(ValueError):
            read_summary(path, before)
        path.unlink()
        path.write_text(json.dumps(self.report(passed=9, survived=1)))
        self.assertEqual(read_summary(path, before)["survived"], 1)
