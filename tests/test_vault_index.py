from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_efficiency.vault import index as index_module
from agent_efficiency.vault.index import (
    INDEX_CAP,
    TYPE_ORDER,
    IndexError_,
    build_from_notes,
    build_index,
    cap_overflow,
    is_index_artifact,
    is_note_path,
    iter_note_paths,
    preflight,
    render_markdown,
    render_payload,
    write_index,
)
from agent_efficiency.vault.schema import TYPES

EM_DASH = "—"


def make_tree(root: Path, classification: str = "work") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".vault.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "classification": classification,
                "preferred_remote": "origin",
                "remote_url": "git@git.example.com:example/vault-work.git",
            }
        ),
        encoding="utf-8",
    )
    (root / "projects").mkdir(exist_ok=True)
    (root / "feedback").mkdir(exist_ok=True)


def note(
    id_: str,
    type_: str,
    status: str = "active",
    classification: str = "work",
    body: str = "Body.",
) -> str:
    return (
        "---\n"
        "schema: 1\n"
        f"id: {id_}\n"
        f"title: {id_} title\n"
        f"type: {type_}\n"
        f"classification: {classification}\n"
        f"status: {status}\n"
        "updated: 2026-09-21\n"
        f"hook: {id_} hook line\n"
        "---\n"
        f"{body}\n"
    )


def leftovers(root: Path) -> list[str]:
    """Every file in the tree root that is not an expected artifact."""

    expected = {".vault.json", "INDEX.md", "index.json", "projects", "feedback"}
    return sorted(item.name for item in root.iterdir() if item.name not in expected)


class NotePathTests(unittest.TestCase):
    def test_excludes_every_archive_directory_at_any_depth(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_text(
                note("alpha", "project"), encoding="utf-8"
            )
            shallow = root / "archive"
            shallow.mkdir()
            (shallow / "old.md").write_text(note("old", "project"), encoding="utf-8")
            deep = root / "projects" / "alpha" / "archive" / "2026"
            deep.mkdir(parents=True)
            (deep / "older.md").write_text(note("older", "project"), encoding="utf-8")

            found = [path.name for path in iter_note_paths(root)]
            self.assertEqual(found, ["alpha.md"])

    def test_excludes_the_generated_index_at_any_depth(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "INDEX.md").write_text("# not a note\n", encoding="utf-8")
            (root / "projects" / "INDEX.md").write_text("# nested\n", encoding="utf-8")
            (root / "projects" / "alpha.md").write_text(
                note("alpha", "project"), encoding="utf-8"
            )

            found = [path.name for path in iter_note_paths(root)]
            self.assertEqual(found, ["alpha.md"])

    def test_keeps_a_lowercase_index_note_because_the_rule_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "index.md").write_text(
                note("index", "project"), encoding="utf-8"
            )

            found = [path.name for path in iter_note_paths(root)]
            self.assertEqual(found, ["index.md"])

    def test_excludes_dot_directories_and_dot_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            hooks = root / ".githooks"
            hooks.mkdir()
            (hooks / "notes.md").write_text(note("hook", "project"), encoding="utf-8")
            (root / ".draft.md").write_text(note("draft", "project"), encoding="utf-8")
            (root / "projects" / "alpha.md").write_text(
                note("alpha", "project"), encoding="utf-8"
            )

            found = [path.name for path in iter_note_paths(root)]
            self.assertEqual(found, ["alpha.md"])

    def test_returns_paths_in_sorted_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            for name in ("zeta", "alpha", "mid"):
                (root / "projects" / f"{name}.md").write_text(
                    note(name, "project"), encoding="utf-8"
                )

            found = [path.name for path in iter_note_paths(root)]
            self.assertEqual(found, ["alpha.md", "mid.md", "zeta.md"])


class IndexTests(unittest.TestCase):
    def test_orders_notes_deterministically_and_excludes_archives(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "zeta.md").write_text(
                note("zeta", "project"), encoding="utf-8"
            )
            (root / "projects" / "alpha.md").write_text(
                note("alpha", "project"), encoding="utf-8"
            )
            (root / "feedback" / "plain.md").write_text(
                note("plain", "feedback"), encoding="utf-8"
            )
            archive = root / "projects" / "alpha" / "archive"
            archive.mkdir(parents=True)
            (archive / "0001.md").write_text(note("old", "project"), encoding="utf-8")

            index = build_index(root)
            ids = [entry["id"] for entry in index["notes"]]
            self.assertEqual(ids, ["plain", "alpha", "zeta"])
            self.assertNotIn("old", ids)

    def test_entry_carries_the_fields_the_runtime_reads(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            text = note("alpha", "project").replace(
                "hook: alpha hook line\n",
                "hook: alpha hook line\nrepos: [owner/one]\nbranches: [main]\n",
            )
            (root / "projects" / "alpha.md").write_text(text, encoding="utf-8")

            index = build_index(root)
            self.assertEqual(index["schema"], 1)
            self.assertEqual(index["classification"], "work")
            entry = index["notes"][0]
            self.assertEqual(entry["path"], "projects/alpha.md")
            self.assertEqual(entry["repos"], ["owner/one"])
            self.assertEqual(entry["branches"], ["main"])
            self.assertEqual(entry["paths"], [])
            self.assertEqual(entry["size"], len(text))
            self.assertTrue(entry["within_cap"])
            self.assertEqual(entry["status"], "active")
            self.assertEqual(entry["updated"], "2026-09-21")

    def test_build_is_stable_across_runs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_text(
                note("alpha", "project"), encoding="utf-8"
            )
            self.assertEqual(build_index(root), build_index(root))

    def test_an_empty_tree_builds_an_empty_note_list(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            self.assertEqual(build_index(root)["notes"], [])

    def test_builds_from_a_nested_directory_inside_the_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_text(
                note("alpha", "project"), encoding="utf-8"
            )
            self.assertEqual(build_index(root / "projects"), build_index(root))

    def test_rejects_duplicate_ids(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "one.md").write_text(
                note("same", "project"), encoding="utf-8"
            )
            (root / "projects" / "two.md").write_text(
                note("same", "project"), encoding="utf-8"
            )
            with self.assertRaises(IndexError_) as caught:
                build_index(root)
            self.assertIn("same", str(caught.exception))

    def test_ids_that_differ_only_in_case_stay_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "lower.md").write_text(
                note("alpha", "project"), encoding="utf-8"
            )
            (root / "projects" / "upper.md").write_text(
                note("Alpha", "project"), encoding="utf-8"
            )
            ids = [entry["id"] for entry in build_index(root)["notes"]]
            self.assertEqual(sorted(ids), ["Alpha", "alpha"])

    def test_rejects_a_note_whose_classification_differs_from_the_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            wrong = note("alpha", "project", classification="core")
            (root / "projects" / "alpha.md").write_text(wrong, encoding="utf-8")
            with self.assertRaises(IndexError_) as caught:
                build_index(root)
            self.assertIn("alpha.md", str(caught.exception))

    def test_rejects_malformed_frontmatter_and_names_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "broken.md").write_text(
                "no frontmatter here\n", encoding="utf-8"
            )
            with self.assertRaises(IndexError_) as caught:
                build_index(root)
            self.assertIn("broken.md", str(caught.exception))

    def test_rejects_a_directory_named_like_a_note(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").mkdir()
            with self.assertRaises(IndexError_) as caught:
                build_index(root)
            self.assertIn("alpha.md", str(caught.exception))

    def test_rejects_a_broken_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").symlink_to(root / "missing-target.md")
            with self.assertRaises(IndexError_) as caught:
                build_index(root)
            self.assertIn("alpha.md", str(caught.exception))

    def test_rejects_a_note_that_is_not_valid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_bytes(b"---\nid: \xff\xfe\n---\n")
            with self.assertRaises(IndexError_) as caught:
                build_index(root)
            self.assertIn("alpha.md", str(caught.exception))
            self.assertIn("UTF-8", str(caught.exception))

    def test_rejects_a_directory_that_is_not_a_vault_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "plain"
            root.mkdir()
            with self.assertRaises(IndexError_):
                build_index(root)

    def test_type_order_covers_every_schema_type(self) -> None:
        self.assertEqual(sorted(TYPE_ORDER), sorted(TYPES))


class RenderTests(unittest.TestCase):
    def test_uses_a_colon_and_never_an_em_dash(self) -> None:
        index = {
            "schema": 1,
            "classification": "work",
            "notes": [
                {"title": "alpha title", "path": "projects/alpha.md", "hook": "state"}
            ],
        }
        markdown = render_markdown(index)
        self.assertIn("- [alpha title](projects/alpha.md): state", markdown)
        self.assertNotIn(EM_DASH, markdown)

    def test_percent_encodes_a_path_that_would_break_the_link(self) -> None:
        index = {
            "schema": 1,
            "classification": "work",
            "notes": [
                {"title": "spaced", "path": "projects/a note (draft).md", "hook": "h"}
            ],
        }
        markdown = render_markdown(index)
        self.assertIn("projects/a%20note%20%28draft%29.md", markdown)
        self.assertNotIn("(draft)", markdown)

    def test_renders_an_empty_tree_without_a_dangling_list(self) -> None:
        markdown = render_markdown(
            {"schema": 1, "classification": "core", "notes": []}
        )
        self.assertIn("# Vault index (core)", markdown)
        self.assertNotIn("- [", markdown)
        self.assertTrue(markdown.endswith("\n"))


class WriteIndexTests(unittest.TestCase):
    def test_write_index_emits_both_files_and_leaves_no_temp(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_text(
                note("alpha", "project"), encoding="utf-8"
            )
            write_index(root)
            self.assertTrue((root / "index.json").is_file())
            markdown = (root / "INDEX.md").read_text(encoding="utf-8")
            self.assertIn("alpha hook line", markdown)
            self.assertNotIn(EM_DASH, markdown)
            self.assertEqual(list(root.glob("*.tmp")), [])
            self.assertEqual(leftovers(root), [])

    def test_two_runs_produce_byte_identical_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_text(
                note("alpha", "project"), encoding="utf-8"
            )
            write_index(root)
            first_json = (root / "index.json").read_bytes()
            first_markdown = (root / "INDEX.md").read_bytes()
            write_index(root)
            self.assertEqual((root / "index.json").read_bytes(), first_json)
            self.assertEqual((root / "INDEX.md").read_bytes(), first_markdown)

    def test_written_json_round_trips_to_the_built_index(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_text(
                note("alpha", "project"), encoding="utf-8"
            )
            index = write_index(root)
            written = json.loads((root / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(written, index)

    def test_replaces_an_earlier_index_rather_than_appending(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_text(
                note("alpha", "project"), encoding="utf-8"
            )
            write_index(root)
            (root / "projects" / "alpha.md").unlink()
            write_index(root)
            self.assertNotIn(
                "alpha", (root / "INDEX.md").read_text(encoding="utf-8")
            )

    def test_replaces_each_target_instead_of_writing_through_it(self) -> None:
        """A rename swaps in a new file. Writing in place would corrupt a reader
        holding the old file, so pin the difference with a hard link: the link
        must still hold the old bytes after a regeneration."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "tree"
            make_tree(root)
            (root / "projects" / "alpha.md").write_text(
                note("alpha", "project"), encoding="utf-8"
            )
            write_index(root)

            witnesses = {}
            for name in ("index.json", "INDEX.md"):
                witness = Path(raw) / f"witness-{name}"
                os.link(root / name, witness)
                witnesses[name] = (witness, witness.read_bytes())

            (root / "projects" / "beta.md").write_text(
                note("beta", "project"), encoding="utf-8"
            )
            write_index(root)

            for name, (witness, before) in witnesses.items():
                self.assertNotEqual((root / name).read_bytes(), before, name)
                self.assertEqual(witness.read_bytes(), before, name)

    def test_leaves_no_temp_file_when_a_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_text(
                note("alpha", "project"), encoding="utf-8"
            )
            # A directory cannot be replaced by a file, so the rename fails.
            (root / "INDEX.md").mkdir()
            with self.assertRaises(IndexError_) as caught:
                write_index(root)
            self.assertIn("INDEX.md", str(caught.exception))
            self.assertEqual(list(root.glob("*.tmp")), [])
            self.assertEqual(leftovers(root), [])

    def test_written_files_are_readable_by_the_owner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            write_index(root)
            for name in ("index.json", "INDEX.md"):
                mode = (root / name).stat().st_mode
                self.assertTrue(mode & 0o400, name)
                self.assertFalse(mode & 0o111, name)


class UnreadableDirectoryTests(unittest.TestCase):
    """An index built from a directory nobody can read is not an empty index."""

    def setUp(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")

    def test_iter_note_paths_raises_and_names_the_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_text(
                note("alpha", "project"), encoding="utf-8"
            )
            (root / "projects").chmod(0o000)
            try:
                with self.assertRaises(IndexError_) as caught:
                    iter_note_paths(root)
            finally:
                (root / "projects").chmod(0o755)
            self.assertIn("projects", str(caught.exception))

    def test_build_index_raises_rather_than_reporting_no_notes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_text(
                note("alpha", "project"), encoding="utf-8"
            )
            (root / "projects").chmod(0o000)
            try:
                with self.assertRaises(IndexError_):
                    build_index(root)
            finally:
                (root / "projects").chmod(0o755)


class PathRuleTests(unittest.TestCase):
    def test_a_note_path_is_decided_without_the_filesystem(self) -> None:
        self.assertTrue(is_note_path("projects/alpha.md"))
        self.assertFalse(is_note_path("projects/alpha/archive/0001.md"))
        self.assertFalse(is_note_path("INDEX.md"))
        self.assertFalse(is_note_path(".githooks/notes.md"))
        self.assertFalse(is_note_path("projects/alpha.txt"))

    def test_only_the_root_artifacts_are_index_artifacts(self) -> None:
        self.assertTrue(is_index_artifact("INDEX.md"))
        self.assertTrue(is_index_artifact("index.json"))
        self.assertFalse(is_index_artifact("projects/alpha/archive/INDEX.md"))
        self.assertFalse(is_index_artifact("projects/index.json"))


class BuildFromNotesTests(unittest.TestCase):
    """The index rule, applied to note text rather than to files."""

    def test_builds_the_same_payload_as_a_tree_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "projects" / "alpha.md").write_text(
                note("alpha", "project"), encoding="utf-8"
            )
            self.assertEqual(
                build_index(root),
                build_from_notes(
                    "work", [("projects/alpha.md", note("alpha", "project"))]
                ),
            )

    def test_reports_a_duplicate_id_by_both_paths(self) -> None:
        with self.assertRaises(IndexError_) as caught:
            build_from_notes(
                "work",
                [
                    ("projects/alpha.md", note("alpha", "project")),
                    ("feedback/copy.md", note("alpha", "project")),
                ],
            )
        self.assertIn("projects/alpha.md", str(caught.exception))
        self.assertIn("feedback/copy.md", str(caught.exception))

    def test_reports_a_classification_that_does_not_match(self) -> None:
        with self.assertRaises(IndexError_):
            build_from_notes(
                "core", [("projects/alpha.md", note("alpha", "project"))]
            )


class PreflightTests(unittest.TestCase):
    """What the guards check so that a clean check means a working index."""

    def test_a_ready_tree_reports_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            self.assertEqual(preflight(root), [])

    def test_reports_an_artifact_that_is_a_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            (root / "index.json").mkdir()
            self.assertEqual(len(preflight(root)), 1)
            self.assertIn("index.json", preflight(root)[0])

    def test_reports_an_artifact_that_is_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            elsewhere = root / "projects" / "target.json"
            elsewhere.write_text("{}", encoding="utf-8")
            (root / "index.json").symlink_to(elsewhere)
            self.assertIn("symlink", preflight(root)[0])

    def test_reports_a_root_that_cannot_be_written_to(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores directory write permission")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            root.chmod(0o500)
            try:
                problems = preflight(root)
            finally:
                root.chmod(0o700)
            self.assertTrue(problems)


class IndexCapTests(unittest.TestCase):
    """The cap is a sanity bound on the artifact. Nothing is ever dropped.

    The overflow cases patch the cap down rather than writing enough notes to
    exceed the real one, so they keep testing the mechanism whatever the real
    value is. One case pins that a realistic vault fits under the real cap.
    """

    def build(self, root: Path, count: int) -> dict:
        make_tree(root)
        for number in range(count):
            (root / "projects" / f"note-{number:03d}.md").write_text(
                note(f"note-{number:03d}", "project"), encoding="utf-8"
            )
        return write_index(root)

    def test_a_small_tree_is_within_cap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(cap_overflow(self.build(Path(raw), 2)), [])

    def test_an_oversized_index_is_reported_with_its_measured_size(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            index = self.build(Path(raw), 60)
            with mock.patch.object(index_module, "INDEX_CAP", 800):
                reports = cap_overflow(index)
                self.assertTrue(reports)
                self.assertIn(str(len(render_payload(index))), reports[0])
                self.assertIn("800", reports[0])

    def test_a_realistic_vault_fits_under_the_cap_with_headroom(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            index = self.build(Path(raw), 200)
            self.assertEqual(cap_overflow(index), [])
            # A vault holding well past a realistic note count should not be
            # anywhere near the bound, or the bound is a budget by accident.
            self.assertLess(len(render_payload(index)), INDEX_CAP // 2)

    def test_no_entry_is_dropped_to_fit_the_cap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(index_module, "INDEX_CAP", 800):
                index = self.build(root, 60)
                self.assertTrue(cap_overflow(index))
            self.assertEqual(len(index["notes"]), 60)
            written = json.loads((root / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(len(written["notes"]), 60)
            self.assertEqual(len(render_markdown(index).splitlines()), 62)


class IndexGenerationTests(unittest.TestCase):
    """The two artifacts are one generation, or the previous one stands."""

    def prepare(self, root: Path) -> None:
        make_tree(root)
        (root / "projects" / "alpha.md").write_text(
            note("alpha", "project"), encoding="utf-8"
        )
        write_index(root)

    def test_a_failure_on_the_second_artifact_restores_the_first(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.prepare(root)
            before = {
                name: (root / name).read_bytes()
                for name in ("index.json", "INDEX.md")
            }

            (root / "projects" / "beta.md").write_text(
                note("beta", "project"), encoding="utf-8"
            )

            real = os.replace
            calls = []

            def failing(source, target, **keywords):
                calls.append(target)
                if len(calls) == 2:
                    raise OSError(5, "injected failure")
                return real(source, target, **keywords)

            with (
                mock.patch("agent_efficiency.vault.index.os.replace", failing),
                self.assertRaises(IndexError_),
            ):
                write_index(root)

            for name, content in before.items():
                self.assertEqual((root / name).read_bytes(), content, name)
            self.assertEqual(leftovers(root), [])

    def test_a_failure_on_close_leaves_no_temporary(self) -> None:
        """The descriptor is closed inside the cleanup scope, not before it."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.prepare(root)
            real = os.fdopen

            class RefusesToClose:
                def __init__(self, stream) -> None:
                    self.stream = stream

                def __enter__(self):
                    return self

                def __exit__(self, *details) -> bool:
                    self.stream.close()
                    raise OSError(5, "close failed")

                def write(self, data):
                    return self.stream.write(data)

                def flush(self):
                    return self.stream.flush()

                def fileno(self):
                    return self.stream.fileno()

            opened = []

            def through_the_descriptor(handle, mode):
                opened.append(handle)
                return RefusesToClose(real(handle, mode))

            with (
                mock.patch(
                    "agent_efficiency.vault.index.os.fdopen",
                    through_the_descriptor,
                ),
                self.assertRaises(IndexError_),
            ):
                write_index(root)
            self.assertTrue(opened, "the temporary was not written through mkstemp")
            self.assertEqual(leftovers(root), [])

    def test_a_failure_while_the_temporary_is_open_leaves_nothing_behind(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.prepare(root)
            with (
                mock.patch(
                    "agent_efficiency.vault.index.os.fsync",
                    side_effect=OSError(28, "no space"),
                ),
                self.assertRaises(IndexError_),
            ):
                write_index(root)
            self.assertEqual(leftovers(root), [])
            self.assertEqual(list(root.glob("*.tmp")), [])


class WriteIndexUnwritableTreeTests(unittest.TestCase):
    def test_reports_an_unwritable_tree_as_an_index_error(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores directory write permission")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_tree(root)
            root.chmod(0o500)
            try:
                with self.assertRaises(IndexError_):
                    write_index(root)
            finally:
                root.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
