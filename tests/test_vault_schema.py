from __future__ import annotations

import unittest

from agent_efficiency.vault.frontmatter import FrontmatterError
from agent_efficiency.vault.schema import CAPS, Note, load_note

VALID = (
    "---\n"
    "schema: 1\n"
    "id: umbra\n"
    "title: umbra example application engine\n"
    "type: project\n"
    "classification: work\n"
    "status: active\n"
    "updated: 2026-09-21\n"
    "repos: [umbra-dev/umbra]\n"
    "paths: [~/checkout/repos/umbra]\n"
    "hook: engine state and next move\n"
    "---\n"
    "Next: applicability slice.\n"
)


class NoteSchemaTests(unittest.TestCase):
    def test_loads_a_valid_note(self) -> None:
        note = load_note(VALID)
        self.assertIsInstance(note, Note)
        self.assertEqual(note.id, "umbra")
        self.assertEqual(note.repos, ("umbra-dev/umbra",))
        self.assertEqual(note.branches, ())

    def test_rejects_missing_required_field(self) -> None:
        text = VALID.replace("status: active\n", "")
        with self.assertRaises(FrontmatterError) as caught:
            load_note(text)
        self.assertIn("status", str(caught.exception))

    def test_rejects_unknown_type(self) -> None:
        with self.assertRaises(FrontmatterError):
            load_note(VALID.replace("type: project", "type: notes"))

    def test_rejects_unknown_classification(self) -> None:
        with self.assertRaises(FrontmatterError):
            load_note(VALID.replace("classification: work", "classification: public"))

    def test_rejects_unsupported_schema_version(self) -> None:
        with self.assertRaises(FrontmatterError):
            load_note(VALID.replace("schema: 1", "schema: 2"))

    def test_records_size_and_cap_state(self) -> None:
        note = load_note(VALID)
        self.assertEqual(note.size, len(VALID))
        self.assertTrue(note.within_cap)
        self.assertEqual(CAPS["project"], 3000)

    def test_reports_a_note_over_its_cap(self) -> None:
        oversized = VALID.replace(
            "Next: applicability slice.", "x" * (CAPS["project"] + 1)
        )
        note = load_note(oversized)
        self.assertFalse(note.within_cap)


EXPECTED_CAPS = {
    "project": 3000,
    "feedback": 3500,
    "reference": 3500,
    "doctrine": 3500,
    "session": 1000,
}


def note_of_size(note_type: str, size: int) -> str:
    """Build a valid note of exactly ``size`` code points, header included."""
    head = (
        "---\n"
        "schema: 1\n"
        "id: sized\n"
        "title: sized note\n"
        f"type: {note_type}\n"
        "classification: core\n"
        "status: active\n"
        "hook: sized hook\n"
        "---\n"
    )
    padding = size - len(head)
    if padding < 1:
        raise AssertionError(f"size {size} is smaller than the header")
    return head + "x" * padding


class SizeAndCapTests(unittest.TestCase):
    def test_size_counts_the_whole_input_including_frontmatter(self) -> None:
        note = load_note(VALID)
        self.assertEqual(note.size, 255)
        self.assertEqual(note.size, len(VALID))
        self.assertGreater(note.size, len(note.body))

    def test_size_counts_code_points_not_bytes(self) -> None:
        text = VALID.replace("Next: applicability slice.", "Ned")
        note = load_note(text)
        self.assertEqual(note.size, len(text))
        self.assertEqual(len(text.encode("utf-8")), note.size)

    def test_caps_hold_their_documented_values(self) -> None:
        self.assertEqual(CAPS, EXPECTED_CAPS)

    def test_a_note_at_exactly_its_cap_is_within_cap(self) -> None:
        for note_type, cap in EXPECTED_CAPS.items():
            with self.subTest(type=note_type):
                note = load_note(note_of_size(note_type, cap))
                self.assertEqual(note.size, cap)
                self.assertTrue(note.within_cap)

    def test_a_note_one_over_its_cap_is_not_within_cap(self) -> None:
        for note_type, cap in EXPECTED_CAPS.items():
            with self.subTest(type=note_type):
                note = load_note(note_of_size(note_type, cap + 1))
                self.assertEqual(note.size, cap + 1)
                self.assertFalse(note.within_cap)

    def test_a_note_one_under_its_cap_is_within_cap(self) -> None:
        for note_type, cap in EXPECTED_CAPS.items():
            with self.subTest(type=note_type):
                note = load_note(note_of_size(note_type, cap - 1))
                self.assertTrue(note.within_cap)


class FieldValidationTests(unittest.TestCase):
    def test_rejects_unknown_status(self) -> None:
        with self.assertRaises(FrontmatterError) as caught:
            load_note(VALID.replace("status: active", "status: paused"))
        message = str(caught.exception)
        self.assertIn("status", message)
        self.assertIn("paused", message)

    def test_accepts_every_known_status(self) -> None:
        for status in ("active", "dormant", "done"):
            with self.subTest(status=status):
                note = load_note(VALID.replace("status: active", f"status: {status}"))
                self.assertEqual(note.status, status)

    def test_rejects_an_unknown_key(self) -> None:
        with self.assertRaises(FrontmatterError) as caught:
            load_note(VALID.replace("repos: [umbra-dev/umbra]", "branch: [main]"))
        self.assertIn("branch", str(caught.exception))

    def test_rejects_an_unknown_key_beside_a_complete_note(self) -> None:
        with self.assertRaises(FrontmatterError) as caught:
            load_note(VALID.replace("hook: ", "notes: extra\nhook: "))
        self.assertIn("notes", str(caught.exception))

    def test_accepts_every_known_key(self) -> None:
        text = VALID.replace(
            "hook: ", "branches: [main]\nlinks: [other-note]\nhook: "
        )
        note = load_note(text)
        self.assertEqual(note.branches, ("main",))
        self.assertEqual(note.links, ("other-note",))

    def test_rejects_an_id_with_a_forward_slash(self) -> None:
        with self.assertRaises(FrontmatterError) as caught:
            load_note(VALID.replace("id: umbra", "id: work/umbra"))
        self.assertIn("id", str(caught.exception))

    def test_rejects_an_id_with_a_backslash(self) -> None:
        with self.assertRaises(FrontmatterError) as caught:
            load_note(VALID.replace("id: umbra", "id: work\\umbra"))
        self.assertIn("id", str(caught.exception))

    def test_rejects_an_id_that_traverses_upward(self) -> None:
        with self.assertRaises(FrontmatterError):
            load_note(VALID.replace("id: umbra", "id: ../umbra"))

    def test_rejects_a_dot_id(self) -> None:
        for value in (".", ".."):
            with self.subTest(value=value), self.assertRaises(FrontmatterError):
                load_note(VALID.replace("id: umbra", f"id: {value}"))

    def test_rejects_a_list_where_a_scalar_belongs(self) -> None:
        with self.assertRaises(FrontmatterError) as caught:
            load_note(
                VALID.replace(
                    "title: umbra example application engine", "title: [a, b]"
                )
            )
        self.assertIn("title", str(caught.exception))

    def test_rejects_a_scalar_where_a_list_belongs(self) -> None:
        with self.assertRaises(FrontmatterError) as caught:
            load_note(
                VALID.replace("repos: [umbra-dev/umbra]", "repos: umbra-dev/umbra")
            )
        self.assertIn("repos", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
