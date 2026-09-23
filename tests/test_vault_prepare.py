from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from agent_efficiency.vault.prepare import EMPTY_DIGEST, TIME_BUDGET_NS, prepare
from vault_fixtures import (
    make_repo,
    make_vault_tree,
    note_text,
    recommit,
    register_trees,
)


class PrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.data = self.base / "data"
        self.project = make_repo(
            self.base / "catalog",
            remotes={"origin": "https://git.example.com/example/catalog.git"},
        )
        self.core = make_vault_tree(
            self.base / "vault-core",
            "core",
            [
                note_text(
                    "writing",
                    note_type="feedback",
                    classification="core",
                    hook="Plain English.",
                )
            ],
        )
        self.private = make_vault_tree(
            self.base / "vault-private",
            "private",
            [
                note_text(
                    "catalog", repos=("example/catalog",), body="Next: API slice."
                ),
                note_text("calendar"),
            ],
        )
        register_trees(self.data, self.core, self.private)

    def test_no_registered_trees_is_unavailable(self) -> None:
        result = prepare(self.project, self.base / "empty-data")
        self.assertEqual(
            (result.status, result.reason, result.digest),
            ("unavailable", "no_trees", EMPTY_DIGEST),
        )

    def test_matched_project_is_ready_with_digests(self) -> None:
        result = prepare(self.project, self.data)
        self.assertEqual(
            (result.status, result.reason, result.selection.outcome),
            ("ready", None, "matched"),
        )
        self.assertIn("Next: API slice.", result.rendered.text)
        self.assertRegex(result.revision, r"^[0-9a-f]{16}$")
        self.assertRegex(result.digest, r"^[0-9a-f]{16}$")

    def test_unmapped_directory_reports_unmapped(self) -> None:
        other = self.base / "other"
        other.mkdir()
        self.assertEqual(prepare(other, self.data).reason, "unmapped")

    def test_corrupt_index_degrades(self) -> None:
        (self.private / "index.json").write_text("{", encoding="utf-8")
        result = prepare(self.project, self.data)
        self.assertEqual((result.status, result.reason), ("degraded", "parse_error"))

    def test_corrupt_tree_list_degrades(self) -> None:
        (self.data / "vault.json").write_text("[]", encoding="utf-8")
        result = prepare(self.project, self.data)
        self.assertEqual((result.status, result.reason), ("degraded", "parse_error"))

    def test_slow_selection_degrades_with_timeout(self) -> None:
        ticks = iter([0, TIME_BUDGET_NS + 1])
        result = prepare(self.project, self.data, clock=lambda: next(ticks))
        self.assertEqual((result.status, result.reason), ("degraded", "timeout"))

    def test_revision_follows_commits_and_digest_follows_text(self) -> None:
        first = prepare(self.project, self.data)
        note = self.private / "projects" / "catalog.md"
        note.write_text(
            note.read_text(encoding="utf-8").replace(
                "Next: API slice.", "Next: CLI slice."
            ),
            encoding="utf-8",
        )
        recommit(self.private)
        second = prepare(self.project, self.data)
        self.assertNotEqual(first.revision, second.revision)
        self.assertNotEqual(first.digest, second.digest)
        self.assertEqual(second.digest, prepare(self.project, self.data).digest)

    def test_realistic_vault_prepares_well_inside_the_hook_budget(self) -> None:
        notes = [
            note_text(
                f"rule-{n:03d}",
                note_type="feedback",
                classification="work",
                hook="h" * 90,
            )
            for n in range(580)
        ]
        work = make_vault_tree(self.base / "vault-work", "work", notes, commit=False)
        register_trees(self.data, self.core, work, self.private)
        started = time.perf_counter()
        prepare(self.project, self.data)
        self.assertLess(time.perf_counter() - started, 0.5)


if __name__ == "__main__":
    unittest.main()
