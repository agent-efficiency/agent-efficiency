from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from agent_efficiency.vault.config import (
    VaultConfigError,
    config_path,
    load_trees,
    register_tree,
    stale_entries,
)
from vault_fixtures import make_vault_tree, register_trees


class VaultConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.data = self.base / "data"
        self.core = make_vault_tree(self.base / "core", "core", [], commit=False)
        self.private = make_vault_tree(
            self.base / "private", "private", [], commit=False
        )

    def test_missing_file_means_no_trees(self) -> None:
        self.assertEqual(load_trees(self.data), ())

    def test_register_writes_and_load_orders_by_classification(self) -> None:
        register_tree(self.data, self.private)
        register_tree(self.data, self.core)
        trees = load_trees(self.data)
        self.assertEqual([tree.classification for tree in trees], ["core", "private"])
        self.assertEqual(json.loads(config_path(self.data).read_text())["schema"], 1)

    def test_register_same_root_twice_is_a_no_op(self) -> None:
        register_tree(self.data, self.core)
        register_tree(self.data, self.core)
        stored = json.loads(config_path(self.data).read_text())
        self.assertEqual(stored["trees"], [str(self.core)])

    def test_second_tree_with_same_classification_is_refused(self) -> None:
        other = make_vault_tree(self.base / "core2", "core", [], commit=False)
        register_tree(self.data, self.core)
        with self.assertRaises(VaultConfigError):
            register_tree(self.data, other)

    def test_subdirectory_of_a_tree_is_refused(self) -> None:
        with self.assertRaises(VaultConfigError):
            register_tree(self.data, self.core / "feedback")

    def test_directory_that_is_not_a_tree_is_refused(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()
        with self.assertRaises(VaultConfigError):
            register_tree(self.data, plain)

    def test_malformed_file_is_reported(self) -> None:
        self.data.mkdir(parents=True)
        for text in (
            "{",
            "[]",
            '{"schema": true, "trees": []}',
            '{"schema": 1, "trees": [1]}',
        ):
            config_path(self.data).write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(VaultConfigError):
                load_trees(self.data)

    def test_hand_edited_duplicate_classification_is_reported(self) -> None:
        other = make_vault_tree(self.base / "core2", "core", [], commit=False)
        register_trees(self.data, self.core, other)
        with self.assertRaises(VaultConfigError):
            load_trees(self.data)

    def test_entry_with_a_nul_byte_is_reported(self) -> None:
        register_trees(self.data, self.core, Path("/vault\x00tree"))
        with self.assertRaises(VaultConfigError):
            load_trees(self.data)

    def test_relative_entry_is_refused(self) -> None:
        self.chdir(self.base)
        register_trees(self.data, Path("core"))
        with self.assertRaisesRegex(VaultConfigError, "entry 1"):
            load_trees(self.data)

    def test_empty_entry_is_refused(self) -> None:
        self.chdir(self.core)
        self.data.mkdir(parents=True)
        config_path(self.data).write_text(
            json.dumps({"schema": 1, "trees": [str(self.private), ""]}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(VaultConfigError, "entry 2"):
            load_trees(self.data)

    def test_stale_entry_is_dropped_when_a_tree_is_registered(self) -> None:
        register_tree(self.data, self.core)
        register_tree(self.data, self.private)
        shutil.rmtree(self.private)
        moved = make_vault_tree(self.base / "moved", "private", [], commit=False)
        self.assertEqual(stale_entries(self.data), [str(self.private)])
        self.assertEqual(register_tree(self.data, moved).root, moved)
        stored = json.loads(config_path(self.data).read_text())
        self.assertEqual(stored["trees"], [str(self.core), str(moved)])
        self.assertEqual(stale_entries(self.data), [])

    def test_stale_entry_makes_load_strict_and_names_the_fix(self) -> None:
        register_trees(self.data, self.core, self.base / "gone")
        with self.assertRaises(VaultConfigError) as caught:
            load_trees(self.data)
        self.assertTrue(
            str(caught.exception).endswith(
                f"Fix or remove the entry in {config_path(self.data)}."
            )
        )

    def chdir(self, path: Path) -> None:
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(path)


if __name__ == "__main__":
    unittest.main()
