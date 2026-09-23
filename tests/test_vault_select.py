from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_efficiency.vault.gitmeta import find_repository
from agent_efficiency.vault.root import find_tree
from agent_efficiency.vault.select import SelectionError, load_index, select_project
from vault_fixtures import git, make_repo, make_vault_tree, note_text


def entry(
    note_id: str,
    *,
    note_type: str = "project",
    status: str = "active",
    repos: tuple[str, ...] = (),
    paths: tuple[str, ...] = (),
    branches: tuple[str, ...] = (),
) -> dict:
    return {
        "id": note_id,
        "title": f"{note_id} title",
        "type": note_type,
        "status": status,
        "updated": "2026-09-22",
        "hook": f"{note_id} hook",
        "path": f"projects/{note_id}.md",
        "size": 100,
        "within_cap": True,
        "repos": list(repos),
        "paths": list(paths),
        "branches": list(branches),
    }


def indexes(**trees: list[dict]) -> dict[str, dict]:
    trees.setdefault("core", [])
    return {
        name: {"schema": 1, "classification": name, "notes": notes}
        for name, notes in trees.items()
    }


class SelectProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()

    def select(self, cwd: Path, idx: dict[str, dict]):
        return select_project(cwd, find_repository(cwd), idx)

    def test_plain_directory_matches_by_absolute_path(self) -> None:
        work = self.base / "notes-dir" / "sub"
        work.mkdir(parents=True)
        idx = indexes(private=[entry("notes", paths=(str(self.base / "notes-dir"),))])
        result = self.select(work, idx)
        self.assertEqual(
            (result.outcome, result.entry["id"], result.scope),
            ("matched", "notes", ("core", "private")),
        )

    def test_no_match_is_unmapped_with_core_scope(self) -> None:
        work = self.base / "elsewhere"
        work.mkdir()
        result = self.select(
            work, indexes(private=[entry("notes", paths=("/nowhere",))])
        )
        self.assertEqual(
            (result.outcome, result.entry, result.scope), ("unmapped", None, ("core",))
        )

    def test_longest_path_wins_in_a_monorepo(self) -> None:
        repo = make_repo(self.base / "mono")
        (repo / "svc" / "api").mkdir(parents=True)
        idx = indexes(
            work=[
                entry("mono", paths=(str(repo),)),
                entry("api", paths=(str(repo / "svc"),)),
            ]
        )
        self.assertEqual(self.select(repo / "svc" / "api", idx).entry["id"], "api")
        self.assertEqual(self.select(repo, idx).entry["id"], "mono")

    def test_repository_relative_path_needs_a_matching_remote(self) -> None:
        repo = make_repo(
            self.base / "mono", remotes={"origin": "git@git.example.com:o/mono.git"}
        )
        (repo / "svc").mkdir()
        matching = indexes(work=[entry("svc", repos=("o/mono",), paths=("svc",))])
        foreign = indexes(work=[entry("svc", repos=("o/other",), paths=("svc",))])
        self.assertEqual(self.select(repo / "svc", matching).entry["id"], "svc")
        self.assertEqual(self.select(repo / "svc", foreign).outcome, "unmapped")

    def test_remote_match_is_the_fallback(self) -> None:
        repo = make_repo(
            self.base / "app",
            remotes={"origin": "https://git.example.com/example/catalog.git"},
        )
        idx = indexes(private=[entry("catalog", repos=("example/catalog",))])
        self.assertEqual(self.select(repo, idx).entry["id"], "catalog")

    def test_path_match_beats_remote_match(self) -> None:
        repo = make_repo(
            self.base / "app", remotes={"origin": "https://git.example.com/o/app.git"}
        )
        idx = indexes(
            private=[
                entry("by-remote", repos=("o/app",)),
                entry("by-path", paths=(str(repo),)),
            ]
        )
        self.assertEqual(self.select(repo, idx).entry["id"], "by-path")

    def test_worktree_matches_through_the_shared_remote(self) -> None:
        repo = make_repo(
            self.base / "app", remotes={"origin": "https://git.example.com/o/app.git"}
        )
        worktree = self.base / "app-wt"
        git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
        idx = indexes(private=[entry("app", repos=("o/app",), paths=(str(repo),))])
        self.assertEqual(self.select(worktree, idx).entry["id"], "app")

    def test_parent_path_is_never_inherited_by_a_nested_repository(self) -> None:
        outer = make_repo(self.base / "outer")
        inner = make_repo(outer / "inner")
        idx = indexes(private=[entry("outer", paths=(str(outer),))])
        self.assertEqual(self.select(inner, idx).outcome, "unmapped")
        self.assertEqual(self.select(outer, idx).entry["id"], "outer")

    def test_exact_tie_is_ambiguous_and_names_the_ids(self) -> None:
        repo = make_repo(self.base / "app")
        idx = indexes(
            work=[entry("b", paths=(str(repo),))],
            private=[entry("a", paths=(str(repo),))],
        )
        result = self.select(repo, idx)
        self.assertEqual(
            (result.outcome, result.tied, result.scope),
            ("ambiguous", ("a", "b"), ("core",)),
        )

    def test_branch_narrows_a_tie(self) -> None:
        repo = make_repo(self.base / "app")
        git(repo, "checkout", "-q", "-b", "feature")
        idx = indexes(
            work=[
                entry("main-line", paths=(str(repo),), branches=("main",)),
                entry("feature-line", paths=(str(repo),), branches=("feature",)),
            ]
        )
        self.assertEqual(self.select(repo, idx).entry["id"], "feature-line")

    def test_general_note_beats_a_note_for_another_branch(self) -> None:
        repo = make_repo(self.base / "app")
        idx = indexes(
            work=[
                entry("general", paths=(str(repo),)),
                entry("other", paths=(str(repo),), branches=("feature",)),
            ]
        )
        self.assertEqual(self.select(repo, idx).entry["id"], "general")

    def test_dormant_and_non_project_notes_are_never_selected(self) -> None:
        repo = make_repo(self.base / "app")
        idx = indexes(
            private=[
                entry("old", status="dormant", paths=(str(repo),)),
                entry("rule", note_type="feedback", paths=(str(repo),)),
            ]
        )
        self.assertEqual(self.select(repo, idx).outcome, "unmapped")

    def test_tilde_paths_expand(self) -> None:
        work = self.base / "proj"
        work.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(self.base)}):
            idx = indexes(private=[entry("proj", paths=("~/proj",))])
            self.assertEqual(self.select(work, idx).entry["id"], "proj")


class LoadIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = make_vault_tree(
            Path(self.temp.name) / "core",
            "core",
            [note_text("rule", note_type="feedback", classification="core")],
            commit=False,
        )

    def test_reads_a_generated_index(self) -> None:
        index = load_index(find_tree(self.root))
        self.assertEqual(index["notes"][0]["id"], "rule")

    def test_missing_index_is_an_error(self) -> None:
        (self.root / "index.json").unlink()
        with self.assertRaises(SelectionError):
            load_index(find_tree(self.root))

    def test_index_from_another_tree_is_an_error(self) -> None:
        (self.root / "index.json").write_text(
            '{"schema": 1, "classification": "work", "notes": []}', encoding="utf-8"
        )
        with self.assertRaises(SelectionError):
            load_index(find_tree(self.root))


if __name__ == "__main__":
    unittest.main()
