"""A project-owned comparison proves only its own declared, workspace-bound inputs."""

import json
import sys
import unittest

import test_verification
from agent_efficiency.store import project_identity
from agent_efficiency.verification.config import load_project_config
from agent_efficiency.verification.runner import run_checks


class RetainedArtifactCheckTests(unittest.TestCase):
    setUp = test_verification.VerificationTests.setUp

    def test_reuse_is_bound_to_artifact_and_cannot_approve_another_gate(self):
        artifact = self.root / "retained-reference.json"
        artifact.write_text('{"accepted":1}\n')
        command = [
            sys.executable,
            "-c",
            'from pathlib import Path; raise SystemExit(0 if Path("retained-reference.json").read_text() == \'{"accepted":1}\\n\' else 1)',
        ]
        path = self.root / "agent-efficiency.toml"
        path.write_text(
            path.read_text()
            + '\n[[checks]]\nid = "compare-retained"\ncommand = '
            + json.dumps(command)
            + "\n"
        )
        config = load_project_config(self.root)
        comparison = config.get("compare-retained")
        (first,) = run_checks(config, [comparison], self.store)
        self.assertEqual(first["result"], "pass")
        (reused,) = run_checks(config, [comparison], self.store, reuse_current=True)
        self.assertTrue(reused["reused"])
        self.assertEqual(first["receipt_id"], reused["receipt_id"])
        key, _ = project_identity(str(self.root))
        self.assertEqual(self.store.verification_receipts(key, check_id="unit"), [])
        artifact.write_text('{"accepted":0}\n')
        (changed,) = run_checks(config, [comparison], self.store, reuse_current=True)
        self.assertEqual(changed["result"], "fail")
        self.assertNotEqual(changed["receipt_id"], first["receipt_id"])
        self.assertEqual(
            self.store.verification_receipt(first["receipt_id"])["result"], "pass"
        )
