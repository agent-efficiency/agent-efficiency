from __future__ import annotations

import hashlib
import hmac
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_efficiency import digests
from agent_efficiency.digests import (
    FALLBACK_KEY,
    KEY_NAME,
    keyed_digest,
    load_key,
    read_key,
)
from agent_efficiency.paths import PLUGIN_ROOT

VALID_TEXT = "0123456789abcdef" * 4 + "\n"


class KeyedDigestTests(unittest.TestCase):
    def test_digest_is_hmac_sha256_over_label_zero_and_value(self) -> None:
        key = bytes(range(32))
        label = b"agent-efficiency/v1/call"
        expected = hmac.new(key, label + b"\x00" + b"value", hashlib.sha256)
        self.assertEqual(
            keyed_digest(key, label, b"value"), expected.hexdigest().lower()
        )

    def test_labels_must_carry_the_tool_prefix(self) -> None:
        with self.assertRaises(ValueError):
            keyed_digest(b"k", b"call", b"value")

    def test_different_keys_give_different_digests(self) -> None:
        label = b"agent-efficiency/v1/call"
        self.assertNotEqual(
            keyed_digest(b"a" * 32, label, b"v"), keyed_digest(b"b" * 32, label, b"v")
        )


class KeyFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data"
        self.root.mkdir(mode=0o700)
        self.path = self.root / KEY_NAME

    def write_key(self, text: str = VALID_TEXT, mode: int = 0o600) -> None:
        self.path.write_text(text, encoding="ascii")
        os.chmod(self.path, mode)

    def assert_left_alone(self, before: bytes, mode: int) -> None:
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), mode)

    def test_first_use_creates_a_private_key_file(self) -> None:
        key = load_key(self.root)
        data = self.path.read_bytes()
        self.assertRegex(data.decode("ascii"), r"\A[0-9a-f]{64}\n\Z")
        self.assertEqual(key, bytes.fromhex(data[:64].decode("ascii")))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(sorted(os.listdir(self.root)), [KEY_NAME])
        self.assertEqual(read_key(self.root).state, "valid")

    def test_later_uses_read_the_same_key(self) -> None:
        first = load_key(self.root)
        data = self.path.read_bytes()
        self.assertEqual(load_key(self.root), first)
        self.assertEqual(self.path.read_bytes(), data)

    def test_first_use_creates_a_missing_data_folder(self) -> None:
        folder = self.root / "new"
        load_key(folder)
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(read_key(folder).state, "valid")

    def test_reading_never_creates_the_key(self) -> None:
        found = read_key(self.root)
        self.assertEqual((found.key, found.state), (FALLBACK_KEY, "missing"))
        self.assertFalse(self.path.exists())

    def test_a_valid_key_file_is_read(self) -> None:
        self.write_key()
        found = read_key(self.root)
        self.assertEqual(found.state, "valid")
        self.assertEqual(found.key, bytes.fromhex(VALID_TEXT[:64]))

    def test_a_key_shared_with_others_is_not_used_or_changed(self) -> None:
        self.write_key(mode=0o640)
        self.assertEqual(read_key(self.root).state, "mode")
        self.assertEqual(load_key(self.root), FALLBACK_KEY)
        self.assert_left_alone(VALID_TEXT.encode(), 0o640)

    def test_a_key_of_the_wrong_size_is_not_used_or_changed(self) -> None:
        self.write_key(VALID_TEXT.strip())
        self.assertEqual(read_key(self.root).state, "size")
        self.assertEqual(load_key(self.root), FALLBACK_KEY)
        self.assert_left_alone(VALID_TEXT.strip().encode(), 0o600)

    def test_a_key_that_is_not_lowercase_hex_is_not_used_or_changed(self) -> None:
        self.write_key(VALID_TEXT.upper())
        self.assertEqual(read_key(self.root).state, "content")
        self.assertEqual(load_key(self.root), FALLBACK_KEY)
        self.assert_left_alone(VALID_TEXT.upper().encode(), 0o600)

    def test_a_key_owned_by_someone_else_is_not_used(self) -> None:
        self.write_key()
        with mock.patch.object(digests.os, "geteuid", return_value=os.geteuid() + 1):
            self.assertEqual(read_key(self.root).state, "owner")
        self.assert_left_alone(VALID_TEXT.encode(), 0o600)

    def test_a_link_to_a_valid_key_is_not_followed(self) -> None:
        elsewhere = Path(self.temp.name) / "elsewhere.key"
        elsewhere.write_text(VALID_TEXT, encoding="ascii")
        os.chmod(elsewhere, 0o600)
        self.path.symlink_to(elsewhere)
        self.assertEqual(read_key(self.root).state, "file type")
        self.assertEqual(load_key(self.root), FALLBACK_KEY)
        self.assertTrue(self.path.is_symlink())

    def test_a_folder_or_pipe_in_place_of_the_key_is_not_used(self) -> None:
        self.path.mkdir()
        self.assertEqual(read_key(self.root).state, "file type")
        self.assertEqual(load_key(self.root), FALLBACK_KEY)
        self.assertTrue(self.path.is_dir())
        self.path.rmdir()
        os.mkfifo(self.path, 0o600)
        self.assertEqual(read_key(self.root).state, "file type")
        self.assertEqual(load_key(self.root), FALLBACK_KEY)

    @unittest.skipIf(os.geteuid() == 0, "root can write to a read-only folder")
    def test_a_folder_that_cannot_hold_the_key_falls_back(self) -> None:
        os.chmod(self.root, 0o500)
        self.addCleanup(os.chmod, self.root, 0o700)
        self.assertEqual(load_key(self.root), FALLBACK_KEY)
        self.assertEqual(read_key(self.root).state, "missing")

    def test_without_owner_checks_the_key_is_still_created_and_read(self) -> None:
        # Some systems have no effective user id, so there is no owner to check.
        saved = os.geteuid
        del os.geteuid
        self.addCleanup(setattr, os, "geteuid", saved)
        key = load_key(self.root)
        self.assertNotEqual(key, FALLBACK_KEY)
        self.assertEqual(load_key(self.root), key)
        found = read_key(self.root)
        self.assertEqual((found.key, found.state), (key, "valid"))

    def test_any_failure_while_loading_gives_the_fallback_key(self) -> None:
        with mock.patch.object(digests, "read_key", side_effect=RuntimeError):
            self.assertEqual(load_key(self.root), FALLBACK_KEY)

    def test_the_process_that_loses_a_creation_race_keeps_the_winner(self) -> None:
        self.write_key()
        digests._create_key(self.root)
        self.assert_left_alone(VALID_TEXT.encode(), 0o600)
        self.assertEqual(sorted(os.listdir(self.root)), [KEY_NAME])

    def test_without_hard_links_the_key_is_still_created(self) -> None:
        with mock.patch.object(digests.os, "link", side_effect=PermissionError):
            key = load_key(self.root)
        self.assertEqual(read_key(self.root).state, "valid")
        self.assertEqual(load_key(self.root), key)
        self.assertEqual(sorted(os.listdir(self.root)), [KEY_NAME])

    def test_racing_processes_agree_on_one_key(self) -> None:
        script = (
            "import sys; from agent_efficiency.digests import load_key; "
            "print(load_key(sys.argv[1]).hex())"
        )
        environment = {**os.environ, "PYTHONPATH": str(PLUGIN_ROOT / "src")}
        racers = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.root)],
                env=environment,
                stdout=subprocess.PIPE,
                text=True,
            )
            for _ in range(6)
        ]
        keys = {racer.communicate(timeout=30)[0].strip() for racer in racers}
        self.assertEqual(keys, {self.path.read_text(encoding="ascii")[:64]})


if __name__ == "__main__":
    unittest.main()
