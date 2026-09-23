from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_efficiency.vault.gitmeta import GitMetaError, find_repository
from vault_fixtures import git, make_repo


class GitMetaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()

    def test_directory_outside_any_repository_has_none(self) -> None:
        self.assertIsNone(find_repository(self.base / "plain" / "deeper"))

    def test_reads_root_branch_head_and_remotes(self) -> None:
        repo = make_repo(
            self.base / "app", remotes={"origin": "git@git.example.com:Owner/App.git"}
        )
        (repo / "src").mkdir()
        found = find_repository(repo / "src")
        self.assertEqual(found.root, repo)
        self.assertEqual(found.branch, "main")
        self.assertEqual(found.head, git(repo, "rev-parse", "HEAD"))
        self.assertEqual(found.remotes, (("origin", "git@git.example.com:Owner/App.git"),))
        self.assertEqual(found.identity(), "git.example.com/Owner/App")

    def test_worktree_shares_identity_and_has_its_own_branch(self) -> None:
        repo = make_repo(
            self.base / "app", remotes={"origin": "https://git.example.com/owner/app.git"}
        )
        worktree = self.base / "app-feature"
        git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
        found = find_repository(worktree)
        self.assertEqual(found.root, worktree)
        self.assertEqual(found.common_dir, (repo / ".git").resolve())
        self.assertEqual(found.branch, "feature")
        self.assertEqual(found.head, git(worktree, "rev-parse", "HEAD"))
        self.assertEqual(found.identity(), "git.example.com/owner/app")

    def test_packed_refs_resolve(self) -> None:
        repo = make_repo(self.base / "app")
        git(repo, "pack-refs", "--all", "--prune")
        self.assertFalse((repo / ".git" / "refs" / "heads" / "main").exists())
        self.assertEqual(find_repository(repo).head, git(repo, "rev-parse", "HEAD"))

    def test_detached_head_has_no_branch(self) -> None:
        repo = make_repo(self.base / "app")
        git(repo, "checkout", "-q", "--detach")
        found = find_repository(repo)
        self.assertIsNone(found.branch)
        self.assertEqual(found.head, git(repo, "rev-parse", "HEAD"))

    def test_unborn_branch_has_no_head(self) -> None:
        found = find_repository(make_repo(self.base / "app", commit=False))
        self.assertEqual(found.branch, "main")
        self.assertIsNone(found.head)

    def test_nearest_repository_wins(self) -> None:
        outer = make_repo(self.base / "outer")
        inner = make_repo(outer / "inner")
        self.assertEqual(find_repository(inner / "x").root, inner)

    def test_several_remotes_without_origin_give_no_identity(self) -> None:
        repo = make_repo(
            self.base / "app",
            remotes={
                "a": "https://git.example.com/o/a.git",
                "b": "https://git.example.com/o/b.git",
            },
        )
        self.assertEqual(find_repository(repo).identity(), "")

    def test_single_remote_not_named_origin_is_used(self) -> None:
        repo = make_repo(
            self.base / "app", remotes={"upstream": "https://git.example.com/o/a.git"}
        )
        self.assertEqual(find_repository(repo).identity(), "git.example.com/o/a")

    def test_no_remote_gives_no_identity(self) -> None:
        self.assertEqual(find_repository(make_repo(self.base / "app")).identity(), "")

    def test_broken_git_file_is_reported(self) -> None:
        root = self.base / "broken"
        root.mkdir()
        (root / ".git").write_text("not a pointer\n", encoding="utf-8")
        with self.assertRaises(GitMetaError):
            find_repository(root)

    def test_reftable_is_reported(self) -> None:
        repo = self.base / "reftable"
        try:
            git(self.base, "init", "-q", "--ref-format=reftable", str(repo))
        except subprocess.CalledProcessError:
            self.skipTest("this git cannot create a reftable repository")
        with self.assertRaisesRegex(GitMetaError, "reftable"):
            find_repository(repo)

    def test_relative_gitdir_pointer_resolves(self) -> None:
        outer = make_repo(self.base / "outer")
        module = outer / "mod"
        (outer / ".git" / "modules").mkdir()
        git(
            self.base,
            "init",
            "-q",
            "-b",
            "main",
            "--separate-git-dir",
            str(outer / ".git" / "modules" / "mod"),
            str(module),
        )
        (module / ".git").write_text("gitdir: ../.git/modules/mod\n", encoding="utf-8")
        (module / "file").write_text("x\n", encoding="utf-8")
        git(module, "add", "file")
        git(module, "commit", "-q", "-m", "module")
        found = find_repository(module)
        self.assertEqual(found.root, module)
        self.assertEqual(found.git_dir, (outer / ".git" / "modules" / "mod").resolve())
        self.assertEqual(found.branch, "main")
        self.assertEqual(found.head, git(module, "rev-parse", "HEAD"))

    def remotes_from(self, section: str) -> tuple[tuple[str, str], ...]:
        repo = make_repo(Path(tempfile.mkdtemp(dir=self.base)))
        with (repo / ".git" / "config").open("a", encoding="utf-8") as stream:
            stream.write(section)
        return find_repository(repo).remotes

    def test_remote_keyword_is_case_insensitive(self) -> None:
        self.assertEqual(
            self.remotes_from('[Remote "origin"]\n\turl = https://git.example.com/o/a\n'),
            (("origin", "https://git.example.com/o/a"),),
        )

    def test_url_key_is_case_insensitive(self) -> None:
        self.assertEqual(
            self.remotes_from('[remote "origin"]\n\tURL = https://git.example.com/o/a\n'),
            (("origin", "https://git.example.com/o/a"),),
        )

    def test_quoted_url_loses_its_quotes(self) -> None:
        self.assertEqual(
            self.remotes_from('[remote "origin"]\n\turl = "https://git.example.com/o/a"\n'),
            (("origin", "https://git.example.com/o/a"),),
        )

    def test_trailing_comments_are_dropped(self) -> None:
        for comment in (" ; mirror", " # mirror"):
            with self.subTest(comment=comment):
                self.assertEqual(
                    self.remotes_from(
                        f'[remote "origin"]\n\turl = https://git.example.com/o/a{comment}\n'
                    ),
                    (("origin", "https://git.example.com/o/a"),),
                )

    def test_git_file_with_a_nul_byte_is_reported(self) -> None:
        root = self.base / "nul"
        root.mkdir()
        (root / ".git").write_text("gitdir: a\x00b\n", encoding="utf-8")
        with self.assertRaises(GitMetaError):
            find_repository(root)

    def test_unreadable_marker_is_reported(self) -> None:
        denied = PermissionError(13, "Permission denied")
        with (
            mock.patch.object(Path, "is_file", side_effect=denied),
            self.assertRaises(GitMetaError),
        ):
            find_repository(self.base / "plain")


if __name__ == "__main__":
    unittest.main()
