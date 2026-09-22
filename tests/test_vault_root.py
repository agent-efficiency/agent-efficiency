from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_efficiency.vault.root import (
    VaultRootError,
    VaultTree,
    find_tree,
    normalize_remote,
)


class RemoteNormalizationTests(unittest.TestCase):
    def test_ssh_and_https_forms_agree(self) -> None:
        expected = "git.example.com/example/vault-work"
        for url in (
            "git@git.example.com:example/vault-work.git",
            "https://git.example.com/example/vault-work.git",
            "https://git.example.com/example/vault-work",
            "ssh://git@git.example.com/example/vault-work.git",
            "git@Git.Example.com:example/vault-work",
        ):
            with self.subTest(url=url):
                self.assertEqual(normalize_remote(url), expected)


class TreeDiscoveryTests(unittest.TestCase):
    def test_finds_the_tree_from_a_nested_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".vault.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "classification": "work",
                        "preferred_remote": "origin",
                        "remote_url": "git@git.example.com:example/vault-work.git",
                    }
                ),
                encoding="utf-8",
            )
            nested = root / "projects" / "umbra"
            nested.mkdir(parents=True)
            tree = find_tree(nested)
            self.assertIsInstance(tree, VaultTree)
            self.assertEqual(tree.classification, "work")
            self.assertEqual(tree.root, root)
            self.assertEqual(
                tree.normalized_remote, "git.example.com/example/vault-work"
            )

    def test_raises_when_no_tree_marker_exists(self) -> None:
        with tempfile.TemporaryDirectory() as raw, self.assertRaises(VaultRootError):
            find_tree(Path(raw))

    def test_rejects_an_unknown_classification_in_the_marker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".vault.json").write_text(
                json.dumps({"schema": 1, "classification": "public"}),
                encoding="utf-8",
            )
            with self.assertRaises(VaultRootError):
                find_tree(root)


SUPPORTED_FORMS = (
    "git@git.example.com:example/vault-work.git",
    "deploy@git.example.com:example/vault-work.git",
    "git.example.com:example/vault-work",
    "https://git.example.com/example/vault-work",
    "https://git.example.com/example/vault-work.git/",
    "https://git.example.com:443/example/vault-work",
    "https://git.example.com:8443/example/vault-work",
    "http://git.example.com:80/example/vault-work",
    "http://git.example.com:8080/example/vault-work",
    "ssh://git@git.example.com:22/example/vault-work.git",
    "ssh://git.example.com/example/vault-work",
    "git://git.example.com/example/vault-work.git",
    "https://token@git.example.com/example/vault-work",
    "file:///srv/git/Repos/Vault.git",
    "/srv/git/Repos/Vault.git",
    "/tmp/allowed.git/",
    "../Local/Repo.git",
    "../local/repo.git",
    "~/vault",
    "",
)


class RemoteCanonicalFormTests(unittest.TestCase):
    def test_equivalent_spellings_agree(self) -> None:
        expected = "git.example.com/example/vault-work"
        for url in (
            "git@git.example.com:example/vault-work.git",
            "deploy@git.example.com:example/vault-work.git",
            "git.example.com:example/vault-work",
            "https://git.example.com/example/vault-work",
            "https://git.example.com/example/vault-work.git/",
            "https://git.example.com:443/example/vault-work",
            "http://Git.Example.com:80/example/vault-work",
            "ssh://git@git.example.com:22/example/vault-work.git",
            "ssh://git.example.com/example/vault-work",
            "git://git.example.com/example/vault-work.git",
            "https://token@git.example.com/example/vault-work",
        ):
            with self.subTest(url=url):
                self.assertEqual(normalize_remote(url), expected)

    def test_keeps_a_non_default_port(self) -> None:
        self.assertEqual(
            normalize_remote("https://git.example.com:8443/example/vault-work"),
            "git.example.com:8443/example/vault-work",
        )
        self.assertEqual(
            normalize_remote("http://git.example.com:8080/example/vault-work"),
            "git.example.com:8080/example/vault-work",
        )

    def test_different_repositories_do_not_collide(self) -> None:
        pairs = (
            (
                "https://git.example.com/example/vault-work",
                "https://git.example.com/example/vault-home",
            ),
            (
                "git@git.example.com:example/vault-work.git",
                "git@gitlab.com:example/vault-work.git",
            ),
            ("../Local/Repo.git", "../local/repo.git"),
            ("/srv/git/Repos/Vault", "/srv/git/repos/vault"),
            (
                "https://git.example.com:8443/example/vault-work",
                "https://git.example.com/example/vault-work",
            ),
        )
        for left, right in pairs:
            with self.subTest(left=left, right=right):
                self.assertNotEqual(normalize_remote(left), normalize_remote(right))

    def test_preserves_path_case_for_local_and_file_remotes(self) -> None:
        self.assertEqual(normalize_remote("../Local/Repo.git"), "../Local/Repo.git")
        self.assertEqual(
            normalize_remote("/srv/git/Repos/Vault.git"), "/srv/git/Repos/Vault.git"
        )
        self.assertEqual(
            normalize_remote("file:///srv/git/Repos/Vault.git"),
            "/srv/git/Repos/Vault.git",
        )

    def test_a_local_path_keeps_its_git_ending(self) -> None:
        """Two directories, one with .git in its name, are two repositories."""

        for bare, plain in (
            ("/tmp/allowed.git", "/tmp/allowed"),
            ("../Repo.git", "../Repo"),
            ("file:///srv/vault.git", "file:///srv/vault"),
            ("~/vault.git", "~/vault"),
        ):
            with self.subTest(bare=bare):
                self.assertNotEqual(normalize_remote(bare), normalize_remote(plain))
                self.assertTrue(normalize_remote(bare).endswith(".git"))

    def test_a_local_path_drops_only_a_trailing_separator(self) -> None:
        self.assertEqual(normalize_remote("/tmp/allowed.git/"), "/tmp/allowed.git")
        self.assertEqual(
            normalize_remote("file:///tmp/allowed.git/"), "/tmp/allowed.git"
        )

    def test_a_hosted_url_still_drops_a_trailing_git(self) -> None:
        self.assertEqual(
            normalize_remote("https://git.example.com/example/vault.git"),
            normalize_remote("https://git.example.com/example/vault"),
        )
        self.assertEqual(
            normalize_remote("git@git.example.com:example/vault.git"),
            normalize_remote("https://git.example.com/example/vault"),
        )

    def test_a_malformed_url_raises_the_module_error(self) -> None:
        for url in ("https://[oops/example/vault", "https://git.example.com:no/vault"):
            with self.subTest(url=url), self.assertRaises(VaultRootError):
                normalize_remote(url)

    def test_preserves_path_case_for_hosted_remotes(self) -> None:
        self.assertEqual(
            normalize_remote("https://Git.Example.com/Example/Vault-Work.git"),
            "git.example.com/Example/Vault-Work",
        )

    def test_is_idempotent_for_every_supported_form(self) -> None:
        for url in SUPPORTED_FORMS:
            with self.subTest(url=url):
                once = normalize_remote(url)
                self.assertEqual(normalize_remote(once), once)

    def test_empty_remote_stays_empty(self) -> None:
        self.assertEqual(normalize_remote(""), "")
        self.assertEqual(normalize_remote("   "), "")


class MalformedMarkerTests(unittest.TestCase):
    def _find(self, payload: str | bytes) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            marker = root / ".vault.json"
            if isinstance(payload, bytes):
                marker.write_bytes(payload)
            else:
                marker.write_text(payload, encoding="utf-8")
            find_tree(root)

    def test_rejects_non_object_json(self) -> None:
        for payload in ("[]", "null", '"a string"', "3", "true"):
            with self.subTest(payload=payload), self.assertRaises(VaultRootError):
                self._find(payload)

    def test_rejects_invalid_json(self) -> None:
        with self.assertRaises(VaultRootError):
            self._find("{not json")

    def test_rejects_invalid_utf8(self) -> None:
        with self.assertRaises(VaultRootError):
            self._find(b'{"schema": 1, "classification": "w\xffrk"}')

    def test_rejects_boolean_schema(self) -> None:
        with self.assertRaises(VaultRootError):
            self._find('{"schema": true, "classification": "work"}')

    def test_rejects_string_schema(self) -> None:
        with self.assertRaises(VaultRootError):
            self._find('{"schema": "1", "classification": "work"}')

    def test_rejects_missing_or_wrong_schema(self) -> None:
        for payload in (
            '{"classification": "work"}',
            '{"schema": 2, "classification": "work"}',
        ):
            with self.subTest(payload=payload), self.assertRaises(VaultRootError):
                self._find(payload)

    def test_rejects_non_string_classification(self) -> None:
        for payload in (
            '{"schema": 1, "classification": null}',
            '{"schema": 1, "classification": 7}',
            '{"schema": 1}',
        ):
            with self.subTest(payload=payload), self.assertRaises(VaultRootError):
                self._find(payload)

    def test_rejects_non_string_remote_url(self) -> None:
        for value in ("null", "7", "[]"):
            with self.subTest(value=value), self.assertRaises(VaultRootError):
                self._find(
                    '{"schema": 1, "classification": "work", "remote_url": '
                    + value
                    + "}"
                )

    def test_rejects_non_string_preferred_remote(self) -> None:
        with self.assertRaises(VaultRootError):
            self._find(
                '{"schema": 1, "classification": "work", "preferred_remote": null}'
            )

    def test_applies_defaults_for_absent_optional_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".vault.json").write_text(
                '{"schema": 1, "classification": "core"}', encoding="utf-8"
            )
            tree = find_tree(root)
            self.assertEqual(tree.preferred_remote, "origin")
            self.assertEqual(tree.remote_url, "")
            self.assertEqual(tree.normalized_remote, "")


if __name__ == "__main__":
    unittest.main()
