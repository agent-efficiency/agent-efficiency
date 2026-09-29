"""Checks shared by the git hooks and the CLI.

A finding names a rule, a location, and a remedy. It never carries matched
secret material and never carries note body text, so findings are safe to
print, to log, and to paste into a ticket. A parser message is the one piece of
outside text a finding repeats, and it is withheld if it matches a secret rule.

There are two ways in. ``check_tree`` reads a tree from disk. ``check_content``
takes ``(path, bytes)`` pairs and never touches the filesystem, which is how
the pre-commit and pre-push hooks check what git is about to carry rather than
what happens to be in the working copy at the time. Both run the same rules
over the same definition of a note, so the two cannot drift apart.

Both report every problem they find. They do not stop at the first bad note,
and a note that cannot be read becomes a finding rather than an exception. What
they refuse outright is a path that is not a vault tree, and a directory that
cannot be listed: the first is a mistake in the invocation, the second means
the answer is unknown. Neither is a state of the vault, so both raise
``GuardError``. An unreadable directory is never reported as an empty one.

Scope of the secret scan: every markdown file in the tree, archives included,
excluding dot directories and the two generated index artifacts at the tree
root. A file named ``INDEX.md`` deeper in the tree is scanned like any other,
because the exemption belongs to the artifact this tool writes and not to a
name anybody can reuse. Files that are not markdown are not scanned. The scan
is a pattern match, so it finds known shapes of credential and nothing else. A
clean result is not proof that a tree holds no secret.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agent_efficiency.vault.frontmatter import FrontmatterError
from agent_efficiency.vault.index import (
    INDEX_JSON,
    IndexError_,
    build_from_notes,
    cap_overflow,
    is_index_artifact,
    is_note_path,
    preflight,
    render_payload,
)
from agent_efficiency.vault.root import (
    VaultRootError,
    VaultTree,
    find_tree,
    normalize_remote,
)
from agent_efficiency.vault.schema import CAPS, Note, load_note
from agent_efficiency.vault.walk import WalkError, walk_markdown

SECRET_REMEDY = "remove the value and store it outside the vault"

SECRET_RULES = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "assigned_secret",
        re.compile(
            # A secret-sounding key assigned a long opaque value. A URL and a
            # filesystem path are excluded: both are locations, not values, and
            # notes are full of them.
            r"\b(?:api[_-]?key|secret|token|password|passwd)\b"
            r"\s*[:=]\s*"
            r"(?!https?://)(?![~./])"
            r"[^\s\"']{20,}",
            re.IGNORECASE,
        ),
    ),
)

_LINE_BREAK = re.compile(r"\r\n|\r|\n")

MARKDOWN_SUFFIX = ".md"


class GuardError(ValueError):
    """Raised when the tree itself cannot be checked."""


# Passed as ``index`` when the caller has no index to compare, so the check
# that the index is current is skipped rather than failed.
NOT_CHECKED = object()


@dataclass(frozen=True)
class Finding:
    rule: str
    location: str
    detail: str
    remedy: str


def scan_secrets(text: str, location: str = "") -> list[Finding]:
    """Report every line that matches a secret rule.

    Every match is reported, not only the first, so one secret cannot hide
    another. Repeat matches of the same rule on the same line collapse into one
    finding. The matched value never reaches the returned findings.
    """

    starts = _line_starts(text)
    findings: list[Finding] = []
    seen: set[tuple[str, int]] = set()
    for rule, pattern in SECRET_RULES:
        for match in pattern.finditer(text):
            line = bisect.bisect_right(starts, match.start())
            if (rule, line) in seen:
                continue
            seen.add((rule, line))
            findings.append(
                Finding(
                    rule=rule,
                    location=f"{location}:{line}" if location else f"line {line}",
                    detail=f"{rule} pattern matched",
                    remedy=SECRET_REMEDY,
                )
            )
    return findings


def check_tree(root: Path, remote_url: str | None = None) -> list[Finding]:
    """Check the vault tree at or above ``root`` and return every finding.

    ``remote_url`` is the destination of a push, supplied by the pre-push hook.
    Leave it ``None`` to skip the destination check.

    This reads the working copy. It answers "is what is on disk safe", which is
    not the same question as "is what git is about to carry safe". The hooks
    ask the second question through ``check_content``.
    """

    tree = _tree(root)
    findings = [
        Finding(
            rule="index",
            location=str(tree.root),
            detail=message,
            remedy="make the index artifacts ordinary writable files",
        )
        for message in preflight(tree.root)
    ]

    try:
        paths = walk_markdown(tree.root)
    except WalkError as exc:
        raise GuardError(str(exc)) from None

    items: list[tuple[str, bytes | None]] = []
    for path in paths:
        relative = path.relative_to(tree.root).as_posix()
        items.append((relative, _read(path)))

    index_path = tree.root / INDEX_JSON
    index: bytes | None | object
    if index_path.is_symlink() or (index_path.exists() and not index_path.is_file()):
        # The preflight above already reports an artifact that is not a file.
        index = NOT_CHECKED
    elif index_path.is_file():
        index = _read(index_path)
    else:
        index = None
    findings.extend(
        _check_items(
            tree,
            items,
            remote_url,
            index=index,
            index_remedy=f"run agent-efficiency vault index {tree.root}",
        )
    )
    return findings


def check_content(
    tree: VaultTree,
    items: Iterable[tuple[str, bytes]],
    remote_url: str | None = None,
    *,
    index: bytes | None | object = NOT_CHECKED,
    index_remedy: str = "",
) -> list[Finding]:
    """Check content that is already in hand rather than content on disk.

    ``items`` carries ``(path relative to the tree root, file bytes)`` pairs.
    The git hooks build that list from the git index or from the objects a push
    would send, so the checks run on what is being committed or pushed even
    when the working copy is clean.

    Every rule ``check_tree`` applies is applied here: classification, size
    cap, note schema, duplicate ids, index size, the secret scan, and the push
    destination. ``index`` is the ``index.json`` content that goes with the
    items, or None when there is none; it must match what ``vault index``
    would write. Leave it out to skip that comparison.
    """

    return _check_items(
        tree,
        [(path, data) for path, data in items],
        remote_url,
        index=index,
        index_remedy=index_remedy,
    )


def check_history_classification(
    tree: VaultTree, items: Iterable[tuple[str, bytes]]
) -> list[Finding]:
    """Classification over every blob a push would send, history included.

    A note committed under the wrong classification and corrected in a later
    commit still travels with the push, and its content reaches the remote.
    Correcting it does not unsend it.

    Only classification is checked here. A cap or duplicate id in an old commit
    is not a disclosure, so applying those rules to history would refuse pushes
    for no gain. Content that does not parse as a note is skipped: history
    predates this format, and unparseable does not mean misfiled.
    """

    findings: list[Finding] = []
    for location, data in items:
        # An outgoing object's location carries a " (object <oid>)" suffix so a
        # finding can name the object it came from. The path test needs the
        # path alone.
        path = location.split(" (object ", 1)[0]
        if not is_note_path(path):
            continue
        try:
            note = load_note(data.decode("utf-8"))
        except (UnicodeDecodeError, FrontmatterError):
            continue
        if note.classification != tree.classification:
            findings.append(
                Finding(
                    rule="classification_history",
                    location=location,
                    detail=(
                        f"a commit in this push carries a {note.classification!r} "
                        f"note in a {tree.classification!r} tree"
                    ),
                    remedy=(
                        "drop or rewrite the commit that carries it; correcting "
                        "the note later does not stop the content being pushed"
                    ),
                )
            )
    return findings


def _check_items(
    tree: VaultTree,
    items: list[tuple[str, bytes | None]],
    remote_url: str | None,
    *,
    index: bytes | None | object = NOT_CHECKED,
    index_remedy: str = "",
) -> list[Finding]:
    """The whole of the rule set. ``None`` bytes mean the file could not be read."""

    findings: list[Finding] = []
    findings.extend(_check_destination(tree, remote_url))

    notes: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    # A note that cannot be read cannot be indexed either, so the index is only
    # compared when every note was read.
    all_notes_read = True

    for relative, data in sorted(items, key=lambda item: item[0]):
        _check_relative(relative)
        if not is_scannable(relative):
            continue
        if data is None:
            findings.append(
                _unreadable(relative, "file could not be read as UTF-8 text")
            )
            all_notes_read = all_notes_read and not is_note_path(relative)
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(
                _unreadable(relative, "file is not UTF-8 text")
            )
            all_notes_read = all_notes_read and not is_note_path(relative)
            continue
        if is_note_path(relative):
            findings.extend(_check_note(text, relative, tree.classification, seen))
            notes.append((relative, text))
        findings.extend(scan_secrets(text, relative))

    findings.extend(
        _check_index(
            tree,
            notes,
            findings,
            index if all_notes_read else NOT_CHECKED,
            index_remedy,
        )
    )
    return findings


def _check_index(
    tree: VaultTree,
    notes: list[tuple[str, str]],
    found: list[Finding],
    current: bytes | None | object = NOT_CHECKED,
    remedy: str = "",
) -> list[Finding]:
    """Report anything that would stop this note set from indexing.

    A tree that passes the guards has to be a tree that indexes, or a commit
    passes and the next ``vault index`` fails. When ``current`` is given, it
    also has to equal the index that ``vault index`` would write, because the
    hooks read the index and never the notes.
    """

    try:
        index = build_from_notes(tree.classification, notes)
    except IndexError_ as exc:
        if found:
            # The same problem, already reported against the note it belongs
            # to. Saying it twice adds noise, not information.
            return []
        return [
            Finding(
                rule="index",
                location=str(tree.root),
                detail=str(exc),
                remedy="correct the note this names, then check again",
            )
        ]
    stale: list[Finding] = []
    if current is not NOT_CHECKED:
        expected = render_payload(index).encode("utf-8")
        if current != expected:
            stale.append(
                Finding(
                    rule="index_stale",
                    location=str(tree.root / INDEX_JSON),
                    detail=(
                        f"{INDEX_JSON} is missing"
                        if current is None
                        else f"{INDEX_JSON} does not match the notes in this tree"
                    ),
                    remedy=remedy or f"run agent-efficiency vault index {tree.root}",
                )
            )
    return stale + [
        Finding(
            rule="index_cap",
            location=str(tree.root),
            detail=message,
            remedy="move notes out of this tree, or archive detail out of them",
        )
        for message in cap_overflow(index)
    ]


def _tree(root: Path) -> VaultTree:
    try:
        return find_tree(root)
    except VaultRootError as exc:
        raise GuardError(str(exc)) from None


def _check_relative(relative: str) -> None:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise GuardError(
            f"{relative!r} is not a path inside the tree, so it cannot be checked"
        )


def is_scannable(relative: str) -> bool:
    """Whether a path relative to the tree root is checked at all.

    Markdown only, nothing under a dot directory, and neither generated index
    artifact at the tree root. The git hooks use this to decide which blobs to
    read, so they read the same files this module would.
    """

    parts = PurePosixPath(relative).parts
    if not parts or not parts[-1].endswith(MARKDOWN_SUFFIX):
        return False
    if any(part.startswith(".") for part in parts):
        return False
    return not is_index_artifact(relative)


def _unreadable(relative: str, detail: str) -> Finding:
    return Finding(
        rule="unreadable",
        location=relative,
        detail=detail,
        remedy="replace it with a UTF-8 markdown file or remove it",
    )


def _check_destination(tree: VaultTree, remote_url: str | None) -> list[Finding]:
    if remote_url is None:
        return []
    try:
        target = normalize_remote(remote_url)
    except VaultRootError as exc:
        raise GuardError(str(exc)) from None
    if not tree.remote_url:
        return [
            Finding(
                rule="destination",
                location=str(tree.root),
                detail=(
                    f"this tree registers no remote, so the push target {target} "
                    "cannot be confirmed"
                ),
                remedy="record the intended remote_url in .vault.json",
            )
        ]
    try:
        registered = tree.normalized_remote
    except VaultRootError as exc:
        raise GuardError(str(exc)) from None
    if target == registered:
        return []
    return [
        Finding(
            rule="destination",
            location=str(tree.root),
            detail=(
                f"push target {target} is not this tree's registered remote "
                f"{registered}"
            ),
            remedy="push to the registered remote or correct .vault.json",
        )
    ]


def _check_note(
    text: str, location: str, classification: str, seen: dict[str, str]
) -> list[Finding]:
    try:
        note = load_note(text)
    except FrontmatterError as exc:
        return [
            Finding(
                rule="schema",
                location=location,
                detail=_withhold(str(exc)),
                remedy="correct the frontmatter to the restricted grammar",
            )
        ]

    findings: list[Finding] = []
    if note.classification != classification:
        findings.append(
            Finding(
                rule="classification",
                location=location,
                detail=(
                    f"note is {note.classification!r} in a {classification!r} tree"
                ),
                remedy="move the note to its own tree or correct the field",
            )
        )
    if not note.within_cap:
        findings.append(
            Finding(
                rule="cap",
                location=location,
                detail=f"{note.size} characters exceeds the {CAPS[note.type]} cap",
                remedy="move older detail into archive/ and keep a pointer",
            )
        )
    findings.extend(_check_identity(note, location, seen))
    return findings


def _check_identity(note: Note, location: str, seen: dict[str, str]) -> list[Finding]:
    first = seen.get(note.id)
    if first is None:
        seen[note.id] = location
        return []
    return [
        Finding(
            rule="duplicate_id",
            location=location,
            detail=f"note id {note.id!r} is already used by {first}",
            remedy="give one of the two notes a different id",
        )
    ]


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _withhold(detail: str) -> str:
    """Keep a parser message out of the finding when it quotes a secret."""

    for rule, pattern in SECRET_RULES:
        if pattern.search(detail):
            return f"frontmatter is invalid; message withheld, it matched {rule}"
    return detail


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for match in _LINE_BREAK.finditer(text):
        starts.append(match.end())
    return starts
