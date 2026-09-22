"""The list of registered vault trees, kept beside the runtime database.

The list holds filesystem paths, which are vault metadata, and no vault path
may enter the SQLite store. So the list is a small JSON file in the runtime
data directory rather than a settings row. Keeping it in the data directory
also isolates it per data root, so a test store never sees a real vault.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from agent_efficiency.vault.root import VaultRootError, VaultTree, find_tree
from agent_efficiency.vault.schema import CLASSIFICATIONS

CONFIG_NAME = "vault.json"
CONFIG_SCHEMA = 1


class VaultConfigError(ValueError):
    """Raised when the tree list is unreadable or names an invalid tree."""


def config_path(data_root: Path) -> Path:
    return data_root / CONFIG_NAME


def load_trees(data_root: Path) -> tuple[VaultTree, ...]:
    """Return the registered trees in classification order. No file means none."""

    trees = [_tree(Path(raw)) for raw in _paths(config_path(data_root))]
    seen: dict[str, Path] = {}
    for tree in trees:
        if tree.classification in seen:
            raise VaultConfigError(
                f"two registered trees are classified {tree.classification!r}: "
                f"{seen[tree.classification]} and {tree.root}"
            )
        seen[tree.classification] = tree.root
    return tuple(
        sorted(trees, key=lambda tree: CLASSIFICATIONS.index(tree.classification))
    )


def register_tree(data_root: Path, path: Path) -> VaultTree:
    """Add a tree root to the list. Registering the same root again is a no-op."""

    tree = _tree(path)
    config = config_path(data_root)
    current = _paths(config)
    for raw in current:
        other = _tree(Path(raw))
        if other.root == tree.root:
            return tree
        if other.classification == tree.classification:
            raise VaultConfigError(
                f"a {tree.classification} tree is already registered at {other.root}"
            )
    _write(config, [*current, str(tree.root)])
    return tree


def _tree(path: Path) -> VaultTree:
    resolved = path.expanduser().resolve()
    try:
        tree = find_tree(resolved)
    except VaultRootError as exc:
        raise VaultConfigError(str(exc)) from None
    if tree.root != resolved:
        raise VaultConfigError(
            f"{resolved} is inside the vault tree at {tree.root}; "
            "register the tree root"
        )
    return tree


def _paths(config: Path) -> list[str]:
    if not config.is_file():
        return []
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VaultConfigError(f"{config} could not be read: {exc}") from None
    schema = data.get("schema") if isinstance(data, dict) else None
    if isinstance(schema, bool) or schema != CONFIG_SCHEMA:
        raise VaultConfigError(
            f"{config} must be a JSON object with schema {CONFIG_SCHEMA}"
        )
    trees = data.get("trees")
    if not isinstance(trees, list) or not all(isinstance(item, str) for item in trees):
        raise VaultConfigError(f"{config} trees must be a list of paths")
    return trees


def _write(config: Path, trees: list[str]) -> None:
    config.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"schema": CONFIG_SCHEMA, "trees": trees}, indent=2) + "\n"
    handle, staged = tempfile.mkstemp(
        dir=config.parent, prefix=".vault-", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(staged, config)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise
