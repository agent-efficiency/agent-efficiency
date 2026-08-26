"""Filesystem locations used by both hook and CLI entrypoints."""

from __future__ import annotations

import os
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = PACKAGE_DIR.parents[1]


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
        self.root.mkdir(parents=True, exist_ok=True)
        self.knowledge_cards.mkdir(parents=True, exist_ok=True)
        self.capability_packs.mkdir(parents=True, exist_ok=True)
