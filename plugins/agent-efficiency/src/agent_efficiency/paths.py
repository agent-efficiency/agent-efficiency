"""Filesystem locations used by both hook and CLI entrypoints."""

from __future__ import annotations

import os
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = PACKAGE_DIR.parents[1]

# The data folder holds session history and the list of vault trees, so only
# its owner may read it. These modes are set on what this package creates,
# whatever the umask. Existing folders and files keep their modes; doctor
# reports a data folder that others can read.
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def make_private_dir(path: Path) -> None:
    """Create ``path`` and any missing parents with mode 0700.

    Only folders this call creates are given the private mode. A folder that
    already exists is left as it is.
    """

    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for folder in reversed(missing):
        try:
            folder.mkdir(mode=PRIVATE_DIR_MODE)
        except FileExistsError:
            continue
        os.chmod(folder, PRIVATE_DIR_MODE)


def create_private_file(path: Path) -> bool:
    """Create an empty file with mode 0600 unless it exists. Return True if new."""

    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE
        )
    except FileExistsError:
        return False
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
    finally:
        os.close(descriptor)
    return True


def is_private(path: Path) -> bool:
    """Whether no one but the owner can read or enter ``path``."""

    try:
        return path.stat().st_mode & 0o077 == 0
    except OSError:
        return True


def data_dir(explicit: str | Path | None = None) -> Path:
    """Return the writable persistent data directory.

    Plugin-provided directories take precedence over the generic XDG fallback,
    while AGENT_EFFICIENCY_DATA is an explicit operator override useful for
    testing and standalone installs.
    """

    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    for key in ("AGENT_EFFICIENCY_DATA", "CLAUDE_PLUGIN_DATA", "PLUGIN_DATA"):
        value = os.environ.get(key)
        if value:
            return Path(value).expanduser().resolve()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return (base / "agent-efficiency").resolve()


class RuntimePaths:
    __slots__ = (
        "root",
        "database",
        "knowledge",
        "knowledge_cards",
        "active_pack",
        "capability_packs",
    )

    def __init__(
        self,
        *,
        root: Path,
        database: Path,
        knowledge: Path,
        knowledge_cards: Path,
        active_pack: Path,
        capability_packs: Path,
    ) -> None:
        self.root = root
        self.database = database
        self.knowledge = knowledge
        self.knowledge_cards = knowledge_cards
        self.active_pack = active_pack
        self.capability_packs = capability_packs

    @classmethod
    def from_root(cls, root: str | Path | None = None) -> "RuntimePaths":
        resolved = data_dir(root)
        knowledge = resolved / "knowledge"
        return cls(
            root=resolved,
            database=resolved / "agent-efficiency.db",
            knowledge=knowledge,
            knowledge_cards=knowledge / "cards",
            active_pack=knowledge / "active-pack.json",
            capability_packs=knowledge / "packs",
        )

    def ensure(self) -> None:
        make_private_dir(self.root)
        make_private_dir(self.knowledge_cards)
        make_private_dir(self.capability_packs)
