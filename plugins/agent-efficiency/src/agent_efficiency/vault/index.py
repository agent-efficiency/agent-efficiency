"""Deterministic vault index generation.

Two artifacts come out of one scan. ``index.json`` is what the runtime reads,
so the hook path never parses markdown. ``INDEX.md`` is for a person. Neither
carries a timestamp, so regenerating without a note change rewrites the same
bytes and produces no git diff.

The two artifacts are one generation. Each is prepared in full, then both are
put in place, and a failure on the second restores the first. A reader that
opens one and then the other never sees a pair that disagrees.

Which files are notes is decided by ``is_note_path`` on a path relative to the
tree root, not by reading the filesystem. The guards and the git hooks apply
the same rule to content they were handed, so one definition of "note" serves
the disk, the git index, and a push.

Every failure this module reports is an ``IndexError_``. A note that cannot be
read, a directory that cannot be listed, a directory named like a note, a
broken symlink, bytes that are not UTF-8, a tree with no marker: each is named
by path and raised as this module's own error, never as a bare ``OSError`` or
``UnicodeDecodeError``.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from agent_efficiency.vault.frontmatter import FrontmatterError
from agent_efficiency.vault.root import VaultRootError, VaultTree, find_tree
from agent_efficiency.vault.schema import Note, load_note
from agent_efficiency.vault.walk import WalkError, walk_markdown

INDEX_SCHEMA = 1
INDEX_MARKDOWN = "INDEX.md"
INDEX_JSON = "index.json"
ARCHIVE = "archive"

# A sanity bound on each index artifact, not a token budget. Only a bounded
# rendered subset of the index ever enters a session, so the artifact's size on
# disk costs parse time and nothing else. At roughly 450 characters per entry
# this holds about 580 notes, which leaves room to grow well past a realistic
# vault while still catching a tree that has run away.
#
# Nothing is ever dropped to fit: an index that does not fit is reported at its
# measured size and written whole, because a truncated index is a silent lie
# about what the tree holds.
INDEX_CAP = 262144

ARTIFACTS = (INDEX_JSON, INDEX_MARKDOWN)

TYPE_ORDER = ("feedback", "doctrine", "project", "reference", "session")
_TYPE_RANK = {name: rank for rank, name in enumerate(TYPE_ORDER)}


class IndexError_(ValueError):
    """Raised when a tree cannot produce a consistent index.

    The trailing underscore keeps the builtin ``IndexError`` reachable.
    """


def is_note_path(relative: str | PurePosixPath) -> bool:
    """Whether a path relative to the tree root is a candidate note.

    Three exclusions, each deliberate and each tested:

    * Anything below a directory named ``archive``. Archived detail is kept for
      people to read on demand and never enters the index.
    * Any file named exactly ``INDEX.md``, at any depth. The generated index
      never indexes itself. The match is exact, so a real note named
      ``index.md`` is still indexed rather than silently dropped.
    * Anything whose name starts with a dot, including every path below a dot
      directory such as ``.git`` or ``.githooks``. Those hold tooling.

    A path that survives the exclusions is a candidate even when it is not a
    readable note. The caller then reports it by name. Nothing that looks like
    a note is dropped without a message.
    """

    parts = PurePosixPath(relative).parts
    if not parts or not parts[-1].endswith(".md"):
        return False
    if ARCHIVE in parts[:-1]:
        return False
    if parts[-1] == INDEX_MARKDOWN:
        return False
    return not any(part.startswith(".") for part in parts)


def is_index_artifact(relative: str | PurePosixPath) -> bool:
    """Whether a path relative to the tree root is a generated index artifact.

    Only at the root. A file named ``INDEX.md`` deeper in the tree is somebody
    else's file, and it is scanned like any other.
    """

    parts = PurePosixPath(relative).parts
    return len(parts) == 1 and parts[0] in ARTIFACTS


def iter_note_paths(root: Path) -> list[Path]:
    """Return every candidate note path under ``root``, sorted.

    A directory that cannot be listed is an error naming that directory. It is
    never reported as an absence of notes.
    """

    try:
        found = walk_markdown(root)
    except WalkError as exc:
        raise IndexError_(str(exc)) from None
    return [
        path for path in found if is_note_path(path.relative_to(root).as_posix())
    ]


def build_index(root: Path) -> dict:
    """Build the index for the vault tree at or above ``root``."""

    return _build(_tree(root))


def build_from_notes(
    classification: str, notes: Iterable[tuple[str, str]]
) -> dict:
    """Build an index payload from note text that is already in hand.

    ``notes`` carries ``(path relative to the tree root, note text)`` pairs.
    This is the whole of the index rule, so a prospective tree, a git index,
    and the files on disk are all judged the same way.
    """

    seen: dict[str, str] = {}
    ranked: list[tuple[int, str, dict]] = []

    for relative, text in notes:
        note = _parse(relative, text)
        if note.type not in _TYPE_RANK:
            raise IndexError_(
                f"{relative}: note type {note.type!r} has no index order"
            )
        if note.classification != classification:
            raise IndexError_(
                f"{relative}: classification {note.classification!r} does not "
                f"match tree classification {classification!r}"
            )
        if note.id in seen:
            raise IndexError_(
                f"duplicate note id {note.id!r} in {relative} and {seen[note.id]}"
            )
        seen[note.id] = relative
        ranked.append((_TYPE_RANK[note.type], note.id, _entry(note, relative)))

    ranked.sort(key=lambda item: (item[0], item[1]))
    return {
        "schema": INDEX_SCHEMA,
        "classification": classification,
        "notes": [entry for _, _, entry in ranked],
    }


def render_markdown(index: dict) -> str:
    """Render the human index. Entry lines separate title from hook with a colon."""

    lines = [f"# Vault index ({index['classification']})", ""]
    if index["notes"]:
        for entry in index["notes"]:
            link = quote(entry["path"])
            lines.append(f"- [{entry['title']}]({link}): {entry['hook']}")
    else:
        lines.append("No notes yet.")
    lines.append("")
    return "\n".join(lines)


def render_payload(index: dict) -> str:
    """Render the machine index exactly as it is written."""

    return json.dumps(index, indent=2, ensure_ascii=False) + "\n"


def cap_overflow(index: dict) -> list[str]:
    """Report each index artifact that is over the on-disk character cap.

    A message names the artifact, its measured size, and the cap. Nothing is
    dropped to make it fit.
    """

    reports = []
    for name, text in (
        (INDEX_JSON, render_payload(index)),
        (INDEX_MARKDOWN, render_markdown(index)),
    ):
        if len(text) > INDEX_CAP:
            reports.append(
                f"{name} is {len(text)} characters, over the {INDEX_CAP} "
                f"character cap for an index artifact"
            )
    return reports


def preflight(root: Path) -> list[str]:
    """Report every reason ``write_index`` would fail before it is called.

    The guards run this too, so a tree cannot pass ``vault check`` and then
    fail ``vault index``. Two things are checked: that each artifact target is
    an ordinary file the tool may replace, and that the tree root can be
    written to.
    """

    problems: list[str] = []
    if not os.access(root, os.W_OK | os.X_OK):
        problems.append(f"{root} cannot be written to, so no index can be placed")
    for name in ARTIFACTS:
        target = root / name
        if target.is_symlink():
            problems.append(
                f"{target} is a symlink. An index artifact is written in place, "
                "so it has to be an ordinary file."
            )
        elif target.exists() and not target.is_file():
            problems.append(
                f"{target} is not an ordinary file, so the index cannot be "
                "written there"
            )
    return problems


def write_index(root: Path) -> dict:
    """Regenerate both index artifacts for the tree at or above ``root``.

    Both payloads are built before either file is touched, so a note that fails
    to parse leaves the previous index in place. Both are then put in place as
    one generation.
    """

    tree = _tree(root)
    problems = preflight(tree.root)
    if problems:
        raise IndexError_(problems[0])
    index = _build(tree)
    _write_pair(tree.root, render_payload(index), render_markdown(index))
    return index


def _tree(root: Path) -> VaultTree:
    try:
        return find_tree(root)
    except VaultRootError as exc:
        raise IndexError_(str(exc)) from None


def _build(tree: VaultTree) -> dict:
    root = tree.root
    notes = [
        (path.relative_to(root).as_posix(), _read(path))
        for path in iter_note_paths(root)
    ]
    return build_from_notes(tree.classification, notes)


def _entry(note: Note, relative: str) -> dict:
    return {
        "id": note.id,
        "title": note.title,
        "type": note.type,
        "status": note.status,
        "updated": note.updated,
        "hook": note.hook,
        "path": relative,
        "size": note.size,
        "within_cap": note.within_cap,
        "repos": list(note.repos),
        "paths": list(note.paths),
        "branches": list(note.branches),
    }


def _parse(relative: str, text: str) -> Note:
    try:
        return load_note(text)
    except FrontmatterError as exc:
        raise IndexError_(f"{relative}: {exc}") from None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise IndexError_(f"{path}: is not valid UTF-8: {exc}") from None
    except OSError as exc:
        raise IndexError_(f"{path}: could not be read: {exc}") from None


def _write_pair(root: Path, payload: str, markdown: str) -> None:
    """Install both artifacts, or leave the previous generation in place.

    Each rename is atomic on its own, but the pair is not. So the machine
    artifact is replaced first with its previous bytes held, and if the human
    artifact cannot be replaced the machine artifact is put back. The tree is
    then one generation old rather than half a generation new.
    """

    machine = root / INDEX_JSON
    human = root / INDEX_MARKDOWN
    previous = _previous(machine)

    staged = []
    try:
        staged.append(_stage(machine, payload))
        staged.append(_stage(human, markdown))
    except BaseException:
        for temporary in staged:
            temporary.unlink(missing_ok=True)
        raise

    try:
        os.replace(staged[0], machine)
    except OSError as exc:
        for temporary in staged:
            temporary.unlink(missing_ok=True)
        raise IndexError_(f"{machine}: could not be written: {exc}") from None

    try:
        os.replace(staged[1], human)
    except OSError as exc:
        staged[1].unlink(missing_ok=True)
        _restore(machine, previous)
        raise IndexError_(f"{human}: could not be written: {exc}") from None


def _previous(target: Path) -> bytes | None:
    try:
        return target.read_bytes()
    except OSError:
        return None


def _restore(target: Path, previous: bytes | None) -> None:
    try:
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            temporary = _stage_bytes(target, previous)
            os.replace(temporary, target)
    except OSError:
        # The restore is a best effort. The failure that brought us here is
        # the one the caller is told about.
        pass


def _stage(target: Path, content: str) -> Path:
    try:
        return _stage_bytes(target, content.encode("utf-8"))
    except OSError as exc:
        raise IndexError_(f"{target}: could not be written: {exc}") from None
    except UnicodeEncodeError as exc:
        raise IndexError_(f"{target}: is not UTF-8 encodable: {exc}") from None


def _stage_bytes(target: Path, data: bytes) -> Path:
    """Write ``data`` to a unique temporary beside ``target`` and return it.

    The descriptor ``mkstemp`` returns is written through and closed inside the
    cleanup scope, so a failure on close removes the temporary rather than
    leaving it for the next scan to trip over.
    """

    handle, raw = tempfile.mkstemp(
        dir=target.parent, prefix=f"{target.name}.", suffix=".tmp"
    )
    temporary = Path(raw)
    try:
        stream = os.fdopen(handle, "wb")
    except BaseException:
        os.close(handle)
        temporary.unlink(missing_ok=True)
        raise
    try:
        with stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # mkstemp creates the file 0600. Index artifacts are ordinary tracked
        # files, so give them the mode a plain write would have produced.
        os.chmod(temporary, 0o666 & ~_umask())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _umask() -> int:
    mask = os.umask(0o022)
    os.umask(mask)
    return mask
