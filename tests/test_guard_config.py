from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_efficiency.verification.config import ConfigMissing, load_project_config


class ConfigLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_missing_config_raises_config_missing(self) -> None:
        with self.assertRaises(ConfigMissing) as caught:
            load_project_config(self.root)
        self.assertIsInstance(caught.exception, ValueError)

    def test_invalid_config_raises_a_plain_value_error(self) -> None:
        (self.root / "agent-efficiency.toml").write_text("version = [\n")
        with self.assertRaises(ValueError) as caught:
            load_project_config(self.root)
        self.assertNotIsInstance(caught.exception, ConfigMissing)

    def test_a_config_that_fails_validation_is_not_missing(self) -> None:
        (self.root / "agent-efficiency.toml").write_text("version = 2\n")
        with self.assertRaises(ValueError) as caught:
            load_project_config(self.root)
        self.assertNotIsInstance(caught.exception, ConfigMissing)
