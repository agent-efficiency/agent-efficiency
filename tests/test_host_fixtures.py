from __future__ import annotations

import json
import unittest
from pathlib import Path

from agent_efficiency.adapters import ADAPTERS, adapter_for
from agent_efficiency.contracts.effects import CanonicalEffect


FIXTURES = Path(__file__).parent / "fixtures" / "hosts"


class HostFixtureTests(unittest.TestCase):
    def test_every_declared_event_has_a_privacy_safe_fixture(self) -> None:
        for path in sorted(FIXTURES.glob("*.json")):
            document = json.loads(path.read_text())
            host = document["host"]
            adapter = adapter_for(host)
            self.assertEqual(
                {case["native_event"] for case in document["cases"]},
                set(ADAPTERS[host].native_events),
            )
            for case in document["cases"]:
                with self.subTest(host=host, event=case["native_event"]):
                    event = adapter.to_event(case["native_input"])
                    self.assertIsNotNone(event)
                    self.assertEqual(event.name, case["expected_canonical_event"])
                    self.assertEqual(dict(event.facts), case["expected_safe_facts"])
                    self.assertEqual(
                        list(event.capabilities), case["expected_capabilities"]
                    )
                    self.assertNotIn("private_fixture", repr(event))
                    effect = CanonicalEffect(
                        effect=case["canonical_effect"],
                        policy_id="fixture.none",
                        policy_revision="1",
                        observed_fact="fixture",
                        agent_message="",
                    )
                    self.assertEqual(
                        adapter.render(effect, native_event=case["native_event"]),
                        case["expected_native_output"],
                    )
                    self.assertIsNone(case["selected_policy"])


if __name__ == "__main__":
    unittest.main()
