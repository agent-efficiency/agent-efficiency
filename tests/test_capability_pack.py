from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_efficiency.capability_pack import (
    BUNDLED_CAPABILITY_PACK,
    adapt_capability_pack,
    capability_pack_bytes,
    capability_pack_digest,
    capability_pack_policies,
    load_bundled_capability_pack,
    load_capability_pack,
    validate_capability_pack_document,
)
from agent_efficiency.capability_validation import (
    ContractViolation,
    canonical_sha256,
)
from agent_efficiency.paths import PLUGIN_ROOT
from agent_efficiency.policy import PolicyPack


REPO_ROOT = PLUGIN_ROOT.parents[1]


class CapabilityPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = load_bundled_capability_pack()

    def test_bundled_pack_is_canonical_and_digest_bound(self) -> None:
        self.assertEqual(
            BUNDLED_CAPABILITY_PACK.read_bytes(),
            capability_pack_bytes(self.pack),
        )
        self.assertEqual(capability_pack_digest(self.pack), canonical_sha256(self.pack))

    def test_pack_contains_only_project_owned_reviewed_guidance(self) -> None:
        self.assertEqual(
            self.pack["built_from"],
            {"project": "agent-efficiency", "source": "bundled"},
        )
        self.assertEqual(
            self.pack["publication"]["reviewers"],
            ["agent-efficiency maintainers"],
        )
        self.assertEqual(
            [card["id"] for card in self.pack["cards"]],
            [
                "core.narrow-review",
                "core.research-boundary",
                "core.work-packet",
            ],
        )
        for card in self.pack["cards"]:
            self.assertEqual(card["authority"], "advisory")
            self.assertEqual(card["recommendation"], "adopt")
            self.assertTrue(all(item.startswith("AE.") for item in card["principles"]))

    def test_card_tampering_is_rejected(self) -> None:
        tampered = copy.deepcopy(self.pack)
        tampered["cards"][0]["directive"] += " Tampered."
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tampered.json"
            path.write_bytes(capability_pack_bytes(tampered))
            with self.assertRaises(ContractViolation) as caught:
                load_capability_pack(path)
        self.assertEqual(caught.exception.code, "card_digest_mismatch")

    def test_envelope_budget_is_enforced(self) -> None:
        changed = copy.deepcopy(self.pack)
        changed["cards"][0]["directive"] = ("Long guidance text " * 25).strip()
        changed["card_digests"][0]["content_sha256"] = canonical_sha256(
            changed["cards"][0]
        )
        with self.assertRaises(ContractViolation) as caught:
            validate_capability_pack_document(changed)
        self.assertEqual(caught.exception.code, "envelope_budget")

    def test_incompatible_pack_fails_closed(self) -> None:
        incompatible = copy.deepcopy(self.pack)
        incompatible["requires"]["agent_efficiency"]["min_inclusive"] = "9.0.0"
        incompatible["requires"]["agent_efficiency"]["max_exclusive"] = "10.0.0"
        with self.assertRaises(ContractViolation) as caught:
            capability_pack_policies(incompatible)
        self.assertEqual(caught.exception.code, "incompatible_pack")

    def test_unknown_pack_schema_fails_closed(self) -> None:
        incompatible = copy.deepcopy(self.pack)
        incompatible["schema_version"] = 2
        with self.assertRaises(ContractViolation) as caught:
            adapt_capability_pack(incompatible)
        self.assertEqual(caught.exception.code, "incompatible_pack")

    def test_pack_applies_to_all_supported_hosts(self) -> None:
        adapted = adapt_capability_pack(self.pack)
        for card in adapted["cards"]:
            self.assertEqual(
                set(card["applies_to"]["hosts"]),
                {"claude", "cursor", "codex"},
            )
        self.assertTrue(
            capability_pack_policies(
                self.pack,
                agent_efficiency_version="0.1.0",
            )
        )

    def test_policy_pack_uses_guidance_wording(self) -> None:
        policies = {
            policy["id"]: policy
            for policy in capability_pack_policies(self.pack)
        }
        for card in self.pack["cards"]:
            self.assertEqual(policies[card["id"]]["message"], card["directive"])
            self.assertEqual(
                policies[card["id"]]["task_types"],
                card["applies_to"]["tasks"],
            )

    def test_bad_guidance_pack_preserves_operational_core(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            patch(
                "agent_efficiency.policy.load_bundled_capability_pack",
                side_effect=ValueError("bad pack"),
            ),
        ):
            policies = PolicyPack.load(temp)
        self.assertIn("core.break-failure-loop", policies.ids())
        self.assertIn("core.verify-change", policies.ids())
        self.assertNotIn("core.work-packet", policies.ids())

    def test_builder_confirms_committed_pack_is_current(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/build_guidance_pack.py", "--check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_policy_builder_confirms_committed_pack_is_current(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/build_policy_pack.py", "--check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
