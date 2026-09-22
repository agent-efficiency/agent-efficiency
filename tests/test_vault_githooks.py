"""The generated git hooks, run by git, against real staged and pushed content.

These tests do not inspect the hook scripts. They build a repository, put
content into the git index or into commits, and let git run the installed hook.
That is the only way to tell whether a hook checks what git is about to carry
or merely what happens to be in the working copy at the time. A hook that reads
the working copy passes every one of the bad cases below while the bad content
goes to the remote.

The ``agent-efficiency`` command the hooks call is provided by a small shim on
PATH that runs this checkout, so the hooks under test are the ones this branch
generates.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import agent_efficiency
from agent_efficiency.cli import main

SOURCE_ROOT = Path(agent_efficiency.__file__).resolve().parents[1]

# Assembled from parts so this source file carries no complete credential.
TOKEN_LINE = "api_key: " + "sk" + "-" + "x" * 40


def note(id_: str, classification: str = "work", body: str = "Body.") -> str:
    return (
        "---\n"
        "schema: 1\n"
        f"id: {id_}\n"
        f"title: {id_} title\n"
        "type: project\n"
        f"classification: {classification}\n"
        "status: active\n"
        "updated: 2026-09-21\n"
        f"hook: {id_} hook\n"
        "---\n"
        f"{body}\n"
    )


class HookHarness(unittest.TestCase):
    """A vault tree that is a git repository with the generated hooks live."""

    def setUp(self) -> None:
        if shutil.which("git") is None:
            raise unittest.SkipTest("git is not installed")
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        shim = self.bin / "agent-efficiency"
        shim.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" -m agent_efficiency "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)

    def environment(self) -> dict[str, str]:
        found = dict(os.environ)
        found.update(
            {
                "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "PYTHONPATH": str(SOURCE_ROOT),
                "GIT_AUTHOR_NAME": "vault test",
                "GIT_AUTHOR_EMAIL": "vault@example.com",
                "GIT_COMMITTER_NAME": "vault test",
                "GIT_COMMITTER_EMAIL": "vault@example.com",
                # Keep the machine's own git configuration out of the result.
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
            }
        )
        return found

    def git(self, *arguments: str, cwd: Path | None = None):
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd or self.root,
            capture_output=True,
            text=True,
            env=self.environment(),
            check=False,
        )

    def run_git(self, *arguments: str, cwd: Path | None = None):
        result = self.git(*arguments, cwd=cwd)
        if result.returncode != 0:
            self.fail(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
        return result

    def vault(self, *arguments: str) -> int:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            return main(list(arguments))

    def make_tree(self, remote: str) -> Path:
        self.root = self.base / "vault-work"
        self.assertEqual(
            self.vault(
                "vault",
                "init",
                str(self.root),
                "--classification",
                "work",
                "--remote",
                remote,
            ),
            0,
        )
        self.run_git("init", "-b", "main")
        self.run_git("config", "--local", "core.hooksPath", ".githooks")
        self.run_git("add", "-A")
        self.run_git("commit", "-m", "init")
        return self.root

    def write(self, relative: str, text: str) -> Path:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def stage_then_restore(self, relative: str, staged: str, restored: str) -> None:
        """Stage one content and leave a different, clean content on disk.

        This is the shape of the whole problem. A check that reads the working
        copy sees ``restored`` and reports nothing while git carries ``staged``.
        """

        self.write(relative, staged)
        self.run_git("add", relative)
        self.write(relative, restored)


class PreCommitTests(HookHarness):
    def setUp(self) -> None:
        super().setUp()
        self.make_tree("git@git.example.com:example/vault-work.git")

    def test_a_clean_staged_note_commits(self) -> None:
        self.write("projects/alpha.md", note("alpha"))
        self.run_git("add", "-A")
        result = self.git("commit", "-m", "alpha")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_staged_misclassification_is_refused_behind_a_clean_copy(self) -> None:
        self.stage_then_restore(
            "projects/alpha.md", note("alpha", "private"), note("alpha", "work")
        )
        result = self.git("commit", "-m", "alpha")
        self.assertNotEqual(result.returncode, 0, "the misclassified note committed")
        self.assertIn("classification", result.stdout + result.stderr)
        self.assertEqual(self.git("rev-list", "--count", "HEAD").stdout.strip(), "1")

    def test_a_staged_secret_is_refused_behind_a_clean_copy(self) -> None:
        self.stage_then_restore(
            "projects/alpha.md",
            note("alpha", body=TOKEN_LINE),
            note("alpha"),
        )
        result = self.git("commit", "-m", "alpha")
        self.assertNotEqual(result.returncode, 0, "the staged secret committed")
        self.assertIn("assigned_secret", result.stdout + result.stderr)

    def test_a_staged_note_over_its_cap_is_refused(self) -> None:
        self.stage_then_restore(
            "projects/alpha.md",
            note("alpha", body="y" * 4000),
            note("alpha"),
        )
        result = self.git("commit", "-m", "alpha")
        self.assertNotEqual(result.returncode, 0, "the oversized note committed")
        self.assertIn("cap", result.stdout + result.stderr)

    def test_a_staged_note_that_collides_with_a_committed_one_is_refused(self) -> None:
        self.write("projects/alpha.md", note("alpha"))
        self.run_git("add", "-A")
        self.run_git("commit", "-m", "alpha")

        self.stage_then_restore(
            "feedback/copy.md", note("alpha"), note("second")
        )
        result = self.git("commit", "-m", "copy")
        self.assertNotEqual(result.returncode, 0, "the duplicate id committed")
        self.assertIn("duplicate_id", result.stdout + result.stderr)

    def test_a_staged_archive_secret_is_refused(self) -> None:
        self.stage_then_restore(
            "projects/alpha/archive/0001-imported.md",
            f"Imported.\n{TOKEN_LINE}\n",
            "Imported.\n",
        )
        result = self.git("commit", "-m", "archive")
        self.assertNotEqual(result.returncode, 0, "the archived secret committed")

    def test_an_archive_named_index_md_is_still_scanned(self) -> None:
        self.write("projects/alpha/archive/INDEX.md", f"Imported.\n{TOKEN_LINE}\n")
        self.run_git("add", "-A")
        result = self.git("commit", "-m", "archive")
        self.assertNotEqual(
            result.returncode, 0, "a secret hid behind the INDEX.md name"
        )

    def test_a_dirty_working_copy_does_not_refuse_a_clean_commit(self) -> None:
        """The working copy is not what is being committed, in either direction."""

        self.write("projects/alpha.md", note("alpha"))
        self.run_git("add", "projects/alpha.md")
        self.write("projects/alpha.md", note("alpha", "private"))
        result = self.git("commit", "-m", "alpha")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class PrePushTests(HookHarness):
    def setUp(self) -> None:
        super().setUp()
        self.origin = self.base / "origin.git"
        self.make_tree(str(self.origin))
        self.run_git("init", "--bare", str(self.origin), cwd=self.base)

    def push(self, remote: str | None = None):
        return self.git("push", remote or str(self.origin), "main")

    def commit_without_checking(self, relative: str, text: str, message: str) -> None:
        self.write(relative, text)
        self.run_git("add", "-A")
        self.run_git("commit", "--no-verify", "-m", message)

    def test_a_clean_push_to_the_registered_remote_is_allowed(self) -> None:
        self.write("projects/alpha.md", note("alpha"))
        self.run_git("add", "-A")
        self.run_git("commit", "-m", "alpha")
        result = self.push()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_push_to_another_remote_is_refused(self) -> None:
        other = self.base / "other.git"
        self.run_git("init", "--bare", str(other), cwd=self.base)
        result = self.push(str(other))
        self.assertNotEqual(result.returncode, 0, "the push went to another remote")
        self.assertIn("destination", result.stdout + result.stderr)

    def test_a_misclassified_note_in_an_outgoing_commit_is_refused(self) -> None:
        self.commit_without_checking(
            "projects/alpha.md", note("alpha", "private"), "alpha"
        )
        result = self.push()
        self.assertNotEqual(result.returncode, 0, "the misclassified note pushed")
        self.assertIn("classification", result.stdout + result.stderr)

    def test_a_misclassification_that_only_exists_in_history_is_refused(self) -> None:
        """Correcting a note in a later commit does not unsend the earlier one.

        The push still carries the commit that held the wrong classification,
        so the content still reaches the remote. The tip tree is clean here on
        purpose: that is what makes this different from the tip-tree check.
        """

        self.commit_without_checking(
            "projects/alpha.md", note("alpha", "private"), "wrong"
        )
        self.commit_without_checking(
            "projects/alpha.md", note("alpha"), "corrected"
        )
        result = self.push()
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, "the misclassified history pushed")
        self.assertIn("classification_history", output)

    def test_a_clean_history_still_pushes(self) -> None:
        """The history rule must not refuse a push that never misfiled anything."""

        self.commit_without_checking("projects/alpha.md", note("alpha"), "one")
        self.commit_without_checking("projects/beta.md", note("beta"), "two")
        result = self.push()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_secret_in_an_outgoing_archive_is_refused(self) -> None:
        self.commit_without_checking(
            "projects/alpha/archive/0001-imported.md",
            f"Imported.\n{TOKEN_LINE}\n",
            "archive",
        )
        result = self.push()
        self.assertNotEqual(result.returncode, 0, "the archived secret pushed")

    def test_a_secret_that_only_exists_in_history_is_refused(self) -> None:
        """A push sends every outgoing commit, not only the tip tree."""

        self.commit_without_checking(
            "projects/alpha.md", note("alpha", body=TOKEN_LINE), "alpha"
        )
        self.commit_without_checking("projects/alpha.md", note("alpha"), "fixed")
        result = self.push()
        self.assertNotEqual(result.returncode, 0, "a secret in history pushed")

    def test_a_clean_working_copy_does_not_excuse_a_bad_commit(self) -> None:
        self.commit_without_checking(
            "projects/alpha.md", note("alpha", "private"), "alpha"
        )
        self.write("projects/alpha.md", note("alpha", "work"))
        result = self.push()
        self.assertNotEqual(
            result.returncode, 0, "a clean working copy let the commit push"
        )

    def test_a_local_remote_that_differs_only_by_git_is_refused(self) -> None:
        """``allowed.git`` and ``allowed`` are two directories, so two repositories."""

        twin = self.base / "origin"
        self.run_git("init", "--bare", str(twin), cwd=self.base)
        self.write("projects/alpha.md", note("alpha"))
        self.run_git("add", "-A")
        self.run_git("commit", "-m", "alpha")
        result = self.push(str(twin))
        self.assertNotEqual(result.returncode, 0, "the push went to the wrong twin")
        self.assertIn("destination", result.stdout + result.stderr)
        self.assertEqual(
            self.git("--git-dir", str(twin), "rev-list", "--count", "--all")
            .stdout.strip(),
            "0",
        )

    def test_the_hook_runs_at_all(self) -> None:
        """A pre-push hook that does nothing passes every test above but this one."""

        self.write("projects/alpha.md", note("alpha", "private"))
        self.run_git("add", "-A")
        self.run_git("commit", "--no-verify", "-m", "alpha")
        result = self.push()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("classification", result.stdout + result.stderr)
        self.assertEqual(
            self.git("--git-dir", str(self.origin), "rev-list", "--count", "--all")
            .stdout.strip(),
            "0",
            "the remote received the commit",
        )


if __name__ == "__main__":
    unittest.main()
