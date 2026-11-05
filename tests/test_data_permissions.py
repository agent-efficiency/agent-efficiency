from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from agent_efficiency.cli import main
from agent_efficiency.store import Store
from tests.test_doctor import isolated_home
from tests.vault_fixtures import make_vault_tree


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@contextlib.contextmanager
def umask(value: int):
    previous = os.umask(value)
    try:
        yield
    finally:
        os.umask(previous)


class DataPermissionTests(unittest.TestCase):
    def test_new_store_is_private_under_a_permissive_umask(self) -> None:
        with tempfile.TemporaryDirectory() as temp, umask(0o022):
            root = Path(temp) / "share" / "agent-efficiency"
            store = Store(root)
            store.set_default_mode("observe")
            self.assertEqual(mode(root), 0o700)
            self.assertEqual(mode(root.parent), 0o700)
            self.assertEqual(mode(store.paths.database), 0o600)
            for sidecar in ("-wal", "-shm"):
                path = Path(f"{store.paths.database}{sidecar}")
                if path.exists():
                    self.assertEqual(mode(path), 0o600, sidecar)
            for folder in (
                store.paths.knowledge,
                store.paths.knowledge_cards,
                store.paths.capability_packs,
            ):
                self.assertEqual(mode(folder), 0o700, folder)

    def test_schema_backup_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp, umask(0o022):
            store = Store(temp)
            with store.connect() as conn:
                conn.execute(
                    "UPDATE settings SET value = '6' WHERE key = 'schema_version'"
                )
            store.ensure_current_schema()
            backup = Path(temp) / "agent-efficiency.db.schema-6.bak"
            self.assertTrue(backup.is_file())
            self.assertEqual(mode(backup), 0o600)

    def test_vault_register_writes_a_private_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp, umask(0o022):
            tree = make_vault_tree(Path(temp) / "vault", "work", [], commit=False)
            data = Path(temp) / "fresh" / "data"
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["--data-dir", str(data), "vault", "register", str(tree)])
            self.assertEqual(code, 0)
            self.assertEqual(mode(data), 0o700)
            self.assertEqual(mode(data / "vault.json"), 0o600)

    def test_existing_folder_mode_is_kept_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp, umask(0o022):
            root = Path(temp) / "data"
            root.mkdir(mode=0o755)
            os.chmod(root, 0o755)
            Store(root)
            self.assertEqual(mode(root), 0o755)
            with (
                isolated_home(temp),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                main(["--data-dir", str(root), "doctor", "--json"])
            doctor = json.loads(output.getvalue())
            self.assertFalse(doctor["data_dir_private"])
            self.assertIn("chmod -R go-rwx", doctor["data_dir_permissions"])
            self.assertEqual(mode(root), 0o755)

    def doctor_json(self, temp: str, root: Path) -> dict:
        with (
            isolated_home(temp),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            main(["--data-dir", str(root), "doctor", "--json"])
        return json.loads(output.getvalue())

    def test_loose_file_in_a_private_folder_is_described_as_unreachable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp, umask(0o022):
            root = Path(temp) / "my data"
            store = Store(root)
            store.set_default_mode("observe")
            self.assertEqual(mode(root), 0o700)
            os.chmod(store.paths.database, 0o644)
            (root / "vault.json").write_text("{}", encoding="utf-8")
            os.chmod(root / "vault.json", 0o640)
            doctor = self.doctor_json(temp, root)
            self.assertFalse(doctor["data_dir_private"])
            self.assertEqual(
                doctor["data_dir_readable_by_others"],
                ["agent-efficiency.db", "vault.json"],
            )
            message = doctor["data_dir_permissions"]
            self.assertIn(
                "the data folder is private, so other users cannot reach the "
                "files in it",
                message,
            )
            self.assertIn("agent-efficiency.db, vault.json", message)
            self.assertIn(f"chmod -R go-rwx '{root}'", message)
            self.assertEqual(mode(store.paths.database), 0o644)

    def test_open_folder_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as temp, umask(0o022):
            root = Path(temp) / "data"
            Store(root)
            os.chmod(root, 0o755)
            doctor = self.doctor_json(temp, root)
            self.assertEqual(doctor["data_dir_readable_by_others"], ["."])
            message = doctor["data_dir_permissions"]
            self.assertIn("other users can open the data folder", message)
            self.assertIn(f"chmod -R go-rwx {root}", message)

    def test_link_to_a_readable_file_outside_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp, umask(0o022):
            root = Path(temp) / "data"
            store = Store(root)
            outside = Path(temp) / "shared folder" / "agent-efficiency.db"
            outside.parent.mkdir()
            store.paths.database.rename(outside)
            os.chmod(outside, 0o644)
            store.paths.database.symlink_to(outside)
            doctor = self.doctor_json(temp, root)
            self.assertFalse(doctor["data_dir_private"])
            self.assertEqual(
                doctor["data_dir_readable_by_others"],
                [f"agent-efficiency.db -> {outside}"],
            )
            message = doctor["data_dir_permissions"]
            self.assertNotIn("only you can read", message)
            self.assertIn("links to files outside the data folder", message)
            self.assertIn(f"chmod go-rwx '{outside}'", message)

    def test_link_to_a_private_file_outside_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp, umask(0o022):
            root = Path(temp) / "data"
            store = Store(root)
            outside = Path(temp) / "elsewhere.db"
            store.paths.database.rename(outside)
            os.chmod(outside, 0o600)
            store.paths.database.symlink_to(outside)
            doctor = self.doctor_json(temp, root)
            self.assertTrue(doctor["data_dir_private"])
            self.assertEqual(doctor["data_dir_readable_by_others"], [])

    def test_private_store_is_reported_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp, umask(0o022):
            root = Path(temp) / "data"
            with (
                isolated_home(temp),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                main(["--data-dir", str(root), "doctor", "--json"])
            doctor = json.loads(output.getvalue())
            self.assertTrue(doctor["data_dir_private"])


if __name__ == "__main__":
    unittest.main()
