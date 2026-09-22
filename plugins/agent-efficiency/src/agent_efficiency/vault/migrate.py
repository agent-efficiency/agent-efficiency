"""Two-step migration from the existing memory and handoff stores.

``propose`` reads the old stores and writes a suggested entry for every source
file. A person reviews that file. ``apply_proposal`` consumes only the reviewed
file, so no note reaches a tree on a machine's guess. The suggested
classification is a keyword hint and nothing in ``apply_proposal`` trusts it: a
missing or unknown classification is an error, never a default.

Five rules hold the writing path together.

* Every note is rendered, loaded back through ``load_note``, and compared field
  by field before anything reaches disk. A note that would not load, or that
  would load as something other than what the entry asked for, is refused with
  its source named. Migration writes about a hundred notes at once and one
  malformed note stops the whole tree from indexing.
* Every entry is validated before the first write, and the complete tree the
  migration would produce is built and indexed in memory first. A conflict with
  a note already in the tree is found before any file is created, not halfway
  through.
* Every destination is resolved and confirmed to be inside its classified tree.
  A symlinked component in a destination path is refused, so a migration cannot
  be steered out of the tree it was told to write into.
* Nothing already on disk is replaced unless the caller asks for it. Re-running
  a migration over a tree a person has since edited would otherwise erase that
  work without a word.
* Every file is written through a temporary beside its destination and then
  linked or renamed into place, so a destination is never a half-written file.
  A path is recorded for cleanup before its write begins, so a failure removes
  the file that failed as well as the files before it. After a failed apply the
  tree is exactly what it was.

Oversized sources are imported whole into ``archive/`` under a head stub,
because only a person or an agent can say what the current state is. The stub
carries the archive path written relative to the head note, so the pointer is a
link that resolves rather than a string that reads like one.

An archive is a preservation artifact, so it is written byte for byte. The head
note's body is normalized, which is right for a note that has to parse back
identically, and wrong for an archive: normalization rewrites line endings and
drops boundary blank lines, and an archive that differs from its source is not
a copy of it. So an entry carries the source text unchanged in ``raw`` along
with the ``digest`` of the source bytes. The archive is written from ``raw``,
and the digest is checked against the bytes before the write and against the
file after it.

A proposal carries each source's full text twice, once normalized for the note
body and once unchanged for the archive. The largest real source is about 44 KB
and about 110 sources are expected, so the file stays in the low megabytes.
That cost buys the property the two-step design needs: the reviewed file is
complete on its own, and applying it does not re-read sources that may have
changed since the review.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agent_efficiency.vault.frontmatter import FrontmatterError
from agent_efficiency.vault.index import (
    IndexError_,
    build_from_notes,
    is_note_path,
    iter_note_paths,
    write_index,
)
from agent_efficiency.vault.root import MARKER, VaultRootError, find_tree
from agent_efficiency.vault.schema import (
    CAPS,
    CLASSIFICATIONS,
    SCHEMA_VERSION,
    STATUSES,
    TYPES,
    Note,
    load_note,
)
from agent_efficiency.vault.walk import WalkError, list_markdown

PROPOSAL_SCHEMA = 1

ARCHIVE_DIRECTORY = "archive"
ARCHIVE_NAME = "0001-imported.md"
STUB_LEAD = "Imported without summarizing. Current state has not been written yet."
STUB_POINTER = "Full import:"

MEMORY_INDEX = "MEMORY.md"
REMEMBER_INDEX = "remember.md"

TYPE_PREFIXES = {
    "project_": "project",
    "feedback_": "feedback",
    "reference_": "reference",
    "doctrine_": "doctrine",
    "session_": "session",
}

TYPE_DIRECTORIES = {
    "project": "projects",
    "feedback": "feedback",
    "reference": "reference",
    "doctrine": "doctrine",
    "session": "sessions",
}

# A hint for the reviewer, never an authority. Substring matching over the
# lowercased source, so it fires on a generic note that merely cites a work
# example. That is why a person edits the proposal before it is applied.
WORK_MARKERS = (
    "client",
    "customer",
    "employer",
    "company",
    "contract",
    "confidential",
    "proprietary",
    "internal",
)

ENTRY_REQUIRED = ("id", "title", "type", "classification", "hook", "body")
ENTRY_OPTIONAL = ("source", "status", "updated", "raw", "digest")
ENTRY_KEYS = frozenset(ENTRY_REQUIRED + ENTRY_OPTIONAL)
SCALAR_FIELDS = ("title", "hook")

ID_ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
ID_SEPARATOR = "-"

# Every character str.splitlines treats as a line break. A body may hold "\n"
# and nothing else from this set, or the note would parse back as different
# text than it was written from.
LINE_BREAKS = "\n\r\v\f\x1c\x1d\x1e\x85  "
BODY_BANNED = LINE_BREAKS.replace("\n", "")
SCALAR_BANNED = frozenset(LINE_BREAKS + "[]{}")


class MigrationError(ValueError):
    """Raised when a source, a proposal, or a destination cannot be migrated.

    Every failure in this module surfaces as this type. Read errors, decoding
    errors, encoding errors, parser errors, traversal errors, and index errors
    are all wrapped, so a caller has one thing to catch.
    """


@dataclass(frozen=True)
class _Write:
    """One file the migration will create, with everything needed to check it."""

    path: Path
    real: Path
    root: Path
    relative: str
    data: bytes
    source: str
    digest: str | None = None
    note_text: str | None = None


@dataclass
class _Placed:
    """A destination the writer has started on, and how to put it back."""

    path: Path
    previous: bytes | None


def sanitize_id(value: str) -> str:
    """Reduce a source name to a note id, deterministically.

    Lowercase ASCII letters, digits, and single hyphens survive. Everything
    else becomes a separator, runs of separators collapse, and the edges are
    trimmed. The result is a legal note id, a legal file stem, and stable under
    a second application.

    Different names can reduce to one id. That is a collision, not a result:
    the callers detect it and report both sources rather than guessing.
    """

    folded = []
    for character in value.strip().lower():
        folded.append(character if character in ID_ALLOWED else ID_SEPARATOR)

    cleaned = "".join(folded)
    while ID_SEPARATOR * 2 in cleaned:
        cleaned = cleaned.replace(ID_SEPARATOR * 2, ID_SEPARATOR)
    cleaned = cleaned.strip(ID_SEPARATOR)

    if not cleaned:
        raise MigrationError(
            f"{value!r} holds no character that can form a note id"
        )
    return cleaned


def propose(*, memory_dir: Path | None, remember_dir: Path | None) -> dict:
    """Read the old stores and return a proposal for review.

    Only the top level of each directory is read, and each store's own index
    file is skipped. Entries come out in a stable order, so two runs over an
    unchanged store produce the same file. A directory that cannot be listed is
    an error naming it, never an empty result.
    """

    if memory_dir is None and remember_dir is None:
        raise MigrationError(
            "propose needs at least one of memory_dir and remember_dir"
        )

    entries: list[dict] = []
    owners: dict[str, Path] = {}
    for directory, index_name in (
        (memory_dir, MEMORY_INDEX),
        (remember_dir, REMEMBER_INDEX),
    ):
        if directory is None:
            continue
        for path in _sources(directory, index_name):
            entry = _propose_entry(path)
            first = owners.get(entry["id"])
            if first is not None:
                raise MigrationError(
                    f"{path} and {first} both reduce to the note id "
                    f"{entry['id']!r}; rename one source before proposing"
                )
            owners[entry["id"]] = path
            entries.append(entry)

    return {"schema": PROPOSAL_SCHEMA, "entries": entries}


def apply_proposal(
    proposal: object, trees: dict[str, Path], *, overwrite: bool = False
) -> list[Path]:
    """Write a reviewed proposal into the given trees and return what was written.

    Every entry is validated and rendered first, every destination is resolved
    and confined to its tree, and the complete tree the migration would produce
    is indexed in memory. Only when all of that holds does anything reach disk.
    A write that fails part way puts the tree back exactly as it was, whether
    or not ``overwrite`` was given.
    """

    entries = _entries(proposal)
    roots = _resolve_trees(trees)

    planned: list[_Write] = []
    claimed: dict[Path, str] = {}
    identities: dict[tuple[Path, str], str] = {}
    touched: list[Path] = []

    for position, given in enumerate(entries, start=1):
        entry = _validate_entry(given, roots, position)
        root = roots[entry["classification"]]
        identity = (root, entry["id"])
        first = identities.get(identity)
        if first is not None:
            raise MigrationError(
                f"{entry['source']} and {first} both claim the note id "
                f"{entry['id']!r} in {root}; give one of them a different id"
            )
        identities[identity] = entry["source"]
        if root not in touched:
            touched.append(root)

        for write in _plan_entry(entry, root):
            held = claimed.get(write.real)
            if held is not None:
                raise MigrationError(
                    f"{entry['source']} and {held} both write {write.path}"
                )
            claimed[write.real] = entry["source"]
            if write.path.exists() and not overwrite:
                raise MigrationError(
                    f"{write.path} already exists. Migration does not replace a "
                    "note that is already in the tree. Move it aside, or re-run "
                    "with overwrite."
                )
            planned.append(write)

    _check_prospective_trees(roots, planned)
    _install(planned, overwrite)

    for root in sorted(touched):
        try:
            write_index(root)
        except IndexError_ as exc:
            raise MigrationError(
                f"notes written, but {root} will not index: {exc}"
            ) from None

    return [write.path for write in planned]


def _sources(directory: Path, index_name: str) -> list[Path]:
    location = Path(directory).expanduser()
    if not location.is_dir():
        raise MigrationError(f"{location} is not a directory")
    try:
        found = list_markdown(location)
    except WalkError as exc:
        raise MigrationError(str(exc)) from None
    return [path for path in found if path.name != index_name]


def _propose_entry(path: Path) -> dict:
    data = _read_bytes(path)
    text = _decode(path, data)
    stem = path.stem
    note_type = "project"
    name = stem
    for prefix, mapped in TYPE_PREFIXES.items():
        if stem.startswith(prefix):
            note_type = mapped
            name = stem[len(prefix) :]
            break

    try:
        identifier = sanitize_id(name)
    except MigrationError as exc:
        raise MigrationError(f"{path}: {exc}") from None

    lowered = text.lower()
    classification = "work" if any(m in lowered for m in WORK_MARKERS) else "private"

    return {
        "id": identifier,
        "title": identifier.replace(ID_SEPARATOR, " "),
        "type": note_type,
        "classification": classification,
        "status": "active",
        "updated": _modified(path),
        "hook": f"migrated from {path.name}",
        "source": str(path),
        "body": _trim_blank_lines(_normalize_breaks(text)),
        "raw": text,
        "digest": hashlib.sha256(data).hexdigest(),
    }


def _read_bytes(path: Path) -> bytes:
    """Read the source as bytes.

    Text mode would translate line endings on the way in, which is the one
    thing an archive must not do.
    """

    try:
        return path.read_bytes()
    except OSError as exc:
        raise MigrationError(f"{path}: could not be read: {exc}") from None


def _decode(path: Path, data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MigrationError(f"{path}: is not valid UTF-8: {exc}") from None


def _modified(path: Path) -> str:
    try:
        stamp = path.stat().st_mtime
    except OSError as exc:
        raise MigrationError(f"{path}: could not be read: {exc}") from None
    return datetime.fromtimestamp(stamp, tz=UTC).strftime("%Y-%m-%d")


def _today() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%d")


def _normalize_breaks(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _trim_blank_lines(text: str) -> str:
    """Drop leading and trailing blank lines, keeping every other character.

    Indentation on the first retained line survives, so an imported code block
    is still a code block.
    """

    lines = text.split("\n")
    start = 0
    stop = len(lines)
    while start < stop and not lines[start].strip():
        start += 1
    while stop > start and not lines[stop - 1].strip():
        stop -= 1
    return "\n".join(lines[start:stop])


def _entries(proposal: object) -> list:
    if not isinstance(proposal, dict):
        raise MigrationError("a proposal must be a JSON object")
    schema = proposal.get("schema")
    if schema != PROPOSAL_SCHEMA:
        raise MigrationError(f"unsupported proposal schema {schema!r}")
    entries = proposal.get("entries")
    if not isinstance(entries, list):
        raise MigrationError("a proposal must carry an 'entries' list")
    return entries


def _resolve_trees(trees: dict[str, Path]) -> dict[str, Path]:
    if not trees:
        raise MigrationError("no vault tree was given to write into")

    resolved: dict[str, Path] = {}
    for classification, location in trees.items():
        if classification not in CLASSIFICATIONS:
            raise MigrationError(
                f"unknown classification {classification!r}; allowed are "
                f"{', '.join(CLASSIFICATIONS)}"
            )
        root = Path(location).expanduser()
        if not (root / MARKER).is_file():
            raise MigrationError(f"{root} is not a vault tree; it has no {MARKER}")
        try:
            tree = find_tree(root)
        except VaultRootError as exc:
            raise MigrationError(str(exc)) from None
        if tree.classification != classification:
            raise MigrationError(
                f"{root} is a {tree.classification!r} tree, so it cannot take "
                f"{classification!r} notes"
            )
        resolved[classification] = tree.root
    return resolved


def _validate_entry(given: object, roots: dict[str, Path], position: int) -> dict:
    where = f"entry {position}"
    if not isinstance(given, dict):
        raise MigrationError(f"{where} is not an object")

    source = given.get("source", "")
    if not isinstance(source, str):
        raise MigrationError(f"{where}: 'source' must be a string")
    where = source or where

    unknown = sorted(key for key in given if key not in ENTRY_KEYS)
    if unknown:
        raise MigrationError(
            f"{where}: unknown field {unknown[0]!r}; allowed fields are "
            f"{', '.join(sorted(ENTRY_KEYS))}"
        )
    for key in ENTRY_REQUIRED:
        if key not in given:
            raise MigrationError(f"{where}: missing required field {key!r}")
    for key in ENTRY_KEYS:
        if key in given and not isinstance(given[key], str):
            raise MigrationError(f"{where}: field {key!r} must be a string")

    entry = {key: given[key] for key in ENTRY_REQUIRED}
    entry["source"] = where

    if entry["classification"] not in CLASSIFICATIONS:
        raise MigrationError(
            f"{where}: classification {entry['classification']!r} is not one of "
            f"{', '.join(CLASSIFICATIONS)}"
        )
    if entry["classification"] not in roots:
        raise MigrationError(
            f"{where}: no tree was given for classification "
            f"{entry['classification']!r}"
        )
    if entry["type"] not in TYPES:
        raise MigrationError(
            f"{where}: type {entry['type']!r} is not one of {', '.join(TYPES)}"
        )

    entry["status"] = given.get("status", "active")
    if entry["status"] not in STATUSES:
        raise MigrationError(
            f"{where}: status {entry['status']!r} is not one of "
            f"{', '.join(STATUSES)}"
        )

    entry["updated"] = given.get("updated", "") or _today()
    _check_date(entry["updated"], where)

    entry["raw"] = given.get("raw", "")
    entry["digest"] = given.get("digest", "")

    suggestion = _suggest(entry["id"])
    if not suggestion or suggestion != entry["id"]:
        raise MigrationError(
            f"{where}: id {entry['id']!r} is not in note id form. Use "
            f"{suggestion!r} or pick another id."
        )

    for key in SCALAR_FIELDS:
        _check_scalar(entry[key], key, where)
    _check_body(entry["body"], where)
    for key in ("body", "raw", "title", "hook", "id"):
        _check_encodable(entry[key], key, where)
    return entry


def _suggest(value: str) -> str:
    try:
        return sanitize_id(value)
    except MigrationError:
        return ""


def _check_date(value: str, where: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        raise MigrationError(
            f"{where}: updated {value!r} is not a YYYY-MM-DD date"
        ) from None


def _check_scalar(value: str, key: str, where: str) -> None:
    if not value.strip():
        raise MigrationError(f"{where}: field {key!r} is empty")
    if value != value.strip():
        raise MigrationError(
            f"{where}: field {key!r} has leading or trailing whitespace"
        )
    for character in value:
        if character in SCALAR_BANNED or _is_control(character):
            raise MigrationError(
                f"{where}: field {key!r} holds {character!r}, which the note "
                "grammar cannot carry"
            )


def _check_body(body: str, where: str) -> None:
    for character in body:
        if character in BODY_BANNED:
            line = body.count("\n", 0, body.index(character)) + 1
            raise MigrationError(
                f"{where}: line {line} holds the line break {character!r}, which "
                "would change the note when it is read back. Replace it with a "
                "newline."
            )


def _check_encodable(value: str, key: str, where: str) -> None:
    """Refuse text that cannot be written as UTF-8, before anything is created.

    A JSON proposal can carry an escaped lone surrogate. It survives parsing
    and every grammar check, and then fails at the moment of writing, which is
    the one moment when a failure costs a half-written tree.
    """

    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MigrationError(
            f"{where}: field {key!r} holds a character that cannot be written "
            f"as UTF-8: {exc}"
        ) from None


def _is_control(character: str) -> bool:
    return ord(character) < 0x20 or ord(character) == 0x7F


def _plan_entry(entry: dict, root: Path) -> list[_Write]:
    """Return the writes one entry needs, head note last."""

    directory = root / TYPE_DIRECTORIES[entry["type"]]
    head_path = directory / f"{entry['id']}.md"
    body = _trim_blank_lines(entry["body"])

    text = _render(entry, body)
    note = _verify(text, entry, body)
    if note.within_cap:
        return [_note_write(entry, root, head_path, text)]

    archive_path = directory / entry["id"] / ARCHIVE_DIRECTORY / ARCHIVE_NAME
    pointer = archive_path.relative_to(head_path.parent).as_posix()
    stub = f"{STUB_LEAD}\n\n{STUB_POINTER} {pointer}"
    text = _render(entry, stub)
    note = _verify(text, entry, stub)
    if not note.within_cap:
        raise MigrationError(
            f"{entry['source']}: the head stub is {note.size} characters, over "
            f"the {CAPS[entry['type']]} character cap for a {entry['type']} note. "
            "Shorten the title or the hook."
        )
    return [
        _archive_write(entry, root, archive_path, body),
        _note_write(entry, root, head_path, text),
    ]


def _note_write(entry: dict, root: Path, path: Path, text: str) -> _Write:
    return _Write(
        path=path,
        real=_destination(root, path, entry["source"]),
        root=root,
        relative=path.relative_to(root).as_posix(),
        data=text.encode("utf-8"),
        source=entry["source"],
        note_text=text,
    )


def _archive_write(entry: dict, root: Path, path: Path, body: str) -> _Write:
    """Plan the archive copy.

    The source text is used exactly as it was read. Only an entry with no
    ``raw`` field, which means an entry written by hand rather than proposed,
    falls back to the normalized body.
    """

    if entry["raw"]:
        data = entry["raw"].encode("utf-8")
    else:
        data = (body + "\n").encode("utf-8")

    digest = entry["digest"] or None
    if digest is not None and hashlib.sha256(data).hexdigest() != digest:
        raise MigrationError(
            f"{entry['source']}: the 'raw' text does not match the 'digest' "
            "this entry carries, so the archive is not the source it claims to "
            "be. Restore the text, or drop the digest if the edit was intended."
        )
    return _Write(
        path=path,
        real=_destination(root, path, entry["source"]),
        root=root,
        relative=path.relative_to(root).as_posix(),
        data=data,
        source=entry["source"],
        digest=digest,
    )


def _destination(root: Path, path: Path, source: str) -> Path:
    """Resolve a destination and confirm it stays inside its tree.

    A symlinked component is refused rather than followed. A destination that
    resolves outside the tree is refused by name. Together these keep a
    migration from writing a private note into a work tree, or anywhere else on
    the machine, through a link somebody left in the way.
    """

    try:
        real_root = root.resolve(strict=True)
    except OSError as exc:
        raise MigrationError(f"{source}: {root} cannot be resolved: {exc}") from None

    current = real_root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise MigrationError(
                f"{source}: {current} is a symlink. A note is written to a real "
                "path inside its tree, never through a link."
            )

    resolved = Path(os.path.realpath(path))
    if resolved != real_root and real_root not in resolved.parents:
        raise MigrationError(
            f"{source}: {path} resolves to {resolved}, which is outside the "
            f"tree at {real_root}"
        )
    return resolved


def _render(entry: dict, body: str) -> str:
    header = "\n".join(
        (
            "---",
            f"schema: {SCHEMA_VERSION}",
            f"id: {entry['id']}",
            f"title: {entry['title']}",
            f"type: {entry['type']}",
            f"classification: {entry['classification']}",
            f"status: {entry['status']}",
            f"updated: {entry['updated']}",
            f"hook: {entry['hook']}",
            "---",
        )
    )
    return f"{header}\n{body}\n"


def _verify(text: str, entry: dict, body: str) -> Note:
    """Load the rendered note back and confirm it says what the entry said."""

    try:
        note = load_note(text)
    except FrontmatterError as exc:
        raise MigrationError(
            f"{entry['source']}: the note this entry renders will not load: {exc}"
        ) from None

    fields = ("id", "title", "type", "classification", "status", "updated", "hook")
    for field in fields:
        if getattr(note, field) != entry[field]:
            raise MigrationError(
                f"{entry['source']}: field {field!r} reads back as "
                f"{getattr(note, field)!r} rather than {entry[field]!r}"
            )
    if note.body != body:
        raise MigrationError(
            f"{entry['source']}: the body does not survive a round trip"
        )
    return note


def _check_prospective_trees(
    roots: dict[str, Path], planned: list[_Write]
) -> None:
    """Index the complete tree each write would produce, before writing any of it.

    Validating entries against each other is not enough. A note already in the
    tree can hold the id an entry claims, under a different file name, and that
    only shows up when the index is built. Finding it here costs nothing.
    Finding it afterwards leaves the conflicting note on disk.
    """

    for root in sorted(set(roots.values())):
        classification = next(
            name for name, location in roots.items() if location == root
        )
        notes: dict[str, str] = {}
        try:
            for path in iter_note_paths(root):
                notes[path.relative_to(root).as_posix()] = _read_note(path)
        except IndexError_ as exc:
            raise MigrationError(str(exc)) from None

        for write in planned:
            if write.root != root or write.note_text is None:
                continue
            if not is_note_path(write.relative):
                continue
            notes[write.relative] = write.note_text

        try:
            build_from_notes(classification, sorted(notes.items()))
        except IndexError_ as exc:
            raise MigrationError(
                f"{root} would not index after this migration: {exc}"
            ) from None


def _read_note(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise IndexError_(f"{path}: is not valid UTF-8: {exc}") from None
    except OSError as exc:
        raise IndexError_(f"{path}: could not be read: {exc}") from None


def _install(planned: list[_Write], overwrite: bool) -> None:
    """Create every planned file, or leave the tree exactly as it was.

    Each destination is recorded before its write starts, not after it
    succeeds, so the file a failure interrupts is cleaned up along with the
    files that came before it.
    """

    placed: list[_Placed] = []
    made: list[Path] = []
    current: _Write | None = None
    try:
        for write in planned:
            current = write
            made.extend(_make_directories(write.path.parent, write.source))
            placed.append(_Placed(path=write.path, previous=_previous(write.path)))
            _place(write, overwrite)
            _confirm(write)
    except OSError as exc:
        _undo(placed, made)
        where = current.path if current is not None else "the tree"
        raise MigrationError(f"{where}: could not be written: {exc}") from None
    except BaseException:
        _undo(placed, made)
        raise


def _make_directories(directory: Path, source: str) -> list[Path]:
    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent

    made: list[Path] = []
    try:
        for target in reversed(missing):
            target.mkdir()
            made.append(target)
    except OSError as exc:
        _remove_directories(made)
        raise MigrationError(
            f"{source}: {directory} could not be created: {exc}"
        ) from None
    return made


def _previous(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _place(write: _Write, overwrite: bool) -> None:
    """Put one file in place through a temporary beside it.

    Without ``overwrite`` the destination is created with ``os.link``, which
    fails if anything is already there. That is one filesystem operation, so a
    file that appears between the earlier existence check and this moment is
    refused rather than overwritten.
    """

    temporary = _stage(write)
    try:
        if overwrite:
            os.replace(temporary, write.path)
        else:
            try:
                os.link(temporary, write.path)
            except FileExistsError:
                raise MigrationError(
                    f"{write.source}: {write.path} appeared while the migration "
                    "was running, so it was not replaced"
                ) from None
            except OSError as exc:
                raise MigrationError(
                    f"{write.source}: {write.path} could not be written: {exc}"
                ) from None
    except OSError as exc:
        raise MigrationError(
            f"{write.source}: {write.path} could not be written: {exc}"
        ) from None
    finally:
        temporary.unlink(missing_ok=True)


def _stage(write: _Write) -> Path:
    """Write the bytes to a unique temporary in the destination directory.

    The descriptor ``mkstemp`` returns is written through and closed inside the
    cleanup scope, so a failure on close removes the temporary rather than
    leaving it behind.
    """

    try:
        handle, raw = tempfile.mkstemp(
            dir=write.path.parent, prefix=f"{write.path.name}.", suffix=".tmp"
        )
    except OSError as exc:
        raise MigrationError(
            f"{write.source}: {write.path} could not be written: {exc}"
        ) from None

    temporary = Path(raw)
    try:
        stream = os.fdopen(handle, "wb")
    except BaseException:
        os.close(handle)
        temporary.unlink(missing_ok=True)
        raise
    try:
        with stream:
            stream.write(write.data)
            stream.flush()
            os.fsync(stream.fileno())
        # mkstemp creates the file 0600. A note is an ordinary tracked file,
        # so give it the mode a plain write would have produced.
        os.chmod(temporary, 0o666 & ~_umask())
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise MigrationError(
            f"{write.source}: {write.path} could not be written: {exc}"
        ) from None
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _confirm(write: _Write) -> None:
    """Read the file back and confirm it is the bytes that were planned.

    An archive carries the digest of its source, so this is the end of the
    chain that starts at the source file: read, carried, written, and read
    again without a byte changing.
    """

    try:
        written = write.path.read_bytes()
    except OSError as exc:
        raise MigrationError(
            f"{write.source}: {write.path} could not be read back: {exc}"
        ) from None

    if written != write.data:
        raise MigrationError(
            f"{write.source}: {write.path} was written as {len(written)} bytes "
            f"rather than {len(write.data)}"
        )
    if write.digest is not None:
        actual = hashlib.sha256(written).hexdigest()
        if actual != write.digest:
            raise MigrationError(
                f"{write.source}: {write.path} does not match the digest of its "
                f"source; expected {write.digest}, wrote {actual}"
            )


def _undo(placed: list[_Placed], made: list[Path]) -> None:
    for record in reversed(placed):
        with contextlib.suppress(OSError):
            if record.previous is None:
                record.path.unlink(missing_ok=True)
            else:
                _restore(record.path, record.previous)
    _remove_directories(made)


def _restore(path: Path, previous: bytes) -> None:
    handle, raw = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.")
    temporary = Path(raw)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(previous)
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _remove_directories(made: list[Path]) -> None:
    for directory in reversed(made):
        with contextlib.suppress(OSError):
            directory.rmdir()


def _umask() -> int:
    mask = os.umask(0o022)
    os.umask(mask)
    return mask
