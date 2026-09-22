"""Directory traversal that reports what it could not read.

``Path.rglob`` and ``Path.glob`` swallow an enumeration error. A directory
whose read permission is missing comes back as no entries, which is the same
answer as a directory with no entries in it. For a tool that decides what is
safe to commit, those two answers must never look alike: one means clean, the
other means unknown.

Every function here raises ``WalkError`` naming the path it could not list.
Callers wrap that in their own module error, so a caller still has one type to
catch.
"""

from __future__ import annotations

import os
from pathlib import Path

MARKDOWN_SUFFIX = ".md"


class WalkError(OSError):
    """Raised when a directory cannot be enumerated."""


def walk_markdown(root: Path) -> list[Path]:
    """Return every ``*.md`` file under ``root``, sorted.

    Dot directories are pruned before they are read, so tooling directories
    such as ``.git`` are neither scanned nor reported. Symlinked directories
    are not followed. Any other directory that cannot be listed raises
    ``WalkError`` rather than contributing nothing.

    A directory whose own name ends in ``.md`` is returned as well, so the
    caller reports it by name instead of passing over it. A directory is not a
    note, and a tree that holds one is telling the reader something is wrong.
    """

    found: list[Path] = []
    for directory, names, files in os.walk(root, onerror=_raise):
        names[:] = [name for name in names if not name.startswith(".")]
        base = Path(directory)
        for name in names + files:
            if name.startswith(".") or not name.endswith(MARKDOWN_SUFFIX):
                continue
            found.append(base / name)
    return sorted(found)


def list_markdown(directory: Path) -> list[Path]:
    """Return the ``*.md`` files directly inside ``directory``, sorted."""

    found: list[Path] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.name.endswith(MARKDOWN_SUFFIX):
                    continue
                if entry.is_file():
                    found.append(Path(entry.path))
    except OSError as exc:
        raise _error(exc, directory) from None
    return sorted(found)


def _raise(error: OSError) -> None:
    raise _error(error, None) from None


def _error(error: OSError, fallback: Path | None) -> WalkError:
    location = error.filename or (str(fallback) if fallback is not None else "")
    reason = error.strerror or str(error)
    return WalkError(f"{location} could not be listed: {reason}")
