from __future__ import annotations

import unittest

from agent_efficiency.vault.frontmatter import FrontmatterError, parse_note


class FrontmatterGrammarTests(unittest.TestCase):
    def test_parses_scalars_and_lists(self) -> None:
        text = "---\nid: umbra\nrepos: [umbra-dev/umbra, other/repo]\n---\n\nBody line.\n"
        parsed = parse_note(text)
        self.assertEqual(parsed.fields["id"], "umbra")
        self.assertEqual(parsed.fields["repos"], ["umbra-dev/umbra", "other/repo"])
        self.assertEqual(parsed.body, "Body line.")

    def test_parses_empty_list(self) -> None:
        parsed = parse_note("---\nlinks: []\n---\nBody.\n")
        self.assertEqual(parsed.fields["links"], [])

    def test_rejects_missing_opening_delimiter(self) -> None:
        with self.assertRaises(FrontmatterError):
            parse_note("id: umbra\n")

    def test_rejects_missing_closing_delimiter(self) -> None:
        with self.assertRaises(FrontmatterError):
            parse_note("---\nid: umbra\n")

    def test_rejects_duplicate_key(self) -> None:
        with self.assertRaises(FrontmatterError):
            parse_note("---\nid: a\nid: b\n---\nBody.\n")

    def test_rejects_nested_structure(self) -> None:
        with self.assertRaises(FrontmatterError):
            parse_note("---\nmeta:\n  type: project\n---\nBody.\n")

    def test_rejects_empty_value(self) -> None:
        with self.assertRaises(FrontmatterError):
            parse_note("---\nid:\n---\nBody.\n")

    def test_error_names_the_line_number(self) -> None:
        with self.assertRaises(FrontmatterError) as caught:
            parse_note("---\nid: ok\nbroken\n---\nBody.\n")
        self.assertIn("line 3", str(caught.exception))


class DelimiterAndLineEndingTests(unittest.TestCase):
    def test_parses_crlf_input(self) -> None:
        text = "---\r\nid: umbra\r\nrepos: [a/b]\r\n---\r\nBody line.\r\n"
        parsed = parse_note(text)
        self.assertEqual(parsed.fields["id"], "umbra")
        self.assertEqual(parsed.fields["repos"], ["a/b"])
        self.assertEqual(parsed.body, "Body line.")

    def test_parses_cr_only_input(self) -> None:
        parsed = parse_note("---\rid: umbra\r---\rBody line.\r")
        self.assertEqual(parsed.fields["id"], "umbra")
        self.assertEqual(parsed.body, "Body line.")

    def test_accepts_padded_delimiters_on_both_sides(self) -> None:
        parsed = parse_note("  ---  \nid: umbra\n---  \nBody.\n")
        self.assertEqual(parsed.fields["id"], "umbra")
        self.assertEqual(parsed.body, "Body.")

    def test_missing_opening_delimiter_names_line_one(self) -> None:
        with self.assertRaises(FrontmatterError) as caught:
            parse_note("id: umbra\n")
        self.assertIn("line 1", str(caught.exception))

    def test_missing_closing_delimiter_names_a_line(self) -> None:
        with self.assertRaises(FrontmatterError) as caught:
            parse_note("---\nid: umbra\ntitle: t\n")
        self.assertIn("line 3", str(caught.exception))

    def test_rejects_empty_input(self) -> None:
        with self.assertRaises(FrontmatterError):
            parse_note("")


class IndentationTests(unittest.TestCase):
    def test_rejects_an_indented_key_that_has_a_value(self) -> None:
        with self.assertRaises(FrontmatterError) as caught:
            parse_note("---\nid: umbra\n  title: nested\n---\nBody.\n")
        message = str(caught.exception)
        self.assertIn("line 3", message)
        self.assertIn("indentation", message)

    def test_rejects_a_tab_indented_key_that_has_a_value(self) -> None:
        with self.assertRaises(FrontmatterError) as caught:
            parse_note("---\nid: umbra\n\ttitle: nested\n---\nBody.\n")
        self.assertIn("indentation", str(caught.exception))


class BodyPreservationTests(unittest.TestCase):
    def test_keeps_indentation_and_interior_blank_lines(self) -> None:
        text = "---\nid: a\n---\n\n\n    first\n\n  second   \n\n\n"
        self.assertEqual(parse_note(text).body, "    first\n\n  second   ")

    def test_keeps_a_leading_indented_code_block(self) -> None:
        text = "---\nid: a\n---\n    make test\nplain\n"
        self.assertEqual(parse_note(text).body, "    make test\nplain")

    def test_empty_body_is_empty_string(self) -> None:
        self.assertEqual(parse_note("---\nid: a\n---\n\n\n").body, "")


class ListItemCharacterTests(unittest.TestCase):
    def test_rejects_a_quoted_list_item(self) -> None:
        with self.assertRaises(FrontmatterError) as caught:
            parse_note('---\nrepos: ["a,b", c]\n---\nBody.\n')
        message = str(caught.exception)
        self.assertIn("line 2", message)
        self.assertIn('"a', message)

    def test_rejects_a_single_quoted_list_item(self) -> None:
        with self.assertRaises(FrontmatterError) as caught:
            parse_note("---\nrepos: ['a', b]\n---\nBody.\n")
        self.assertIn("line 2", str(caught.exception))

    def test_rejects_a_braced_list_item(self) -> None:
        with self.assertRaises(FrontmatterError) as caught:
            parse_note("---\nrepos: [{a: b}, c]\n---\nBody.\n")
        self.assertIn("line 2", str(caught.exception))

    def test_rejects_a_bracketed_list_item(self) -> None:
        with self.assertRaises(FrontmatterError):
            parse_note("---\nrepos: [[a], b]\n---\nBody.\n")

    def test_rejects_an_empty_list_item(self) -> None:
        with self.assertRaises(FrontmatterError):
            parse_note("---\nrepos: [a, , b]\n---\nBody.\n")

    def test_rejects_a_braced_scalar(self) -> None:
        with self.assertRaises(FrontmatterError) as caught:
            parse_note("---\nid: {a}\n---\nBody.\n")
        self.assertIn("line 2", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
