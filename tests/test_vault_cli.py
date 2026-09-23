from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_efficiency.cli import main
from agent_efficiency.vault import index as index_module
from vault_fixtures import make_repo, make_vault_tree, note_text

DIRECTORY_NAMES = ("projects", "feedback", "reference", "doctrine", "sessions")


def run(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(argv)
    return code, buffer.getvalue()


def run_both(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def marker(root: Path) -> dict:
    return json.loads((root / ".vault.json").read_text(encoding="utf-8"))


def note(id_: str, classification: str = "core") -> str:
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
        "Body.\n"
    )


class VaultInitTests(unittest.TestCase):
    def test_init_creates_a_tree_and_index(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-work"
            code, _ = run(
                [
                    "vault",
                    "init",
                    str(root),
                    "--classification",
                    "work",
                    "--remote",
                    "git@git.example.com:example/vault-work.git",
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(marker(root)["classification"], "work")
            self.assertEqual(
                marker(root)["remote_url"], "git@git.example.com:example/vault-work.git"
            )
            for name in (*DIRECTORY_NAMES, ".githooks"):
                self.assertTrue((root / name).is_dir(), name)
            self.assertTrue((root / "index.json").is_file())
            self.assertTrue((root / "INDEX.md").is_file())

    def test_init_writes_executable_git_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(["vault", "init", str(root), "--classification", "core"])
            for name in ("pre-commit", "pre-push"):
                hook = root / ".githooks" / name
                mode = stat.S_IMODE(hook.stat().st_mode)
                self.assertEqual(mode & 0o500, 0o500, f"{name} mode {mode:o}")
                self.assertTrue(os.access(hook, os.X_OK), name)
                self.assertTrue(
                    hook.read_text(encoding="utf-8").startswith("#!/bin/sh")
                )

    def test_hooks_invoke_the_check_command(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(["vault", "init", str(root), "--classification", "core"])
            hooks = root / ".githooks"
            self.assertIn(
                "vault check", (hooks / "pre-commit").read_text(encoding="utf-8")
            )
            push = (hooks / "pre-push").read_text(encoding="utf-8")
            self.assertIn("vault check", push)
            self.assertIn("--remote", push)

    def test_hooks_check_git_content_rather_than_the_working_copy(self) -> None:
        """The flags are the difference between checking a commit and a folder."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(["vault", "init", str(root), "--classification", "core"])
            hooks = root / ".githooks"
            self.assertIn(
                "--staged", (hooks / "pre-commit").read_text(encoding="utf-8")
            )
            self.assertIn("--push", (hooks / "pre-push").read_text(encoding="utf-8"))

    def test_init_twice_keeps_existing_notes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(["vault", "init", str(root), "--classification", "core"])
            kept = root / "projects" / "alpha.md"
            kept.write_text(note("alpha"), encoding="utf-8")
            code, _ = run(["vault", "init", str(root), "--classification", "core"])
            self.assertEqual(code, 0)
            self.assertEqual(kept.read_text(encoding="utf-8"), note("alpha"))
            self.assertIn(
                "alpha title", (root / "INDEX.md").read_text(encoding="utf-8")
            )

    def test_init_refuses_to_reclassify_an_existing_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(["vault", "init", str(root), "--classification", "core"])
            code, _, err = run_both(
                ["vault", "init", str(root), "--classification", "work"]
            )
            self.assertEqual(code, 2)
            self.assertIn("reclassify", err)
            self.assertIn("'core'", err)
            self.assertEqual(marker(root)["classification"], "core")

    def test_init_refuses_to_change_a_recorded_remote(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(
                [
                    "vault",
                    "init",
                    str(root),
                    "--classification",
                    "core",
                    "--remote",
                    "git@git.example.com:example/vault-core.git",
                ]
            )
            code, _, err = run_both(
                [
                    "vault",
                    "init",
                    str(root),
                    "--classification",
                    "core",
                    "--remote",
                    "git@git.example.com:someone/other.git",
                ]
            )
            self.assertEqual(code, 2)
            self.assertIn("remote", err)
            self.assertEqual(
                marker(root)["remote_url"], "git@git.example.com:example/vault-core.git"
            )

    def test_init_keeps_a_recorded_remote_when_rerun_without_one(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(
                [
                    "vault",
                    "init",
                    str(root),
                    "--classification",
                    "core",
                    "--remote",
                    "git@git.example.com:example/vault-core.git",
                ]
            )
            code, _ = run(["vault", "init", str(root), "--classification", "core"])
            self.assertEqual(code, 0)
            self.assertEqual(
                marker(root)["remote_url"], "git@git.example.com:example/vault-core.git"
            )

    def test_init_refuses_a_marker_it_cannot_read(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            root.mkdir()
            (root / ".vault.json").write_text("{not json", encoding="utf-8")
            code, _, err = run_both(
                ["vault", "init", str(root), "--classification", "core"]
            )
            self.assertEqual(code, 2)
            self.assertTrue(err.strip())


class VaultCheckTests(unittest.TestCase):
    def test_check_returns_zero_for_a_clean_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(["vault", "init", str(root), "--classification", "core"])
            code, output = run(["vault", "check", str(root), "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["findings"], [])

    def test_check_returns_one_and_names_the_rule_on_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(["vault", "init", str(root), "--classification", "core"])
            (root / "projects" / "bad.md").write_text(
                note("bad", "work"), encoding="utf-8"
            )
            code, output = run(["vault", "check", str(root), "--json"])
            self.assertEqual(code, 1)
            rules = [item["rule"] for item in json.loads(output)["findings"]]
            self.assertIn("classification", rules)

    def test_check_text_output_names_the_rule_and_the_fix(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(["vault", "init", str(root), "--classification", "core"])
            (root / "projects" / "bad.md").write_text(
                note("bad", "work"), encoding="utf-8"
            )
            code, output = run(["vault", "check", str(root)])
            self.assertEqual(code, 1)
            self.assertIn("classification", output)
            self.assertIn("projects/bad.md", output)
            self.assertIn("fix:", output)

    def test_check_reports_a_wrong_push_destination(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(
                [
                    "vault",
                    "init",
                    str(root),
                    "--classification",
                    "core",
                    "--remote",
                    "git@git.example.com:example/vault-core.git",
                ]
            )
            code, output = run(
                [
                    "vault",
                    "check",
                    str(root),
                    "--remote",
                    "git@git.example.com:someone/other.git",
                    "--json",
                ]
            )
            self.assertEqual(code, 1)
            rules = [item["rule"] for item in json.loads(output)["findings"]]
            self.assertEqual(rules, ["destination"])

    def test_check_returns_two_for_a_path_that_is_not_a_vault(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            plain = Path(raw) / "plain"
            plain.mkdir()
            code, _, err = run_both(["vault", "check", str(plain)])
            self.assertEqual(code, 2)
            self.assertIn(".vault.json", err)

    def test_check_json_reports_an_error_on_stdout_for_a_machine(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            plain = Path(raw) / "plain"
            plain.mkdir()
            code, output, _ = run_both(["vault", "check", str(plain), "--json"])
            self.assertEqual(code, 2)
            payload = json.loads(output)
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["error"])


class VaultIndexTests(unittest.TestCase):
    def test_index_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(["vault", "init", str(root), "--classification", "core"])
            first = (root / "index.json").read_text(encoding="utf-8")
            code, _ = run(["vault", "index", str(root)])
            self.assertEqual(code, 0)
            self.assertEqual((root / "index.json").read_text(encoding="utf-8"), first)

    def test_index_picks_up_a_new_note(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(["vault", "init", str(root), "--classification", "core"])
            (root / "projects" / "alpha.md").write_text(
                note("alpha"), encoding="utf-8"
            )
            run(["vault", "index", str(root)])
            payload = json.loads((root / "index.json").read_text(encoding="utf-8"))
            self.assertEqual([item["id"] for item in payload["notes"]], ["alpha"])

    def test_index_returns_two_for_a_note_it_cannot_parse(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(["vault", "init", str(root), "--classification", "core"])
            before = (root / "index.json").read_text(encoding="utf-8")
            (root / "projects" / "broken.md").write_text(
                "no frontmatter\n", encoding="utf-8"
            )
            code, _, err = run_both(["vault", "index", str(root)])
            self.assertEqual(code, 2)
            self.assertIn("broken.md", err)
            self.assertEqual(
                (root / "index.json").read_text(encoding="utf-8"), before
            )


class VaultCheckSourceTests(unittest.TestCase):
    """The failure modes of the two git-reading modes reach a person as text."""

    def test_staged_outside_a_repository_reports_rather_than_raises(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(["vault", "init", str(root), "--classification", "core"])
            code, _, err = run_both(["vault", "check", str(root), "--staged"])
            self.assertEqual(code, 2)
            self.assertTrue(err.strip())

    def test_push_outside_a_repository_reports_rather_than_raises(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(["vault", "init", str(root), "--classification", "core"])
            with mock.patch("sys.stdin", io.StringIO("")):
                code, _, err = run_both(["vault", "check", str(root), "--push"])
            self.assertEqual(code, 2)
            self.assertTrue(err.strip())

    def test_staged_and_push_cannot_be_asked_for_together(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(["vault", "init", str(root), "--classification", "core"])
            with self.assertRaises(SystemExit):
                run_both(["vault", "check", str(root), "--staged", "--push"])

    def test_a_malformed_remote_is_reported_rather_than_raised(self) -> None:
        """An invalid port used to leave the CLI as a traceback."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(
                [
                    "vault",
                    "init",
                    str(root),
                    "--classification",
                    "core",
                    "--remote",
                    "https://git.example.com/example/vault-core",
                ]
            )
            for bad in ("https://git.example.com:no/example/vault-core",):
                code, _, err = run_both(
                    ["vault", "check", str(root), "--remote", bad]
                )
                self.assertEqual(code, 2, bad)
                self.assertIn("port", err)

    def test_a_malformed_remote_is_reported_in_json_too(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(["vault", "init", str(root), "--classification", "core"])
            code, output = run(
                [
                    "vault",
                    "check",
                    str(root),
                    "--json",
                    "--remote",
                    "https://git.example.com:no/example/vault-core",
                ]
            )
            self.assertEqual(code, 2)
            self.assertFalse(json.loads(output)["ok"])

    def test_an_init_that_would_compare_a_malformed_remote_reports(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(
                [
                    "vault",
                    "init",
                    str(root),
                    "--classification",
                    "core",
                    "--remote",
                    "https://git.example.com/example/vault-core",
                ]
            )
            code, _, err = run_both(
                [
                    "vault",
                    "init",
                    str(root),
                    "--classification",
                    "core",
                    "--remote",
                    "https://git.example.com:no/example/vault-core",
                ]
            )
            self.assertEqual(code, 2)
            self.assertTrue(err.strip())


class VaultIndexCapTests(unittest.TestCase):
    def test_index_reports_an_artifact_over_its_character_cap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(["vault", "init", str(root), "--classification", "core"])
            for number in range(60):
                (root / "projects" / f"note-{number:03d}.md").write_text(
                    note(f"note-{number:03d}"), encoding="utf-8"
                )
            with mock.patch.object(index_module, "INDEX_CAP", 800):
                code, output = run(["vault", "index", str(root)])
            self.assertEqual(code, 0)
            self.assertIn("over cap", output)
            self.assertIn("index.json", output)
            written = json.loads((root / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(len(written["notes"]), 60)

    def test_a_small_tree_reports_no_cap_message(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "vault-core"
            run(["vault", "init", str(root), "--classification", "core"])
            (root / "projects" / "alpha.md").write_text(note("alpha"), "utf-8")
            code, output = run(["vault", "index", str(root)])
            self.assertEqual(code, 0)
            self.assertNotIn("over cap", output)


class VaultUsageTests(unittest.TestCase):
    def test_vault_without_a_subcommand_is_a_usage_error(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            main(["vault"])
        self.assertEqual(caught.exception.code, 2)

    def test_an_unknown_classification_is_a_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as caught,
            ):
                main(
                    [
                        "vault",
                        "init",
                        str(Path(raw) / "tree"),
                        "--classification",
                        "public",
                    ]
                )
            self.assertEqual(caught.exception.code, 2)

    def test_check_without_a_root_is_a_usage_error(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            main(["vault", "check"])
        self.assertEqual(caught.exception.code, 2)


class VaultNeedsNoDatabaseTests(unittest.TestCase):
    def test_vault_commands_run_with_an_unusable_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            blocker = Path(raw) / "blocker"
            blocker.write_text("not a directory\n", encoding="utf-8")
            unusable = blocker / "data"
            root = Path(raw) / "vault-core"

            # Proof the data directory really is unusable: any command that
            # builds the Store fails on it.
            with mock.patch.dict(
                os.environ, {"AGENT_EFFICIENCY_DATA": str(unusable)}
            ):
                with self.assertRaises(OSError):
                    main(["status"])

                self.assertEqual(
                    run(["vault", "init", str(root), "--classification", "core"])[0],
                    0,
                )
                self.assertEqual(run(["vault", "index", str(root)])[0], 0)
                self.assertEqual(run(["vault", "check", str(root)])[0], 0)



class VaultMigrateTests(unittest.TestCase):
    def memory(self, base: Path) -> Path:
        source = base / "memory"
        source.mkdir()
        (source / "project_umbra.md").write_text(
            "---\nname: umbra\n---\numbra engine, client work.\n",
            encoding="utf-8",
        )
        return source

    def drafted(self, base: Path) -> Path:
        out = base / "proposal.json"
        code, _ = run(
            [
                "vault",
                "migrate",
                "propose",
                "--memory-dir",
                str(self.memory(base)),
                "--out",
                str(out),
            ]
        )
        self.assertEqual(code, 0)
        return out

    def test_migrate_propose_then_apply(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            work = base / "vault-work"
            run(["vault", "init", str(work), "--classification", "work"])
            out = self.drafted(base)

            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["entries"][0]["classification"], "work")

            code, output = run(
                ["vault", "migrate", "apply", str(out), "--tree", f"work={work}"]
            )
            self.assertEqual(code, 0)
            self.assertIn("umbra.md", (work / "INDEX.md").read_text(encoding="utf-8"))
            self.assertTrue((work / "projects" / "umbra.md").is_file())
            self.assertTrue(output.strip())
            self.assertEqual(run(["vault", "check", str(work), "--json"])[0], 0)

    def test_propose_tells_the_reader_to_review_the_classifications(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            out = base / "proposal.json"
            code, output = run(
                [
                    "vault",
                    "migrate",
                    "propose",
                    "--memory-dir",
                    str(self.memory(base)),
                    "--out",
                    str(out),
                ]
            )
            self.assertEqual(code, 0)
            self.assertIn("classification", output.lower())

    def test_propose_refuses_to_replace_an_edited_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            out = self.drafted(base)
            edited = '{"schema": 1, "entries": []}\n'
            out.write_text(edited, encoding="utf-8")
            code, _, err = run_both(
                [
                    "vault",
                    "migrate",
                    "propose",
                    "--memory-dir",
                    str(base / "memory"),
                    "--out",
                    str(out),
                ]
            )
            self.assertEqual(code, 2)
            self.assertIn(str(out), err)
            self.assertEqual(out.read_text(encoding="utf-8"), edited)

    def test_propose_replaces_the_proposal_when_forced(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            out = self.drafted(base)
            out.write_text('{"schema": 1, "entries": []}\n', encoding="utf-8")
            code, _ = run(
                [
                    "vault",
                    "migrate",
                    "propose",
                    "--memory-dir",
                    str(base / "memory"),
                    "--out",
                    str(out),
                    "--force",
                ]
            )
            self.assertEqual(code, 0)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["entries"]), 1)

    def test_propose_needs_a_source_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "proposal.json"
            code, _, err = run_both(
                ["vault", "migrate", "propose", "--out", str(out)]
            )
            self.assertEqual(code, 2)
            self.assertTrue(err.strip())
            self.assertFalse(out.exists())

    def test_propose_reports_a_source_directory_that_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            missing = base / "nowhere"
            out = base / "proposal.json"
            code, _, err = run_both(
                [
                    "vault",
                    "migrate",
                    "propose",
                    "--memory-dir",
                    str(missing),
                    "--out",
                    str(out),
                ]
            )
            self.assertEqual(code, 2)
            self.assertIn(str(missing), err)
            self.assertFalse(out.exists())

    def test_apply_rejects_a_tree_argument_without_a_classification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            work = base / "vault-work"
            run(["vault", "init", str(work), "--classification", "work"])
            out = self.drafted(base)
            code, _, err = run_both(
                ["vault", "migrate", "apply", str(out), "--tree", str(work)]
            )
            self.assertEqual(code, 2)
            self.assertIn("CLASSIFICATION=PATH", err)
            self.assertFalse((work / "projects" / "umbra.md").exists())

    def test_apply_rejects_a_repeated_classification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            first = base / "vault-work"
            second = base / "vault-work-two"
            for root in (first, second):
                run(["vault", "init", str(root), "--classification", "work"])
            out = self.drafted(base)
            code, _, err = run_both(
                [
                    "vault",
                    "migrate",
                    "apply",
                    str(out),
                    "--tree",
                    f"work={first}",
                    "--tree",
                    f"work={second}",
                ]
            )
            self.assertEqual(code, 2)
            self.assertIn("work", err)
            for root in (first, second):
                self.assertFalse((root / "projects" / "umbra.md").exists())

    def test_apply_rejects_an_unknown_classification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            work = base / "vault-work"
            run(["vault", "init", str(work), "--classification", "work"])
            out = self.drafted(base)
            code, _, err = run_both(
                ["vault", "migrate", "apply", str(out), "--tree", f"public={work}"]
            )
            self.assertEqual(code, 2)
            self.assertIn("public", err)

    def test_apply_rejects_a_path_that_is_not_a_vault_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            plain = base / "plain"
            plain.mkdir()
            out = self.drafted(base)
            code, _, err = run_both(
                ["vault", "migrate", "apply", str(out), "--tree", f"work={plain}"]
            )
            self.assertEqual(code, 2)
            self.assertIn(".vault.json", err)

    def test_apply_rejects_a_tree_of_another_classification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            core = base / "vault-core"
            run(["vault", "init", str(core), "--classification", "core"])
            out = self.drafted(base)
            code, _, err = run_both(
                ["vault", "migrate", "apply", str(out), "--tree", f"work={core}"]
            )
            self.assertEqual(code, 2)
            self.assertIn("core", err)
            self.assertFalse((core / "projects" / "umbra.md").exists())

    def test_apply_reports_a_proposal_that_is_not_json(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            work = base / "vault-work"
            run(["vault", "init", str(work), "--classification", "work"])
            out = base / "proposal.json"
            out.write_text("{not json", encoding="utf-8")
            code, _, err = run_both(
                ["vault", "migrate", "apply", str(out), "--tree", f"work={work}"]
            )
            self.assertEqual(code, 2)
            self.assertIn(str(out), err)

    def test_apply_reports_a_proposal_file_that_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            work = base / "vault-work"
            run(["vault", "init", str(work), "--classification", "work"])
            missing = base / "proposal.json"
            code, _, err = run_both(
                ["vault", "migrate", "apply", str(missing), "--tree", f"work={work}"]
            )
            self.assertEqual(code, 2)
            self.assertIn(str(missing), err)

    def test_apply_refuses_to_replace_notes_unless_forced(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            work = base / "vault-work"
            run(["vault", "init", str(work), "--classification", "work"])
            out = self.drafted(base)
            note_path = work / "projects" / "umbra.md"
            run(["vault", "migrate", "apply", str(out), "--tree", f"work={work}"])
            note_path.write_text(
                note_path.read_text(encoding="utf-8").replace(
                    "hook: migrated from", "hook: edited by hand,"
                ),
                encoding="utf-8",
            )
            edited = note_path.read_text(encoding="utf-8")

            code, _, err = run_both(
                ["vault", "migrate", "apply", str(out), "--tree", f"work={work}"]
            )
            self.assertEqual(code, 2)
            self.assertIn("umbra.md", err)
            self.assertEqual(note_path.read_text(encoding="utf-8"), edited)

            code, _ = run(
                [
                    "vault",
                    "migrate",
                    "apply",
                    str(out),
                    "--tree",
                    f"work={work}",
                    "--force",
                ]
            )
            self.assertEqual(code, 0)
            self.assertNotEqual(note_path.read_text(encoding="utf-8"), edited)

    def test_migrate_without_a_subcommand_is_a_usage_error(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            main(["vault", "migrate"])
        self.assertEqual(caught.exception.code, 2)


class VaultRegisterAndShowTests(unittest.TestCase):
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
            [note_text("writing", note_type="feedback", classification="core")],
        )
        self.private = make_vault_tree(
            self.base / "vault-private",
            "private",
            [note_text("catalog", repos=("example/catalog",), body="Next: API slice.")],
        )

    def cli(self, *args: str) -> list[str]:
        return ["--data-dir", str(self.data), "vault", *args]

    def test_register_then_show_prints_the_session_context(self) -> None:
        for tree in (self.core, self.private):
            code, out = run(self.cli("register", str(tree)))
            self.assertEqual(code, 0)
            self.assertIn("Registered the", out)
        code, out = run(self.cli("show", "--cwd", str(self.project)))
        self.assertEqual(code, 0)
        self.assertIn("Next: API slice.", out)
        self.assertIn("Summary: matched |", out)

    def test_register_reports_a_stale_entry_it_drops(self) -> None:
        run(self.cli("register", str(self.core)))
        moved = self.base / "moved-core"
        self.core.rename(moved)
        code, out = run(self.cli("register", str(self.private)))
        self.assertEqual(code, 0)
        self.assertIn("Dropped a registered tree that no longer exists", out)

    def test_register_reports_an_unwritable_tree_list(self) -> None:
        with mock.patch(
            "agent_efficiency.vault.config._write",
            side_effect=PermissionError(13, "Permission denied"),
        ):
            code, out, err = run_both(self.cli("register", str(self.core)))
        self.assertEqual(code, 2)
        self.assertIn(str(self.data / "vault.json"), err)
        self.assertIn("Permission denied", err)
        self.assertNotIn("Traceback", out + err)

    def test_register_refuses_a_directory_that_is_not_a_tree(self) -> None:
        code, _, err = run_both(self.cli("register", str(self.base)))
        self.assertEqual(code, 2)
        self.assertIn(".vault.json", err)

    def test_show_without_trees_says_so(self) -> None:
        code, out = run(self.cli("show", "--cwd", str(self.project)))
        self.assertEqual(code, 0)
        self.assertIn("No vault context: unavailable (no_trees)", out)

    def test_show_says_why_a_moved_tree_degrades_the_context(self) -> None:
        run(self.cli("register", str(self.core)))
        self.core.rename(self.base / "moved-core")
        code, out, err = run_both(self.cli("show", "--cwd", str(self.project)))
        self.assertEqual(code, 2)
        self.assertIn("No vault context: degraded (parse_error)", out)
        self.assertIn("Fix or remove the entry in", out)
        self.assertNotIn("Traceback", out + err)

    def test_show_reports_an_unreadable_config_path(self) -> None:
        run(self.cli("register", str(self.core)))
        config = self.data / "vault.json"
        original = Path.is_file

        def is_file(path: Path) -> bool:
            if path == config:
                raise PermissionError(13, "Permission denied", str(path))
            return original(path)

        with mock.patch.object(Path, "is_file", is_file):
            code, out, err = run_both(self.cli("show", "--cwd", str(self.project)))
        self.assertEqual(code, 2)
        self.assertIn("No vault context: degraded (parse_error)", out)
        self.assertNotIn("Traceback", out + err)

    def test_show_reports_an_unresolvable_working_directory(self) -> None:
        run(self.cli("register", str(self.core)))
        original = Path.resolve

        def resolve(path: Path, strict: bool = False) -> Path:
            if path == self.project:
                raise RuntimeError("Symlink loop from the working directory")
            return original(path, strict=strict)

        with mock.patch.object(Path, "resolve", resolve):
            code, out, err = run_both(self.cli("show", "--cwd", str(self.project)))
        self.assertEqual(code, 2)
        self.assertIn("No vault context: degraded (parse_error)", out)
        self.assertNotIn("Traceback", out + err)


if __name__ == "__main__":
    unittest.main()
