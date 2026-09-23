"""Choose which project note a session loads. A lookup, never a search.

Selection reads only each registered tree's generated ``index.json`` and the
git metadata of the working directory. It never opens a note body.

Order, first match wins:

1. The longest ``paths`` match. An absolute entry matches the working
   directory directly. A relative entry is relative to the repository root and
   counts only when the note's ``repos`` also match the repository. An absolute
   entry above the nearest repository root never matches, so a parent is never
   inherited by a nested repository.
2. With no path match, a ``repos`` match against the repository identity.
3. ``branches`` narrows an exact tie: a note naming the current branch wins,
   then a note naming no branch. A tie that remains is ``ambiguous``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agent_efficiency.vault.gitmeta import Repository
from agent_efficiency.vault.index import INDEX_JSON, INDEX_SCHEMA
from agent_efficiency.vault.root import VaultRootError, VaultTree, normalize_remote

STRING_FIELDS = ("id", "title", "type", "status", "hook", "path")
LIST_FIELDS = ("repos", "paths", "branches")


class SelectionError(ValueError):
    """Raised when a registered tree's index cannot be used."""


@dataclass(frozen=True)
class Selection:
    outcome: str
    classification: str | None
    entry: dict | None
    scope: tuple[str, ...]
    tied: tuple[str, ...] = ()


def load_index(tree: VaultTree) -> dict:
    path = tree.root / INDEX_JSON
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SelectionError(f"{path} could not be read: {exc}") from None
    schema = data.get("schema") if isinstance(data, dict) else None
    if (
        isinstance(schema, bool)
        or schema != INDEX_SCHEMA
        or not isinstance(data.get("notes"), list)
    ):
        raise SelectionError(f"{path} is not a schema {INDEX_SCHEMA} vault index")
    if data.get("classification") != tree.classification:
        raise SelectionError(
            f"{path} belongs to a {data.get('classification')!r} tree, "
            f"not {tree.classification!r}"
        )
    for position, entry in enumerate(data["notes"], start=1):
        problem = _entry_problem(entry)
        if problem:
            raise SelectionError(f"{path} note entry {position} {problem}")
    return data


def _entry_problem(entry: object) -> str | None:
    """Describe why an index entry has the wrong shape, or return ``None``.

    The index is the trust boundary: once it loads, selection and rendering
    rely on these fields having these types.
    """

    if not isinstance(entry, dict):
        return "is not an object"
    for field in STRING_FIELDS:
        if not isinstance(entry.get(field), str):
            return f"field {field!r} must be a string"
    for field in LIST_FIELDS:
        value = entry.get(field)
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            return f"field {field!r} must be a list of strings"
    return None


def select_project(
    cwd: Path, repository: Repository | None, indexes: Mapping[str, dict]
) -> Selection:
    where = cwd.expanduser().resolve()
    identity = repository.identity() if repository else ""
    candidates = [
        (classification, entry)
        for classification in sorted(indexes)
        for entry in indexes[classification]["notes"]
        if entry.get("type") == "project" and entry.get("status") == "active"
    ]

    scored: list[tuple[int, str, dict]] = []
    for classification, entry in candidates:
        depth = _path_depth(entry, where, repository, identity)
        if depth is not None:
            scored.append((depth, classification, entry))
    if not scored and identity:
        scored = [(0, c, e) for c, e in candidates if _repo_match(e, identity)]
    if not scored:
        return Selection("unmapped", None, None, _scope(None, indexes))

    best = max(depth for depth, _, _ in scored)
    top = [(c, e) for depth, c, e in scored if depth == best]
    if len(top) > 1:
        branch = repository.branch if repository else None
        specific = [
            (c, e) for c, e in top if branch and branch in e.get("branches", [])
        ]
        general = [(c, e) for c, e in top if not e.get("branches")]
        top = specific or general or top
    if len(top) > 1:
        tied = tuple(sorted(str(e["id"]) for _, e in top))
        return Selection("ambiguous", None, None, _scope(None, indexes), tied)
    classification, entry = top[0]
    return Selection("matched", classification, entry, _scope(classification, indexes))


def _path_depth(
    entry: dict, where: Path, repository: Repository | None, identity: str
) -> int | None:
    best: int | None = None
    for raw in entry.get("paths", []):
        if raw.startswith(("/", "~")):
            try:
                base = Path(raw).expanduser().resolve()
            except (RuntimeError, OSError, ValueError):
                # An unknown user, a NUL byte, or a symlink loop: the entry
                # names no directory, so it cannot match.
                continue
            if repository is not None and not _within(base, repository.root):
                continue
        else:
            if repository is None or not _repo_match(entry, identity):
                continue
            base = repository.root.joinpath(*PurePosixPath(raw.strip("/")).parts)
        if _within(where, base):
            depth = len(base.parts)
            best = depth if best is None else max(best, depth)
    return best


def _within(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


def _repo_match(entry: dict, identity: str) -> bool:
    if not identity:
        return False
    for raw in entry.get("repos", []):
        try:
            value = normalize_remote(raw)
        except VaultRootError:
            continue
        if value == identity or ("/" in value and identity.endswith("/" + value)):
            return True
    return False


def _scope(classification: str | None, indexes: Mapping[str, dict]) -> tuple[str, ...]:
    ordered = ["core"]
    if classification and classification != "core":
        ordered.append(classification)
    return tuple(name for name in ordered if name in indexes)
