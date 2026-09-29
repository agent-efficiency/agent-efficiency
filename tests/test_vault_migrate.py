from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_efficiency.vault import migrate
from agent_efficiency.vault.guards import check_tree
from agent_efficiency.vault.index import build_index
from agent_efficiency.vault.migrate import (
    MigrationError,
    _render,
    _verify,
    apply_proposal,
    propose,
    sanitize_id,
)
from agent_efficiency.vault.schema import CAPS, load_note

SOURCE = "---\nname: sample\ndescription: sample note\n---\nBody.\n"


def make_tree(root: Path, classification: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".vault.json").write_text(
        json.dumps({"schema": 1, "classification": classification}),
        encoding="utf-8",
    )
    return root


def entry(**overrides: object) -> dict:
    base = {
        "id": "umbra",
        "title": "umbra engine",
        "type": "project",
        "classification": "work",
        "hook": "engine state",
        "source": "/tmp/project_umbra.md",
        "body": "Next move.",
    }
    base.update(overrides)
    return base


def proposal(*entries: dict) -> dict:
    return {"schema": 1, "entries": list(entries)}


class IdSanitizerTests(unittest.TestCase):
    def test_lowercases_and_folds_separators(self) -> None:
        self.assertEqual(sanitize_id("My Note.v2"), "my-note-v2")

    def test_collapses_runs_and_trims_edges(self) -> None:
        self.assertEqual(sanitize_id("__a   b__"), "a-b")

    def test_refuses_a_name_with_no_usable_characters(self) -> None:
        for value in ("..", ".", "///", "   "):
            with self.subTest(value=value), self.assertRaises(MigrationError):
                sanitize_id(value)

    def test_result_is_stable_under_reapplication(self) -> None:
        once = sanitize_id("Project_Alpha Beta")
        self.assertEqual(sanitize_id(once), once)


class ProposalTests(unittest.TestCase):
    def test_proposes_one_entry_per_source_note(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "memory"
            source.mkdir()
            (source / "MEMORY.md").write_text("# Memory Index\n", encoding="utf-8")
            (source / "project_umbra.md").write_text(SOURCE, encoding="utf-8")
            (source / "feedback_plain.md").write_text(SOURCE, encoding="utf-8")
            result = propose(memory_dir=source, remember_dir=None)
            ids = sorted(item["id"] for item in result["entries"])
            self.assertEqual(ids, ["plain", "umbra"])

    def test_index_file_is_not_proposed_as_a_note(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "memory"
            source.mkdir()
            (source / "MEMORY.md").write_text("# Memory Index\n", encoding="utf-8")
            result = propose(memory_dir=source, remember_dir=None)
            self.assertEqual(result["entries"], [])

    def test_proposes_type_from_the_filename_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "memory"
            source.mkdir()
            for name, expected in (
                ("project_a.md", "project"),
                ("feedback_b.md", "feedback"),
                ("reference_c.md", "reference"),
                ("doctrine_d.md", "doctrine"),
                ("plain_note.md", "project"),
            ):
                (source / name).write_text(SOURCE, encoding="utf-8")
            found = {
                item["id"]: item["type"]
                for item in propose(memory_dir=source, remember_dir=None)["entries"]
            }
            self.assertEqual(
                found,
                {
                    "a": "project",
                    "b": "feedback",
                    "c": "reference",
                    "d": "doctrine",
                    "plain-note": "project",
                },
            )

    def test_reads_both_source_directories(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = Path(raw) / "memory"
            remember = Path(raw) / "remember"
            memory.mkdir()
            remember.mkdir()
            (memory / "project_alpha.md").write_text(SOURCE, encoding="utf-8")
            (remember / "beta.md").write_text(SOURCE, encoding="utf-8")
            (remember / "remember.md").write_text(SOURCE, encoding="utf-8")
            ids = [
                item["id"]
                for item in propose(memory_dir=memory, remember_dir=remember)[
                    "entries"
                ]
            ]
            self.assertEqual(sorted(ids), ["alpha", "beta"])

    def test_reports_two_sources_that_sanitize_to_one_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "memory"
            source.mkdir()
            (source / "feedback_a_b.md").write_text(SOURCE, encoding="utf-8")
            (source / "feedback_a-b.md").write_text(SOURCE, encoding="utf-8")
            with self.assertRaises(MigrationError) as caught:
                propose(memory_dir=source, remember_dir=None)
            message = str(caught.exception)
            self.assertIn("feedback_a_b.md", message)
            self.assertIn("feedback_a-b.md", message)

    def test_reports_the_same_id_arriving_from_both_directories(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = Path(raw) / "memory"
            remember = Path(raw) / "remember"
            memory.mkdir()
            remember.mkdir()
            (memory / "project_newsletter.md").write_text(SOURCE, encoding="utf-8")
            (remember / "newsletter.md").write_text(SOURCE, encoding="utf-8")
            with self.assertRaises(MigrationError):
                propose(memory_dir=memory, remember_dir=remember)

    def test_refuses_a_source_directory_that_does_not_exist(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            missing = Path(raw) / "memory"
            with self.assertRaises(MigrationError) as caught:
                propose(memory_dir=missing, remember_dir=None)
            self.assertIn(str(missing), str(caught.exception))

    def test_refuses_a_source_that_is_not_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "memory"
            source.mkdir()
            broken = source / "project_broken.md"
            broken.write_bytes(b"head\n\xff\xfe\x00tail\n")
            with self.assertRaises(MigrationError) as caught:
                propose(memory_dir=source, remember_dir=None)
            self.assertIn("project_broken.md", str(caught.exception))

    def test_refuses_when_no_source_directory_is_given(self) -> None:
        with self.assertRaises(MigrationError):
            propose(memory_dir=None, remember_dir=None)

    def test_classification_guess_is_only_a_hint(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "memory"
            source.mkdir()
            (source / "project_one.md").write_text(
                "---\nname: one\n---\numbra engine, client work.\n", encoding="utf-8"
            )
            (source / "project_two.md").write_text(
                "---\nname: two\n---\nA birthday gift idea.\n", encoding="utf-8"
            )
            guessed = {
                item["id"]: item["classification"]
                for item in propose(memory_dir=source, remember_dir=None)["entries"]
            }
            self.assertEqual(guessed, {"one": "work", "two": "private"})

    def test_body_keeps_indentation_and_interior_blank_lines(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "memory"
            source.mkdir()
            (source / "project_code.md").write_text(
                "    indented head\n\n    second block\n\n", encoding="utf-8"
            )
            body = propose(memory_dir=source, remember_dir=None)["entries"][0]["body"]
            self.assertEqual(body, "    indented head\n\n    second block")

    def test_body_line_endings_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "memory"
            source.mkdir()
            (source / "project_crlf.md").write_bytes(b"one\r\ntwo\rthree\n")
            body = propose(memory_dir=source, remember_dir=None)["entries"][0]["body"]
            self.assertEqual(body, "one\ntwo\nthree")

    def test_updated_comes_from_the_source_modification_time(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "memory"
            source.mkdir()
            path = source / "project_dated.md"
            path.write_text(SOURCE, encoding="utf-8")
            # 2021-03-04T12:00:00Z
            os.utime(path, (1614859200, 1614859200))
            item = propose(memory_dir=source, remember_dir=None)["entries"][0]
            self.assertEqual(item["updated"], "2021-03-04")

    def test_the_proposal_is_stable_across_runs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "memory"
            source.mkdir()
            for name in ("project_zeta.md", "project_alpha.md", "feedback_mid.md"):
                (source / name).write_text(SOURCE, encoding="utf-8")
            first = propose(memory_dir=source, remember_dir=None)
            self.assertEqual(first, propose(memory_dir=source, remember_dir=None))
            self.assertEqual(
                [item["id"] for item in first["entries"]], ["mid", "alpha", "zeta"]
            )

    def test_every_proposed_entry_records_its_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "memory"
            source.mkdir()
            path = source / "project_umbra.md"
            path.write_text(SOURCE, encoding="utf-8")
            item = propose(memory_dir=source, remember_dir=None)["entries"][0]
            self.assertEqual(item["source"], str(path))


class ApplyTests(unittest.TestCase):
    def two_trees(self, base: Path) -> dict[str, Path]:
        return {
            "core": make_tree(base / "vault-core", "core"),
            "work": make_tree(base / "vault-work", "work"),
        }

    def test_writes_notes_only_into_the_matching_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            trees = self.two_trees(Path(raw))
            apply_proposal(
                proposal(
                    entry(),
                    entry(
                        id="plain",
                        title="plain speech",
                        type="feedback",
                        classification="core",
                        hook="plain speech",
                        body="Say it plainly.",
                    ),
                ),
                trees,
            )
            self.assertTrue((trees["work"] / "projects" / "umbra.md").is_file())
            self.assertTrue((trees["core"] / "feedback" / "plain.md").is_file())
            self.assertFalse((trees["core"] / "projects" / "umbra.md").exists())
            note = load_note(
                (trees["work"] / "projects" / "umbra.md").read_text(encoding="utf-8")
            )
            self.assertEqual(note.classification, "work")
            self.assertEqual(note.status, "active")
            self.assertEqual(note.body, "Next move.")

    def test_each_type_lands_in_the_directory_init_creates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            entries = [
                entry(id=name, type=name, title=f"{name} title", hook=f"{name} hook")
                for name in ("project", "feedback", "reference", "doctrine", "session")
            ]
            apply_proposal(proposal(*entries), {"work": work})
            for note_type, directory in (
                ("project", "projects"),
                ("feedback", "feedback"),
                ("reference", "reference"),
                ("doctrine", "doctrine"),
                ("session", "sessions"),
            ):
                self.assertTrue(
                    (work / directory / f"{note_type}.md").is_file(), note_type
                )

    def test_the_written_tree_indexes_and_checks_clean(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            apply_proposal(proposal(entry()), {"work": work})
            index = json.loads((work / "index.json").read_text(encoding="utf-8"))
            self.assertEqual([item["id"] for item in index["notes"]], ["umbra"])
            self.assertEqual(index, build_index(work))
            self.assertEqual(check_tree(work), [])

    def test_oversized_source_becomes_an_archive_plus_a_head_stub(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            body = "y" * (CAPS["project"] + 5000)
            apply_proposal(proposal(entry(body=body)), {"work": work})
            archive = work / "projects" / "umbra" / "archive" / "0001-imported.md"
            self.assertTrue(archive.is_file())
            self.assertEqual(archive.read_text(encoding="utf-8"), body + "\n")
            head_path = work / "projects" / "umbra.md"
            head = load_note(head_path.read_text(encoding="utf-8"))
            self.assertTrue(head.within_cap)
            self.assertIn("archive/0001-imported.md", head.body)

    def test_the_stub_points_at_where_the_archive_actually_landed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            apply_proposal(
                proposal(entry(body="y" * (CAPS["project"] + 5000))), {"work": work}
            )
            head_path = work / "projects" / "umbra.md"
            head = load_note(head_path.read_text(encoding="utf-8"))
            pointer = head.body.rsplit(" ", 1)[-1].strip()
            self.assertTrue(
                (head_path.parent / pointer).is_file(),
                f"stub points at {pointer!r}, which does not exist",
            )

    def test_an_archived_import_is_kept_out_of_the_index(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            apply_proposal(
                proposal(entry(body="y" * (CAPS["project"] + 5000))), {"work": work}
            )
            index = json.loads((work / "index.json").read_text(encoding="utf-8"))
            self.assertEqual([item["id"] for item in index["notes"]], ["umbra"])
            self.assertEqual(check_tree(work), [])

    def test_refuses_a_head_stub_that_cannot_fit_its_cap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            with self.assertRaises(MigrationError) as caught:
                apply_proposal(
                    proposal(
                        entry(
                            type="feedback",
                            hook="h" * (CAPS["feedback"] + 10),
                            body="y" * (CAPS["feedback"] + 5000),
                        )
                    ),
                    {"work": work},
                )
            self.assertIn("cap", str(caught.exception))
            self.assertFalse((work / "feedback" / "umbra.md").exists())

    def test_refuses_an_entry_with_no_classification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            bare = entry()
            del bare["classification"]
            with self.assertRaises(MigrationError) as caught:
                apply_proposal(proposal(bare), {"work": work})
            self.assertIn("classification", str(caught.exception))
            self.assertFalse((work / "projects" / "umbra.md").exists())

    def test_refuses_an_unknown_classification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            with self.assertRaises(MigrationError):
                apply_proposal(
                    proposal(entry(classification="public")), {"work": work}
                )

    def test_refuses_a_classification_with_no_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            with self.assertRaises(MigrationError) as caught:
                apply_proposal(
                    proposal(entry(classification="private")), {"work": work}
                )
            self.assertIn("private", str(caught.exception))

    def test_refuses_a_tree_path_that_is_not_a_vault(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            plain = Path(raw) / "plain"
            plain.mkdir()
            with self.assertRaises(MigrationError) as caught:
                apply_proposal(proposal(entry()), {"work": plain})
            self.assertIn(".vault.json", str(caught.exception))

    def test_refuses_a_tree_whose_marker_names_another_classification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            core = make_tree(Path(raw) / "vault-core", "core")
            with self.assertRaises(MigrationError) as caught:
                apply_proposal(proposal(entry()), {"work": core})
            self.assertIn("core", str(caught.exception))
            self.assertFalse((core / "projects" / "umbra.md").exists())

    def test_refuses_an_unsupported_proposal_schema(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            with self.assertRaises(MigrationError):
                apply_proposal({"schema": 2, "entries": []}, {"work": work})

    def test_refuses_an_unknown_field_in_an_entry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            with self.assertRaises(MigrationError) as caught:
                apply_proposal(
                    proposal(entry(classifcation="work")), {"work": work}
                )
            self.assertIn("classifcation", str(caught.exception))

    def test_refuses_an_id_that_is_not_already_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            for bad in ("../escape", "Upper", "with space", ".."):
                with self.subTest(bad=bad), self.assertRaises(MigrationError):
                    apply_proposal(proposal(entry(id=bad)), {"work": work})
            self.assertEqual(list((work / "projects").glob("*.md")), [])

    def test_refuses_a_newline_smuggled_into_a_field(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            for smuggled in ("state\nstatus: done", "state\nrepos: [one, two]"):
                with self.subTest(smuggled=smuggled):
                    with self.assertRaises(MigrationError) as caught:
                        apply_proposal(
                            proposal(entry(hook=smuggled)), {"work": work}
                        )
                    self.assertIn("field 'hook'", str(caught.exception))
            self.assertFalse((work / "projects" / "umbra.md").exists())

    def test_refuses_a_field_the_grammar_cannot_hold(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            with self.assertRaises(MigrationError):
                apply_proposal(
                    proposal(entry(title="umbra [engine]")), {"work": work}
                )

    def test_refuses_an_exotic_line_break_in_a_body(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            with self.assertRaises(MigrationError) as caught:
                apply_proposal(
                    proposal(entry(body="one\u2028two")), {"work": work}
                )
            self.assertIn("project_umbra.md", str(caught.exception))
            self.assertFalse((work / "projects" / "umbra.md").exists())

    def test_refuses_two_entries_that_claim_one_note(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            with self.assertRaises(MigrationError) as caught:
                apply_proposal(
                    proposal(
                        entry(source="/tmp/one.md"),
                        entry(source="/tmp/two.md", title="another umbra"),
                    ),
                    {"work": work},
                )
            message = str(caught.exception)
            self.assertIn("/tmp/one.md", message)
            self.assertIn("/tmp/two.md", message)

    def test_the_same_id_may_live_in_two_different_trees(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            trees = self.two_trees(Path(raw))
            apply_proposal(
                proposal(entry(), entry(classification="core")),
                trees,
            )
            self.assertTrue((trees["work"] / "projects" / "umbra.md").is_file())
            self.assertTrue((trees["core"] / "projects" / "umbra.md").is_file())

    def test_a_late_failure_leaves_no_notes_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            good = [
                entry(id=f"note-{number}", title=f"note {number}")
                for number in range(5)
            ]
            with self.assertRaises(MigrationError):
                apply_proposal(
                    proposal(*good, entry(id="last", classification="public")),
                    {"work": work},
                )
            self.assertEqual(list(work.rglob("*.md")), [])
            self.assertFalse((work / "index.json").exists())

    def test_refuses_to_replace_a_note_that_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            apply_proposal(proposal(entry()), {"work": work})
            before = (work / "projects" / "umbra.md").read_text(encoding="utf-8")
            with self.assertRaises(MigrationError) as caught:
                apply_proposal(proposal(entry(body="Different.")), {"work": work})
            self.assertIn("umbra.md", str(caught.exception))
            self.assertEqual(
                (work / "projects" / "umbra.md").read_text(encoding="utf-8"), before
            )

    def test_replaces_an_existing_note_only_when_told_to(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            apply_proposal(proposal(entry()), {"work": work})
            apply_proposal(
                proposal(entry(body="Different.")), {"work": work}, overwrite=True
            )
            note = load_note(
                (work / "projects" / "umbra.md").read_text(encoding="utf-8")
            )
            self.assertEqual(note.body, "Different.")

    def test_status_and_updated_are_taken_from_the_entry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            apply_proposal(
                proposal(entry(status="dormant", updated="2020-01-02")),
                {"work": work},
            )
            note = load_note(
                (work / "projects" / "umbra.md").read_text(encoding="utf-8")
            )
            self.assertEqual(note.status, "dormant")
            self.assertEqual(note.updated, "2020-01-02")

    def test_refuses_an_unknown_status_and_a_malformed_date(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            for override in ({"status": "paused"}, {"updated": "yesterday"}):
                with self.subTest(**override), self.assertRaises(MigrationError):
                    apply_proposal(proposal(entry(**override)), {"work": work})

    def test_an_absent_updated_field_gets_a_calendar_date(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            apply_proposal(proposal(entry()), {"work": work})
            note = load_note(
                (work / "projects" / "umbra.md").read_text(encoding="utf-8")
            )
            year, month, day = note.updated.split("-")
            self.assertEqual((len(year), len(month), len(day)), (4, 2, 2))
            self.assertTrue(note.updated.replace("-", "").isdigit())

    def test_refuses_an_entry_that_is_not_an_object(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            for bad in ("text", 3, None, ["id"]):
                with self.subTest(bad=bad), self.assertRaises(MigrationError):
                    apply_proposal({"schema": 1, "entries": [bad]}, {"work": work})

    def test_refuses_a_proposal_that_is_not_shaped_like_one(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            for bad in ({"schema": 1}, {"schema": 1, "entries": {}}, [], "text"):
                with self.subTest(bad=bad), self.assertRaises(MigrationError):
                    apply_proposal(bad, {"work": work})

    def test_refuses_when_no_tree_is_given_at_all(self) -> None:
        with self.assertRaises(MigrationError):
            apply_proposal(proposal(entry()), {})


class RoundTripTests(unittest.TestCase):
    def test_a_proposed_note_applies_and_passes_the_guards(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            source = base / "memory"
            source.mkdir()
            (source / "project_umbra.md").write_text(
                "---\nname: umbra\n---\numbra engine, client work.\n",
                encoding="utf-8",
            )
            (source / "feedback_plain speech.md").write_text(
                "---\nname: plain\n---\nA gift idea for a birthday.\n",
                encoding="utf-8",
            )
            drafted = propose(memory_dir=source, remember_dir=None)
            # A person reviews and corrects the guessed classification.
            for item in drafted["entries"]:
                item["classification"] = "work"
            work = make_tree(base / "vault-work", "work")
            written = apply_proposal(drafted, {"work": work})
            self.assertEqual(len(written), 2)
            self.assertEqual(check_tree(work), [])
            index = json.loads((work / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(
                sorted(item["id"] for item in index["notes"]),
                ["plain-speech", "umbra"],
            )

    def test_an_oversized_source_survives_the_whole_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            source = base / "remember"
            source.mkdir()
            (source / "newsletter.md").write_text(
                "# Handoff\n\n" + ("detail line\n" * 4000), encoding="utf-8"
            )
            drafted = propose(memory_dir=None, remember_dir=source)
            drafted["entries"][0]["classification"] = "work"
            work = make_tree(base / "vault-work", "work")
            apply_proposal(drafted, {"work": work})
            self.assertEqual(check_tree(work), [])
            head = load_note(
                (work / "projects" / "newsletter.md").read_text(encoding="utf-8")
            )
            self.assertTrue(head.within_cap)
            archive = work / "projects" / "newsletter" / "archive" / "0001-imported.md"
            self.assertIn("detail line", archive.read_text(encoding="utf-8"))

class RoundTripGuardTests(unittest.TestCase):
    """Pin the last check a note passes before it is written.

    The field checks in front of it refuse every input that could reach it
    today, so no proposal can drive it. It is pinned here directly because it
    is the backstop: if the note schema changes, or one of those field checks
    is weakened, this is what still stops a note that says something other
    than its entry said.
    """

    def item(self) -> dict:
        return entry(status="active", updated="2026-09-21")

    def test_accepts_text_that_matches_the_entry(self) -> None:
        item = self.item()
        note = _verify(_render(item, item["body"]), item, item["body"])
        self.assertEqual(note.hook, item["hook"])
        self.assertEqual(note.body, item["body"])

    def test_refuses_text_that_reads_back_with_another_field_value(self) -> None:
        item = self.item()
        for field, line, replacement in (
            ("hook", "hook: engine state", "hook: something else"),
            ("title", "title: umbra engine", "title: another title"),
            ("status", "status: active", "status: done"),
            ("updated", "updated: 2026-09-21", "updated: 2020-01-01"),
        ):
            text = _render(item, item["body"]).replace(line, replacement)
            with self.subTest(field=field):
                with self.assertRaises(MigrationError) as caught:
                    _verify(text, item, item["body"])
                self.assertIn(field, str(caught.exception))

    def test_refuses_text_whose_body_does_not_survive(self) -> None:
        item = self.item()
        with self.assertRaises(MigrationError) as caught:
            _verify(_render(item, "Another body."), item, item["body"])
        self.assertIn("round trip", str(caught.exception))

    def test_refuses_text_that_will_not_load(self) -> None:
        item = self.item()
        text = _render(item, item["body"]).replace("type: project", "type: notes")
        with self.assertRaises(MigrationError):
            _verify(text, item, item["body"])

def reviewed(proposal: dict, classification: str = "work") -> dict:
    """A proposal after the review step that decides every classification."""

    for item in proposal["entries"]:
        item["classification"] = classification
    return proposal


def snapshot(root: Path) -> dict[str, bytes]:
    """Every file in the tree, by path, with its bytes. The whole state."""

    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class WriteAtomicityTests(unittest.TestCase):
    """A failed apply leaves the tree exactly as it was. Not nearly as it was."""

    def tree(self, raw: str) -> Path:
        return make_tree(Path(raw) / "vault-work", "work")

    def entries(self, count: int) -> list[dict]:
        return [
            entry(id=f"note-{number}", title=f"note {number}")
            for number in range(count)
        ]

    def test_a_failure_part_way_through_removes_the_notes_already_written(
        self,
    ) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores directory write permission")
        with tempfile.TemporaryDirectory() as raw:
            work = self.tree(raw)
            (work / "feedback").mkdir()
            before = snapshot(work)
            entries = [
                entry(id="first", title="first"),
                entry(id="second", title="second", type="feedback"),
                entry(id="third", title="third"),
            ]
            (work / "feedback").chmod(0o500)
            try:
                with self.assertRaises(MigrationError) as caught:
                    apply_proposal(proposal(*entries), {"work": work})
            finally:
                (work / "feedback").chmod(0o755)

            self.assertIn("second", str(caught.exception))
            self.assertEqual(snapshot(work), before)
            self.assertEqual(list(work.rglob("*.md")), [])

    def test_an_interrupted_write_leaves_no_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = self.tree(raw)
            before = snapshot(work)
            real = migrate._stage
            calls = []

            def failing(write):
                calls.append(write.path)
                if len(calls) == 2:
                    raise OSError(28, "no space left on device")
                return real(write)

            with (
                mock.patch.object(migrate, "_stage", failing),
                self.assertRaises(MigrationError),
            ):
                apply_proposal(proposal(*self.entries(3)), {"work": work})

            self.assertEqual(snapshot(work), before)
            self.assertEqual(sorted(work.rglob("*.tmp")), [])

    def test_an_index_write_failure_leaves_the_tree_as_it_was(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = self.tree(raw)
            (work / "projects").mkdir()
            (work / "projects" / "kept.md").write_text(
                "---\nschema: 1\nid: kept\ntitle: kept\ntype: project\n"
                "classification: work\nstatus: active\nhook: kept hook\n---\n"
                "Kept.\n",
                encoding="utf-8",
            )
            migrate.write_index(work)
            before = snapshot(work)
            failure = migrate.IndexError_("index.json could not be written")
            with (
                mock.patch.object(migrate, "write_index", side_effect=failure),
                self.assertRaises(MigrationError) as caught,
            ):
                apply_proposal(proposal(entry()), {"work": work})

            self.assertIn("nothing was written", str(caught.exception))
            self.assertEqual(snapshot(work), before)
            self.assertFalse((work / "projects" / "umbra.md").exists())

    def test_a_second_tree_index_failure_restores_both_trees(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            private = make_tree(Path(raw) / "vault-private", "private")
            work = self.tree(raw)
            for root in (private, work):
                migrate.write_index(root)
            before = {root: snapshot(root) for root in (private, work)}
            real = migrate.write_index

            def second_fails(root: Path) -> dict:
                if root == work.resolve():
                    raise OSError(28, "no space left on device")
                return real(root)

            with (
                mock.patch.object(migrate, "write_index", second_fails),
                self.assertRaises(MigrationError),
            ):
                apply_proposal(
                    proposal(
                        entry(id="mine", title="mine", classification="private"),
                        entry(),
                    ),
                    {"private": private, "work": work},
                )

            for root in (private, work):
                self.assertEqual(snapshot(root), before[root], root.name)

    def test_a_failure_after_a_file_is_in_place_still_removes_it(self) -> None:
        """The path is recorded before the write starts, not after it succeeds."""

        with tempfile.TemporaryDirectory() as raw:
            work = self.tree(raw)
            before = snapshot(work)
            calls = []

            def failing(write):
                calls.append(write.path)
                if len(calls) == 2:
                    raise OSError(5, "input output error")

            with (
                mock.patch.object(migrate, "_confirm", failing),
                self.assertRaises(MigrationError),
            ):
                apply_proposal(proposal(*self.entries(3)), {"work": work})

            self.assertEqual(snapshot(work), before)

    def test_a_rollback_removes_the_directories_it_created(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = self.tree(raw)
            before = snapshot(work)
            entries = [
                entry(id="first", title="first"),
                entry(id="second", title="second", classification="core"),
            ]
            with self.assertRaises(MigrationError):
                apply_proposal(proposal(*entries), {"work": work})
            self.assertFalse((work / "projects").exists())
            self.assertEqual(snapshot(work), before)

    def test_a_file_that_appears_during_the_run_is_not_overwritten(self) -> None:
        """The existence check and the write are not the same instant.

        Between them a file can appear. The write is one link operation that
        fails if anything is there, so the file that appeared survives.
        """

        with tempfile.TemporaryDirectory() as raw:
            work = self.tree(raw)
            (work / "projects").mkdir()
            target = work / "projects" / "umbra.md"
            target.write_text("written by somebody else\n", encoding="utf-8")
            real = Path.exists

            def blind(self):
                return False if self == target else real(self)

            with (
                mock.patch.object(Path, "exists", blind),
                self.assertRaises(MigrationError) as caught,
            ):
                apply_proposal(proposal(entry()), {"work": work})

            self.assertIn("umbra.md", str(caught.exception))
            self.assertEqual(
                target.read_text(encoding="utf-8"), "written by somebody else\n"
            )

    def test_an_overwrite_that_fails_later_puts_the_old_note_back(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = self.tree(raw)
            apply_proposal(proposal(entry(id="first", title="first")), {"work": work})
            before = snapshot(work)
            calls = []

            def failing(write):
                calls.append(write.path)
                if len(calls) == 2:
                    raise OSError(5, "input output error")

            entries = [
                entry(id="first", title="first", body="Replaced."),
                entry(id="second", title="second", body="Replaced."),
            ]
            with (
                mock.patch.object(migrate, "_confirm", failing),
                self.assertRaises(MigrationError),
            ):
                apply_proposal(proposal(*entries), {"work": work}, overwrite=True)

            self.assertEqual(snapshot(work), before)

    def test_a_written_note_is_read_back_before_the_run_is_called_done(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = self.tree(raw)
            written = apply_proposal(proposal(entry()), {"work": work})
            self.assertEqual(
                written[0].read_text(encoding="utf-8").count("id: umbra"), 1
            )


class EncodableTests(unittest.TestCase):
    """Text that cannot be written is refused before anything is created."""

    def test_refuses_a_lone_surrogate_in_a_body_and_names_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            before = snapshot(work)
            broken = json.loads('"lead \\ud800 tail"')
            with self.assertRaises(MigrationError) as caught:
                apply_proposal(proposal(entry(body=broken)), {"work": work})
            self.assertIn("project_umbra.md", str(caught.exception))
            self.assertIn("UTF-8", str(caught.exception))
            self.assertEqual(snapshot(work), before)

    def test_refuses_a_lone_surrogate_in_a_title(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            broken = json.loads('"umbra \\udfff"')
            with self.assertRaises(MigrationError):
                apply_proposal(proposal(entry(title=broken)), {"work": work})

    def test_an_earlier_note_is_not_left_behind_by_a_later_surrogate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            before = snapshot(work)
            broken = json.loads('"lead \\ud800 tail"')
            with self.assertRaises(MigrationError):
                apply_proposal(
                    proposal(
                        entry(id="first", title="first"),
                        entry(id="second", title="second", body=broken),
                    ),
                    {"work": work},
                )
            self.assertEqual(snapshot(work), before)


class DestinationContainmentTests(unittest.TestCase):
    """A migration writes inside its classified tree, and nowhere else."""

    def test_refuses_a_symlinked_type_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            outside = Path(raw) / "elsewhere"
            outside.mkdir()
            (work / "projects").symlink_to(outside)
            with self.assertRaises(MigrationError) as caught:
                apply_proposal(proposal(entry()), {"work": work})
            self.assertIn("symlink", str(caught.exception))
            self.assertEqual(list(outside.iterdir()), [])

    def test_refuses_a_symlinked_note_destination(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            outside = Path(raw) / "elsewhere.md"
            (work / "projects").mkdir()
            (work / "projects" / "umbra.md").symlink_to(outside)
            with self.assertRaises(MigrationError) as caught:
                apply_proposal(proposal(entry()), {"work": work})
            self.assertIn("symlink", str(caught.exception))
            self.assertFalse(outside.exists())

    def test_refuses_a_symlinked_archive_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            outside = Path(raw) / "elsewhere"
            outside.mkdir()
            (work / "projects" / "umbra").mkdir(parents=True)
            (work / "projects" / "umbra" / "archive").symlink_to(outside)
            with self.assertRaises(MigrationError) as caught:
                apply_proposal(
                    proposal(entry(body="y" * (CAPS["project"] + 5000))),
                    {"work": work},
                )
            self.assertIn("symlink", str(caught.exception))
            self.assertEqual(list(outside.iterdir()), [])

    def test_refuses_two_entries_that_resolve_to_one_physical_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            core = make_tree(Path(raw) / "vault-core", "core")
            (core / "projects").symlink_to(work / "projects")
            (work / "projects").mkdir()
            with self.assertRaises(MigrationError):
                apply_proposal(
                    proposal(
                        entry(id="umbra", classification="work"),
                        entry(id="umbra", classification="core"),
                    ),
                    {"work": work, "core": core},
                )


class WholeTreeValidationTests(unittest.TestCase):
    """The tree the migration would produce is built and indexed first."""

    def test_refuses_an_id_a_note_already_in_the_tree_owns(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            apply_proposal(proposal(entry(id="umbra")), {"work": work})
            before = snapshot(work)

            with self.assertRaises(MigrationError) as caught:
                apply_proposal(
                    proposal(entry(id="umbra", type="reference")), {"work": work}
                )
            self.assertIn("umbra", str(caught.exception))
            self.assertFalse((work / "reference" / "umbra.md").exists())
            self.assertEqual(snapshot(work), before)

    def test_the_conflicting_note_is_not_written_first(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            (work / "feedback").mkdir()
            (work / "feedback" / "held.md").write_text(
                "---\nschema: 1\nid: umbra\ntitle: held\ntype: feedback\n"
                "classification: work\nstatus: active\nupdated: 2026-09-21\n"
                "hook: held hook\n---\nBody.\n",
                encoding="utf-8",
            )
            before = snapshot(work)
            with self.assertRaises(MigrationError):
                apply_proposal(proposal(entry(id="umbra")), {"work": work})
            self.assertEqual(snapshot(work), before)

    def test_a_tree_that_would_not_index_is_refused_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            apply_proposal(proposal(entry(id="umbra")), {"work": work})
            before = snapshot(work)
            entries = [
                entry(id="first", title="first"),
                entry(id="umbra", type="reference"),
            ]
            with self.assertRaises(MigrationError):
                apply_proposal(proposal(*entries), {"work": work})
            self.assertEqual(snapshot(work), before)


class ArchiveFidelityTests(unittest.TestCase):
    """An archive is a copy of the source. A copy differs in nothing."""

    def source(self, size: int) -> bytes:
        block = "detail line with words in it\r\n" * (size // 30 + 1)
        return ("\r\n\r\n" + block + "\r\n\r\n").encode("utf-8")

    def migrate_one(self, raw: str, payload: bytes) -> tuple[Path, Path]:
        memory = Path(raw) / "memory"
        memory.mkdir()
        (memory / "project_newsletter.md").write_bytes(payload)
        work = make_tree(Path(raw) / "vault-work", "work")
        found = reviewed(propose(memory_dir=memory, remember_dir=None))
        apply_proposal(found, {"work": work})
        return work, (
            work / "projects" / "newsletter" / "archive" / "0001-imported.md"
        )

    def test_an_archive_is_the_source_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            payload = self.source(8000)
            _work, archive = self.migrate_one(raw, payload)
            self.assertEqual(archive.read_bytes(), payload)

    def test_a_large_source_is_archived_whole(self) -> None:
        """The real sources include a 44 KB file. Nothing is trimmed to fit."""

        with tempfile.TemporaryDirectory() as raw:
            payload = self.source(44000)
            self.assertGreater(len(payload), 44000)
            _work, archive = self.migrate_one(raw, payload)
            self.assertEqual(archive.read_bytes(), payload)
            self.assertEqual(len(archive.read_bytes()), len(payload))

    def test_line_endings_and_boundary_blank_lines_survive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            payload = self.source(12000)
            _work, archive = self.migrate_one(raw, payload)
            written = archive.read_bytes()
            self.assertTrue(written.startswith(b"\r\n\r\n"))
            self.assertTrue(written.endswith(b"\r\n\r\n"))
            self.assertIn(b"\r\n", written)

    def test_a_proposal_carries_the_digest_of_its_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = Path(raw) / "memory"
            memory.mkdir()
            payload = self.source(500)
            (memory / "project_newsletter.md").write_bytes(payload)
            found = propose(memory_dir=memory, remember_dir=None)
            item = found["entries"][0]
            self.assertEqual(
                item["digest"], hashlib.sha256(payload).hexdigest()
            )
            self.assertEqual(item["raw"].encode("utf-8"), payload)

    def test_an_edited_body_with_a_stale_digest_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = Path(raw) / "memory"
            memory.mkdir()
            (memory / "project_newsletter.md").write_bytes(self.source(44000))
            work = make_tree(Path(raw) / "vault-work", "work")
            found = reviewed(propose(memory_dir=memory, remember_dir=None))
            found["entries"][0]["raw"] = found["entries"][0]["raw"][:3000]
            before = snapshot(work)
            with self.assertRaises(MigrationError) as caught:
                apply_proposal(found, {"work": work})
            self.assertIn("digest", str(caught.exception))
            self.assertEqual(snapshot(work), before)

    def test_a_short_source_needs_no_archive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = Path(raw) / "memory"
            memory.mkdir()
            (memory / "project_small.md").write_text("Short.\n", encoding="utf-8")
            work = make_tree(Path(raw) / "vault-work", "work")
            apply_proposal(
                reviewed(propose(memory_dir=memory, remember_dir=None)),
                {"work": work},
            )
            self.assertTrue((work / "projects" / "small.md").is_file())
            self.assertFalse((work / "projects" / "small").exists())


class RenderFaultTests(unittest.TestCase):
    """The round trip is what stands between a rendering fault and the tree.

    Each test breaks the renderer and asks for a migration. With the round
    trip in place the migration is refused and nothing is written. Without it
    the broken note reaches disk, which is what these tests are for: they fail
    if the verification is removed from either call site.
    """

    def test_a_head_note_that_renders_wrong_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            before = snapshot(work)
            real = migrate._render

            def wrong(item, body):
                return real(item, body).replace(
                    "hook: engine state", "hook: something else"
                )

            with (
                mock.patch.object(migrate, "_render", wrong),
                self.assertRaises(MigrationError) as caught,
            ):
                apply_proposal(proposal(entry()), {"work": work})

            self.assertIn("hook", str(caught.exception))
            self.assertEqual(snapshot(work), before)

    def test_a_head_stub_that_renders_wrong_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = make_tree(Path(raw) / "vault-work", "work")
            before = snapshot(work)
            real = migrate._render

            def wrong(item, body):
                text = real(item, body)
                if body.startswith(migrate.STUB_LEAD):
                    return text.replace("status: active", "status: done")
                return text

            with (
                mock.patch.object(migrate, "_render", wrong),
                self.assertRaises(MigrationError) as caught,
            ):
                apply_proposal(
                    proposal(entry(body="y" * (CAPS["project"] + 5000))),
                    {"work": work},
                )

            self.assertIn("status", str(caught.exception))
            self.assertEqual(snapshot(work), before)


class SourceDirectoryTests(unittest.TestCase):
    def test_an_unreadable_source_directory_is_an_error(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        with tempfile.TemporaryDirectory() as raw:
            memory = Path(raw) / "memory"
            memory.mkdir()
            (memory / "project_one.md").write_text("Body.\n", encoding="utf-8")
            memory.chmod(0o000)
            try:
                with self.assertRaises(MigrationError) as caught:
                    propose(memory_dir=memory, remember_dir=None)
            finally:
                memory.chmod(0o755)
            self.assertIn("memory", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
