"""Environment identity is optional, bounded and never stores environment values."""

import os
from unittest.mock import patch
import unittest

import test_verification
from agent_efficiency.verification.config import load_project_config
from agent_efficiency.verification.runner import run_checks
from agent_efficiency.verification.state import workspace_state
from agent_efficiency.verification.receipts import receipt_state


class EnvironmentIdentityTests(unittest.TestCase):
    setUp = test_verification.VerificationTests.setUp

    def config(self):
        path = self.root / "agent-efficiency.toml"
        path.write_text(
            path.read_text()
            + '\nenv_allowlist = ["AE_TEST_ENV"]\nenv_fingerprint = ["AE_TEST_ENV"]\n'
        )
        return load_project_config(self.root)

    def test_environment_change_invalidates_receipt(self):
        from agent_efficiency.verification.environment import check_identity

        config = self.config()
        check = config.checks[0]
        with patch.dict(os.environ, {"AE_TEST_ENV": "first-private-value"}):
            (receipt,) = run_checks(config, [check], self.store)
            first = check_identity(check, self.store)
        with patch.dict(os.environ, {"AE_TEST_ENV": "second-private-value"}):
            second = check_identity(check, self.store)
        self.assertNotEqual(first, second)
        self.assertEqual(receipt["check_digest"], first)
        state = workspace_state(self.root, config.digest)
        self.assertEqual(
            receipt_state(receipt, check_digest=second, workspace_digest=state.digest),
            "stale",
        )
        for file in self.store.paths.root.iterdir():
            if file.is_file():
                self.assertNotIn(b"first-private-value", file.read_bytes())
                self.assertNotIn(b"second-private-value", file.read_bytes())

    def test_unset_and_empty_environment_are_distinct(self):
        from agent_efficiency.verification.environment import check_identity

        config = self.config()
        with patch.dict(os.environ, {}, clear=True):
            absent = check_identity(config.checks[0], self.store)
        with patch.dict(os.environ, {"AE_TEST_ENV": ""}, clear=True):
            empty = check_identity(config.checks[0], self.store)
        self.assertNotEqual(absent, empty)

    def test_unbound_check_retains_legacy_identity(self):
        from agent_efficiency.verification.environment import check_identity

        config = load_project_config(self.root)
        self.assertEqual(
            check_identity(config.checks[0], self.store), config.checks[0].digest
        )

    def test_fingerprinting_requires_explicit_environment_allowance(self):
        path = self.root / "agent-efficiency.toml"
        path.write_text(path.read_text() + '\nenv_fingerprint = ["SECRET"]\n')
        with self.assertRaises(ValueError):
            load_project_config(self.root)

    def test_invalid_private_key_is_inconclusive_not_a_public_fingerprint(self):
        from agent_efficiency.verification.environment import check_identity

        config = self.config()
        (self.store.paths.root / "digest.key").write_text("invalid")
        self.assertIsNone(check_identity(config.checks[0], self.store))
        (receipt,) = run_checks(config, config.checks, self.store)
        self.assertEqual(receipt["result"], "inconclusive")
        self.assertEqual(receipt["reason_code"], "environment_unknown")

    def test_environment_changed_while_command_ran_is_inconclusive(self):
        config = self.config()
        with patch(
            "agent_efficiency.verification.runner.check_identity",
            side_effect=["before", "after"],
        ):
            (receipt,) = run_checks(config, config.checks, self.store)
        self.assertEqual(receipt["result"], "inconclusive")
        self.assertEqual(receipt["reason_code"], "environment_changed")
