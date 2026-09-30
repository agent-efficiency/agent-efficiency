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

from agent_efficiency.folder_lock import folder_lock
from agent_efficiency.paths import make_private_dir
from agent_efficiency.vault.root import VaultTree, find_tree
from agent_efficiency.vault.schema import CLASSIFICATIONS

CONFIG_NAME = "vault.json"
CONFIG_SCHEMA = 1


class VaultConfigError(ValueError):
    """Raised when the tree list is unreadable or names an invalid tree."""


def config_path(data_root: Path) -> Path:
    return data_root / CONFIG_NAME


def load_trees(data_root: Path) -> tuple[VaultTree, ...]:
    """Return the registered trees in classification order. No file means none.

    One entry that does not resolve to a tree fails the whole list, so a
    broken configuration is reported rather than silently narrowed.
    """

    config = config_path(data_root)
    trees: list[VaultTree] = []
    seen: dict[str, Path] = {}
    for position, raw in enumerate(_paths(config), start=1):
        try:
            tree = _tree(Path(raw))
        except VaultConfigError as exc:
            raise VaultConfigError(
                f"vault tree entry {position}: {str(exc).rstrip('.')}. "
                f"Fix or remove the entry in {config}."
            ) from None
        if tree.classification in seen:
            raise VaultConfigError(
                f"two registered trees are classified {tree.classification!r}: "
                f"{seen[tree.classification]} and {tree.root}. "
                f"Fix or remove the entry in {config}."
            )
        seen[tree.classification] = tree.root
        trees.append(tree)
    return tuple(
        sorted(trees, key=lambda tree: CLASSIFICATIONS.index(tree.classification))
    )


def stale_entries(data_root: Path) -> list[str]:
    """Return the entries that no longer resolve to a vault tree root.

    These are the entries ``register_tree`` drops.
    """

    return _resolve(_paths(config_path(data_root)))[1]


def register_tree(data_root: Path, path: Path) -> VaultTree:
    """Add a tree root to the list. Registering the same root again is a no-op.

    Entries that no longer resolve to a tree, such as a tree that was moved or
    deleted, are dropped from the list so they cannot block the new one.
    """

    tree = _tree(path)
    config = config_path(data_root)
    current = _paths(config)
    kept, stale = _resolve(current)
    for other in kept.values():
        if other.root == tree.root:
            if stale:
                _save(config, list(kept))
            return tree
        if other.classification == tree.classification:
            raise VaultConfigError(
                f"a {tree.classification} tree is already registered at {other.root}"
            )
    _save(config, [*kept, str(tree.root)])
    return tree


def _save(config: Path, trees: list[str]) -> None:
    try:
        _write(config, trees)
    except OSError as exc:
        raise VaultConfigError(f"{config} could not be written: {exc}") from None


def _resolve(entries: list[str]) -> tuple[dict[str, VaultTree], list[str]]:
    """Split entries into those that resolve to a tree and those that do not."""

    kept: dict[str, VaultTree] = {}
    stale: list[str] = []
    for raw in entries:
        try:
            kept[raw] = _tree(Path(raw))
        except VaultConfigError:
            stale.append(raw)
    return kept, stale


def _tree(path: Path) -> VaultTree:
    try:
        resolved = path.expanduser().resolve()
        tree = find_tree(resolved)
    except (OSError, ValueError, RuntimeError) as exc:
        raise VaultConfigError(str(exc)) from None
    if tree.root != resolved:
        raise VaultConfigError(
            f"{resolved} is inside the vault tree at {tree.root}; "
            "register the tree root"
        )
    return tree


def _paths(config: Path) -> list[str]:
    try:
        # On Python 3.11 to 3.13 ``is_file`` raises on a denied parent.
        if not config.is_file():
            return []
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError) as exc:
        raise VaultConfigError(f"{config} could not be read: {exc}") from None
    schema = data.get("schema") if isinstance(data, dict) else None
    if isinstance(schema, bool) or schema != CONFIG_SCHEMA:
        raise VaultConfigError(
            f"{config} must be a JSON object with schema {CONFIG_SCHEMA}"
        )
    trees = data.get("trees")
    if not isinstance(trees, list) or not all(isinstance(item, str) for item in trees):
        raise VaultConfigError(f"{config} trees must be a list of paths")
    for position, raw in enumerate(trees, start=1):
        try:
            absolute = bool(raw) and Path(raw).expanduser().is_absolute()
        except RuntimeError:
            absolute = False
        if not absolute:
            raise VaultConfigError(
                f"vault tree entry {position} must be an absolute path. "
                f"Fix or remove the entry in {config}."
            )
    return trees


def _write(config: Path, trees: list[str]) -> None:
    make_private_dir(config.parent)
    # A copy of an older store into this folder writes vault.json under the
    # same lock, and never replaces a list that is already there.
    with folder_lock(config.parent, None):
        _replace(config, trees)


def _replace(config: Path, trees: list[str]) -> None:
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
