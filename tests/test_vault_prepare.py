from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_efficiency.vault.index import write_index
from agent_efficiency.vault.prepare import EMPTY_DIGEST, TIME_BUDGET_NS, prepare
from agent_efficiency.vault.render import ALLOWANCE, HEAD_CAP, OMISSION_RESERVE
from vault_fixtures import (
    git,
    make_repo,
    make_vault_tree,
    note_text,
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

    def test_unreadable_config_path_degrades(self) -> None:
        config = self.data / "vault.json"
        original = Path.is_file

        def is_file(path: Path) -> bool:
            if path == config:
                raise PermissionError(13, "Permission denied", str(path))
            return original(path)

        with mock.patch.object(Path, "is_file", is_file):
            result = prepare(self.project, self.data)
        self.assertEqual((result.status, result.reason), ("degraded", "parse_error"))

    def test_unresolvable_working_directory_degrades(self) -> None:
        original = Path.resolve

        def resolve(path: Path, strict: bool = False) -> Path:
            if path == self.project:
                raise RuntimeError("Symlink loop from the working directory")
            return original(path, strict=strict)

        with mock.patch.object(Path, "resolve", resolve):
            result = prepare(self.project, self.data)
        self.assertEqual((result.status, result.reason), ("degraded", "parse_error"))

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
        with mock.patch("agent_efficiency.vault.prepare.render") as render:
            result = prepare(self.project, self.data, clock=lambda: next(ticks))
        self.assertEqual((result.status, result.reason), ("degraded", "timeout"))
        render.assert_not_called()

    def test_commit_that_leaves_the_text_alone_changes_only_the_revision(
        self,
    ) -> None:
        first = prepare(self.project, self.data)
        (self.private / "NOTES.txt").write_text("outside the notes\n", encoding="utf-8")
        git(self.private, "add", "NOTES.txt")
        git(self.private, "commit", "-q", "-m", "unrelated")
        second = prepare(self.project, self.data)
        self.assertNotEqual(first.revision, second.revision)
        self.assertEqual(first.digest, second.digest)

    def test_uncommitted_edit_changes_only_the_digest(self) -> None:
        first = prepare(self.project, self.data)
        note = self.private / "projects" / "catalog.md"
        note.write_text(
            note.read_text(encoding="utf-8").replace(
                "Next: API slice.", "Next: CLI slice."
            ),
            encoding="utf-8",
        )
        write_index(self.private)
        second = prepare(self.project, self.data)
        self.assertIn("Next: CLI slice.", second.rendered.text)
        self.assertEqual(first.revision, second.revision)
        self.assertNotEqual(first.digest, second.digest)
        self.assertEqual(second.digest, prepare(self.project, self.data).digest)

    def test_head_file_over_its_cap_reports_cap_exceeded(self) -> None:
        body = "x" * (HEAD_CAP - 10)
        tree = make_vault_tree(
            self.base / "vault-full",
            "private",
            [note_text("catalog", repos=("example/catalog",), body=body)],
        )
        register_trees(self.data, self.core, tree)
        result = prepare(self.project, self.data)
        self.assertIn(body, result.rendered.text)
        self.assertEqual(
            (result.reason, result.rendered.truncated), ("cap_exceeded", False)
        )

    def test_cut_head_characters_count_as_omitted(self) -> None:
        body = "z" * 5000
        tree = make_vault_tree(
            self.base / "vault-large",
            "private",
            [note_text("catalog", repos=("example/catalog",), body=body)],
        )
        register_trees(self.data, self.core, tree)
        result = prepare(self.project, self.data)
        self.assertTrue(result.rendered.truncated)
        self.assertEqual(result.rendered.head_chars, HEAD_CAP)
        self.assertEqual(result.rendered.chars_omitted, len(body) - HEAD_CAP)

    def test_nested_repository_with_unreadable_metadata_keeps_its_boundary(
        self,
    ) -> None:
        outer = make_repo(self.base / "outer")
        inner = outer / "inner"
        inner.mkdir()
        (inner / ".git").write_text(
            f"gitdir: {self.base / 'missing-gitdir'}\n", encoding="utf-8"
        )
        tree = make_vault_tree(
            self.base / "vault-outer",
            "private",
            [note_text("outer", paths=(str(outer),))],
        )
        register_trees(self.data, self.core, tree)
        self.assertEqual(prepare(outer, self.data).selection.outcome, "matched")
        self.assertEqual(prepare(inner, self.data).selection.outcome, "unmapped")

    def outer_note_tree(self, outer: Path) -> None:
        tree = make_vault_tree(
            self.base / "vault-outer",
            "private",
            [note_text("outer", paths=(str(outer),))],
        )
        register_trees(self.data, self.core, tree)

    def test_nested_marker_that_cannot_be_followed_keeps_its_boundary(
        self,
    ) -> None:
        if os.geteuid() == 0:
            self.skipTest("root reads through any permission")
        outer = make_repo(self.base / "outer")
        inner = make_repo(outer / "inner")
        protected = self.base / "protected"
        protected.mkdir()
        (inner / ".git").rename(protected / "gitdir")
        (inner / ".git").symlink_to(protected / "gitdir", target_is_directory=True)
        self.outer_note_tree(outer)
        protected.chmod(0)
        self.addCleanup(protected.chmod, 0o700)
        self.assertEqual(prepare(inner, self.data).selection.outcome, "unmapped")

    def test_nested_marker_that_cannot_be_checked_keeps_its_boundary(
        self,
    ) -> None:
        outer = make_repo(self.base / "outer")
        inner = outer / "inner"
        inner.mkdir()
        self.outer_note_tree(outer)
        original = Path.lstat

        def lstat(path: Path):
            if path == inner / ".git":
                raise PermissionError(13, "Permission denied", str(path))
            return original(path)

        with mock.patch.object(Path, "lstat", lstat):
            result = prepare(inner, self.data)
        self.assertEqual(result.selection.outcome, "unmapped")

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
        notes.append(
            note_text(
                "catalog-work",
                classification="work",
                repos=("example/catalog",),
                body="Next: API slice.",
            )
        )
        work = make_vault_tree(self.base / "vault-work", "work", notes, commit=False)
        register_trees(self.data, self.core, work)
        started = time.perf_counter()
        result = prepare(self.project, self.data)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.5)
        self.assertEqual(
            (result.selection.classification, result.selection.entry["id"]),
            ("work", "catalog-work"),
        )
        text = result.rendered.text
        self.assertLessEqual(len(text), ALLOWANCE)
        self.assertGreater(len(text), ALLOWANCE - 2 * OMISSION_RESERVE)
        self.assertIn("Omitted for space:", text)
        self.assertEqual(result.reason, "over_budget")


if __name__ == "__main__":
    unittest.main()
