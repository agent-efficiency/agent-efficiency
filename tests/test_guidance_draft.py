import json
import io
from contextlib import redirect_stdout
import tempfile
import unittest
from pathlib import Path

from agent_efficiency.guidance_draft import draft


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "src/auth").mkdir(parents=True)
        self.note = self.root / "project.md"
        self.note.write_text(
            "# Project\nPrivate unrelated prose.\n## Search locations\n- auth: src/auth\n## Other\nDo not include this.\n"
        )

    def test_explicit_location_becomes_an_inactive_proposal(self):
        data = draft(self.note, self.root, "2099-01-01")
        self.assertEqual(data["suggestions"][0]["terms"], ["auth"])
        self.assertEqual(data["suggestions"][0]["target"], "src/auth")
        self.assertNotIn("Private unrelated", json.dumps(data))
        self.assertFalse((self.root / "agent-efficiency-guidance.json").exists())
        self.assertNotIn("approved", json.dumps(data))

    def test_ambiguous_or_missing_section_is_refused(self):
        for text in (
            "Search auth in src/auth",
            "## Search locations\nMaybe use src/auth",
            "## Search locations\n- auth: src/auth\n- auth: src/auth",
        ):
            self.note.write_text(text)
            with self.assertRaises(ValueError):
                draft(self.note, self.root, "2099-01-01")

    def test_invalid_target_and_expired_draft_are_refused(self):
        with self.assertRaises(ValueError):
            draft(self.note, self.root, "2000-01-01")
        self.note.write_text("## Search locations\n- auth: ../outside\n")
        with self.assertRaises(ValueError):
            draft(self.note, self.root, "2099-01-01")

    def test_changed_note_changes_source_version(self):
        first = draft(self.note, self.root, "2099-01-01")
        self.note.write_text(self.note.read_text() + "Updated.\n")
        second = draft(self.note, self.root, "2099-01-01")
        self.assertNotEqual(
            first["suggestions"][0]["source"], second["suggestions"][0]["source"]
        )

    def test_cli_outputs_reviewable_json(self):
        from agent_efficiency.cli import main

        with redirect_stdout(io.StringIO()) as output:
            code = main(
                [
                    "guidance",
                    "draft",
                    "--note",
                    str(self.note),
                    "--cwd",
                    str(self.root),
                    "--expires",
                    "2099-01-01",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(output.getvalue())["suggestions"][0]["target"], "src/auth"
        )
