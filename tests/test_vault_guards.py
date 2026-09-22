from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_efficiency.vault import index as index_module
from agent_efficiency.vault.guards import (
    Finding,
    GuardError,
    check_content,
    check_tree,
    is_scannable,
    scan_secrets,
)
from agent_efficiency.vault.root import find_tree

TOKEN = "ghp_" + "a" * 36
OTHER_TOKEN = "ghp_" + "b" * 36
# Assembled from parts so this source file carries no complete key header.
KEY_HEADER = "-----BEGIN RSA PRIVATE" + " KEY-----"


def make_tree(
    root: Path,
    classification: str = "work",
    remote_url: str = "git@git.example.com:example/vault-work.git",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".vault.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "classification": classification,
                "preferred_remote": "origin",
                "remote_url": remote_url,
            }
        ),
        encoding="utf-8",
    )
    (root / "projects").mkdir(exist_ok=True)


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


def rendered(findings: list[Finding]) -> str:
    """Everything a finding could possibly expose, as one string."""

    parts = [repr(findings)]
    for item in findings:
        parts.extend(str(value) for value in dataclasses.asdict(item).values())
    return "\n".join(parts)


class SecretScanTests(unittest.TestCase):
    def test_detects_a_private_key_header(self) -> None:
        hits = scan_secrets(f"text\n{KEY_HEADER}\n")
        self.assertEqual([hit.rule for hit in hits], ["private_key"])

    def test_detects_a_github_token(self) -> None:
        hits = scan_secrets("token " + TOKEN)
        self.assertEqual([hit.rule for hit in hits], ["github_token"])

    def test_detects_an_aws_access_key(self) -> None:
        hits = scan_secrets("key AKIA" + "Q" * 16 + " here")
        self.assertEqual([hit.rule for hit in hits], ["aws_access_key"])

    def test_detects_an_assigned_api_key(self) -> None:
        hits = scan_secrets("api_key = " + "z" * 24)
        self.assertEqual([hit.rule for hit in hits], ["assigned_secret"])

    def test_passes_ordinary_prose(self) -> None:
        self.assertEqual(scan_secrets("The API key lives in the secret store."), [])

    def test_passes_prose_that_names_a_token_or_a_password(self) -> None:
        prose = (
            "Rotate the token when the contract changes.\n"
            "The password manager holds the production entry.\n"
            "password: ask the platform team for the shared entry\n"
        )
        self.assertEqual(scan_secrets(prose), [])

    def test_passes_a_url_assigned_to_a_secret_sounding_key(self) -> None:
        hits = scan_secrets("token: https://example.com/a/very/long/path/to/docs")
        self.assertEqual(hits, [])

    def test_passes_a_filesystem_path_assigned_to_a_secret_sounding_key(self) -> None:
        hits = scan_secrets("api_key: ~/.secrets/vendor/production-credentials")
        self.assertEqual(hits, [])

    def test_passes_a_short_assigned_value(self) -> None:
        self.assertEqual(scan_secrets("password = hunter2"), [])

    def test_a_hit_never_carries_the_matched_value(self) -> None:
        hits = scan_secrets(TOKEN)
        self.assertNotIn(TOKEN, rendered(hits))

    def test_reports_every_distinct_secret_not_only_the_first(self) -> None:
        text = f"first {TOKEN}\nsecond {OTHER_TOKEN}\n"
        hits = scan_secrets(text)
        self.assertEqual([hit.rule for hit in hits], ["github_token", "github_token"])
        self.assertEqual(
            [hit.location for hit in hits], ["line 1", "line 2"]
        )

    def test_reports_one_finding_per_rule_and_line(self) -> None:
        hits = scan_secrets(f"{TOKEN} and {OTHER_TOKEN} on one line")
        self.assertEqual(len(hits), 1)

    def test_reports_different_rules_from_the_same_text(self) -> None:
        text = f"{TOKEN}\n{KEY_HEADER}\n"
        self.assertEqual(
            sorted(hit.rule for hit in scan_secrets(text)),
            ["github_token", "private_key"],
        )

    def test_line_numbers_survive_crlf_input(self) -> None:
        hits = scan_secrets(f"one\r\ntwo\r\n{TOKEN}\r\n")
        self.assertEqual([hit.location for hit in hits], ["line 3"])

    def test_location_names_the_note_when_one_is_given(self) -> None:
        hits = scan_secrets(f"\n{TOKEN}", location="projects/alpha.md")
        self.assertEqual(hits[0].location, "projects/alpha.md:2")


class TreeCheckTests(unittest.TestCase):
    def test_a_clean_tree_produces_no_findings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_text(
                note("alpha"), encoding="utf-8"
            )
            self.assertEqual(check_tree(root), [])

    def test_flags_a_misclassified_note(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_text(
                note("alpha", "core"), encoding="utf-8"
            )
            findings = check_tree(root)
            self.assertEqual([item.rule for item in findings], ["classification"])
            self.assertIsInstance(findings[0], Finding)
            self.assertEqual(findings[0].location, "projects/alpha.md")
            self.assertTrue(findings[0].remedy)

    def test_flags_a_note_over_its_cap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_text(
                note("alpha", body="x" * 4000), encoding="utf-8"
            )
            self.assertEqual([item.rule for item in check_tree(root)], ["cap"])

    def test_flags_a_wrong_push_destination(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_text(
                note("alpha"), encoding="utf-8"
            )
            findings = check_tree(
                root, remote_url="git@git.example.com:example/vault-core.git"
            )
            self.assertEqual([item.rule for item in findings], ["destination"])

    def test_accepts_an_equivalent_spelling_of_the_registered_remote(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            findings = check_tree(
                root, remote_url="https://git.example.com/example/vault-work"
            )
            self.assertEqual(findings, [])

    def test_flags_a_push_when_the_tree_registers_no_remote(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root, remote_url="")
            findings = check_tree(
                root, remote_url="git@git.example.com:someone/elsewhere.git"
            )
            self.assertEqual([item.rule for item in findings], ["destination"])

    def test_no_destination_check_without_a_push_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root, remote_url="")
            self.assertEqual(check_tree(root), [])

    def test_reports_every_bad_note_not_only_the_first(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_text(
                note("alpha", "core"), encoding="utf-8"
            )
            (root / "projects" / "beta.md").write_text(
                note("beta", body="x" * 4000), encoding="utf-8"
            )
            (root / "projects" / "gamma.md").write_text(
                note("gamma", body=f"leaked {TOKEN}"), encoding="utf-8"
            )
            findings = check_tree(root)
            self.assertEqual(
                [item.rule for item in findings],
                ["classification", "cap", "github_token"],
            )

    def test_flags_broken_frontmatter_as_a_schema_finding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "broken.md").write_text(
                "no frontmatter at all\n", encoding="utf-8"
            )
            findings = check_tree(root)
            self.assertEqual([item.rule for item in findings], ["schema"])

    def test_a_schema_finding_never_echoes_a_secret(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            # The brace makes the scalar illegal, so the parser message would
            # otherwise quote the value back.
            (root / "projects" / "alpha.md").write_text(
                note("alpha").replace(
                    "title: alpha title", "title: {" + TOKEN + "}"
                ),
                encoding="utf-8",
            )
            findings = check_tree(root)
            self.assertIn("schema", [item.rule for item in findings])
            self.assertNotIn(TOKEN, rendered(findings))

    def test_findings_never_carry_note_body_text(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            marker = "acquisition of Northwind closes in March"
            (root / "projects" / "alpha.md").write_text(
                note("alpha", "core", body=marker + " x" * 3000), encoding="utf-8"
            )
            findings = check_tree(root)
            self.assertTrue(findings)
            self.assertNotIn(marker, rendered(findings))

    def test_scans_archives_for_secrets_even_though_they_are_not_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            archive = root / "projects" / "alpha" / "archive"
            archive.mkdir(parents=True)
            (archive / "0001.md").write_text(
                f"Imported handoff.\n{TOKEN}\n", encoding="utf-8"
            )
            findings = check_tree(root)
            self.assertEqual([item.rule for item in findings], ["github_token"])
            self.assertEqual(
                findings[0].location, "projects/alpha/archive/0001.md:2"
            )

    def test_does_not_apply_the_note_schema_to_archived_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            archive = root / "projects" / "alpha" / "archive"
            archive.mkdir(parents=True)
            (archive / "0001.md").write_text(
                "A plain imported handoff with no frontmatter.\n", encoding="utf-8"
            )
            self.assertEqual(check_tree(root), [])

    def test_flags_duplicate_ids(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "one.md").write_text(
                note("same"), encoding="utf-8"
            )
            (root / "projects" / "two.md").write_text(
                note("same"), encoding="utf-8"
            )
            findings = check_tree(root)
            self.assertEqual([item.rule for item in findings], ["duplicate_id"])
            self.assertIn("one.md", findings[0].detail)

    def test_flags_an_unreadable_note_and_keeps_going(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").mkdir()
            (root / "projects" / "beta.md").write_text(
                note("beta", "core"), encoding="utf-8"
            )
            findings = check_tree(root)
            self.assertEqual(
                [item.rule for item in findings], ["unreadable", "classification"]
            )

    def test_flags_a_note_that_is_not_valid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_bytes(b"---\nid: \xff\xfe\n---\n")
            self.assertEqual([item.rule for item in check_tree(root)], ["unreadable"])

    def test_ignores_dot_directories(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            hooks = root / ".githooks"
            hooks.mkdir()
            (hooks / "notes.md").write_text("junk\n", encoding="utf-8")
            self.assertEqual(check_tree(root), [])

    def test_raises_a_guard_error_for_a_malformed_push_target(self) -> None:
        """A bad URL reaches the caller as this module's error, not another one."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            with self.assertRaises(GuardError) as caught:
                check_tree(root, remote_url="https://git.example.com:no/example/vault")
            self.assertIn("port", str(caught.exception))

    def test_raises_for_a_directory_that_is_not_a_vault_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "plain"
            root.mkdir()
            with self.assertRaises(GuardError):
                check_tree(root)

    def test_raises_for_a_tree_with_a_malformed_marker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            root.mkdir(exist_ok=True)
            (root / ".vault.json").write_text("{not json", encoding="utf-8")
            with self.assertRaises(GuardError):
                check_tree(root)

class ContentCheckTests(unittest.TestCase):
    """The rules applied to content that was handed over, not read from disk.

    Every test here writes nothing but the tree marker. What is checked is the
    bytes in the argument, which is how a git hook checks a commit or a push.
    """

    def check(self, root: Path, items: list[tuple[str, str]], remote=None):
        return check_content(
            find_tree(root),
            [(path, text.encode("utf-8")) for path, text in items],
            remote_url=remote,
        )

    def test_flags_a_misclassified_note_that_is_not_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            findings = self.check(root, [("projects/a.md", note("a", "private"))])
            self.assertEqual([item.rule for item in findings], ["classification"])
            self.assertEqual(list(root.rglob("*.md")), [])

    def test_a_clean_set_produces_no_findings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            self.assertEqual(
                self.check(
                    root,
                    [("projects/a.md", note("a")), ("feedback/b.md", note("b"))],
                ),
                [],
            )

    def test_flags_two_handed_over_notes_that_share_an_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            findings = self.check(
                root, [("projects/a.md", note("a")), ("feedback/copy.md", note("a"))]
            )
            self.assertEqual([item.rule for item in findings], ["duplicate_id"])

    def test_scans_an_archive_named_index_md(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            findings = self.check(
                root, [("projects/a/archive/INDEX.md", f"text\n{TOKEN}\n")]
            )
            self.assertEqual([item.rule for item in findings], ["github_token"])

    def test_skips_the_generated_index_artifacts_at_the_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            self.assertEqual(
                self.check(root, [("INDEX.md", f"text\n{TOKEN}\n")]), []
            )
            self.assertFalse(is_scannable("index.json"))
            self.assertFalse(is_scannable("INDEX.md"))
            self.assertTrue(is_scannable("projects/a/archive/INDEX.md"))

    def test_flags_a_push_destination_with_no_content_at_all(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            findings = self.check(root, [], remote="git@git.example.com:someone/other.git")
            self.assertEqual([item.rule for item in findings], ["destination"])

    def test_refuses_a_path_that_leaves_the_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            with self.assertRaises(GuardError):
                self.check(root, [("../outside.md", "text\n")])

    def test_flags_bytes_that_are_not_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            findings = check_content(
                find_tree(root), [("projects/a.md", b"---\nid: \xff\n---\n")]
            )
            self.assertEqual([item.rule for item in findings], ["unreadable"])


class UnreadableDirectoryTests(unittest.TestCase):
    """Missing access and missing content must never look alike."""

    def setUp(self) -> None:
        if os.geteuid() == 0:
            raise unittest.SkipTest("root ignores directory permissions")

    def test_an_unreadable_directory_is_an_error_naming_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_text(note("alpha"), "utf-8")
            (root / "projects").chmod(0o000)
            try:
                with self.assertRaises(GuardError) as caught:
                    check_tree(root)
            finally:
                (root / "projects").chmod(0o755)
            self.assertIn("projects", str(caught.exception))

    def test_an_unreadable_tree_root_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault"
            make_tree(root)
            root.chmod(0o300)
            try:
                with self.assertRaises(GuardError):
                    check_tree(root)
            finally:
                root.chmod(0o755)


class IndexAgreementTests(unittest.TestCase):
    """A tree that passes the guards has to be a tree that indexes."""

    def test_flags_an_index_artifact_that_is_not_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "index.json").mkdir()
            findings = check_tree(root)
            self.assertEqual([item.rule for item in findings], ["index"])
            self.assertIn("index.json", findings[0].detail)

    def test_flags_a_tree_root_that_cannot_be_written_to(self) -> None:
        if os.geteuid() == 0:
            raise unittest.SkipTest("root ignores directory permissions")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault"
            make_tree(root)
            root.chmod(0o500)
            try:
                rules = [item.rule for item in check_tree(root)]
            finally:
                root.chmod(0o755)
            self.assertIn("index", rules)

    def test_reports_an_index_over_its_character_cap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            for number in range(60):
                (root / "projects" / f"note-{number:03d}.md").write_text(
                    note(f"note-{number:03d}"), encoding="utf-8"
                )
            with mock.patch.object(index_module, "INDEX_CAP", 800):
                findings = check_tree(root)
            self.assertTrue(findings)
            self.assertEqual({item.rule for item in findings}, {"index_cap"})
            details = " ".join(item.detail for item in findings)
            self.assertIn("index.json is ", details)
            self.assertIn("INDEX.md is ", details)

    def test_an_index_within_cap_is_not_reported(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_text(note("alpha"), "utf-8")
            self.assertEqual(check_tree(root), [])


class FreshCloneTests(unittest.TestCase):
    """Cover the two git facts the hook design rests on.

    A clone carries the working tree, so a committed ``.githooks/`` arrives
    with it. A clone never carries ``.git/hooks``, so the arriving hook is
    inert until ``core.hooksPath`` points at it. Bootstrap has to set that
    configuration or the guard silently does nothing in a fresh clone.

    The fixture hook refuses every commit, so its effect is visible: a commit
    that succeeds proves the hook did not run, and a commit that fails with the
    hook's own message proves it did.
    """

    refusal = "the committed hook refused this commit"

    def setUp(self) -> None:
        if shutil.which("git") is None:
            raise unittest.SkipTest("git is not installed")

    def git(self, *arguments: str, cwd: Path) -> subprocess.CompletedProcess:
        environment = dict(os.environ)
        environment.update(
            {
                "GIT_AUTHOR_NAME": "vault test",
                "GIT_AUTHOR_EMAIL": "vault@example.com",
                "GIT_COMMITTER_NAME": "vault test",
                "GIT_COMMITTER_EMAIL": "vault@example.com",
                # Keep the machine's own git configuration out of the result.
                # A global core.hooksPath or template directory would decide
                # the outcome this test is measuring.
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
            }
        )
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

    def run_git(self, *arguments: str, cwd: Path) -> subprocess.CompletedProcess:
        result = self.git(*arguments, cwd=cwd)
        if result.returncode != 0:
            self.fail(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
        return result

    def origin_with_a_committed_hook(self, base: Path) -> Path:
        origin = base / "origin"
        make_tree(origin)
        hooks = origin / ".githooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text(
            f"#!/bin/sh\necho '{self.refusal}' >&2\nexit 1\n", encoding="utf-8"
        )
        hook.chmod(0o755)
        (origin / "projects" / "seed.md").write_text(note("seed"), encoding="utf-8")
        self.run_git("init", "-b", "main", cwd=origin)
        self.run_git("add", "-A", cwd=origin)
        self.run_git("commit", "-m", "init", cwd=origin)
        return origin

    def commit(self, clone: Path, name: str) -> subprocess.CompletedProcess:
        target = clone / "projects" / f"{name}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(note(name), encoding="utf-8")
        self.run_git("add", "-A", cwd=clone)
        return self.git("commit", "-m", name, cwd=clone)

    def test_a_committed_hook_survives_a_clone(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            origin = self.origin_with_a_committed_hook(base)
            clone = base / "clone"
            self.run_git("clone", str(origin), str(clone), cwd=base)

            self.assertTrue((clone / ".githooks" / "pre-commit").is_file())
            self.assertFalse((clone / ".git" / "hooks" / "pre-commit").exists())

    def test_the_cloned_hook_is_inert_until_hookspath_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            origin = self.origin_with_a_committed_hook(base)
            clone = base / "clone"
            self.run_git("clone", str(origin), str(clone), cwd=base)

            before = self.commit(clone, "alpha")
            self.assertEqual(
                before.returncode,
                0,
                "a cloned hook ran without core.hooksPath being set",
            )
            self.assertNotIn(self.refusal, before.stderr)

            self.run_git(
                "config", "--local", "core.hooksPath", ".githooks", cwd=clone
            )
            after = self.commit(clone, "beta")
            self.assertNotEqual(
                after.returncode, 0, "the hook did not run after core.hooksPath"
            )
            self.assertIn(self.refusal, after.stderr)

if __name__ == "__main__":
    unittest.main()
