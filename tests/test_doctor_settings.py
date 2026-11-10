from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_efficiency.cli import main
from tests.test_doctor import isolated_home

VALID_TEXT = "0123456789abcdef" * 4 + "\n"


class DoctorCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.data = self.home / "data"
        self.project = self.home / "work" / "app"
        self.project.mkdir(parents=True)
        self.other = str(self.home / "other-data")

    def doctor(
        self, *extra: str, text: bool = False, env: dict[str, str] | None = None
    ) -> tuple[int, object]:
        arguments = ["--data-dir", str(self.data), "doctor", "--cwd", str(self.project)]
        if not text:
            arguments.append("--json")
        with (
            isolated_home(self.home),
            mock.patch.dict(os.environ, env or {}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            code = main([*arguments, *extra])
        value = output.getvalue()
        return code, (value if text else json.loads(value))


class DigestKeyDoctorTests(DoctorCase):
    def write_key(self, mode: int) -> Path:
        self.data.mkdir(mode=0o700)
        path = self.data / "digest.key"
        path.write_text(VALID_TEXT, encoding="ascii")
        os.chmod(path, mode)
        return path

    def test_a_missing_key_is_reported_and_not_created(self) -> None:
        code, doctor = self.doctor()
        self.assertEqual(code, 0)
        self.assertEqual(doctor["digest_key"]["state"], "missing")
        self.assertTrue(doctor["digest_key"]["ok"])
        self.assertFalse((self.data / "digest.key").exists())

    def test_a_valid_key_is_reported_and_never_printed(self) -> None:
        self.write_key(0o600)
        code, doctor = self.doctor()
        self.assertEqual(code, 0)
        self.assertEqual(doctor["digest_key"]["state"], "valid")
        self.assertNotIn(VALID_TEXT.strip(), json.dumps(doctor))
        _, text = self.doctor(text=True)
        self.assertIn("digest_key:", text)
        self.assertNotIn(VALID_TEXT.strip(), text)

    def test_an_invalid_key_fails_and_is_left_alone(self) -> None:
        path = self.write_key(0o644)
        code, doctor = self.doctor()
        self.assertEqual(code, 1)
        self.assertFalse(doctor["ok"])
        self.assertEqual(doctor["digest_key"]["state"], "mode")
        self.assertIn("Move it aside", doctor["digest_key"]["message"])
        self.assertNotIn(VALID_TEXT.strip(), json.dumps(doctor))
        self.assertEqual(path.read_text(encoding="ascii"), VALID_TEXT)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
