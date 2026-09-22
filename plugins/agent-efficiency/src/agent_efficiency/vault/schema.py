"""Vault note model, field validation, and size caps."""

from __future__ import annotations

from dataclasses import dataclass

from agent_efficiency.vault.frontmatter import FrontmatterError, parse_note

SCHEMA_VERSION = "1"

TYPES = ("project", "feedback", "reference", "doctrine", "session")
CLASSIFICATIONS = ("core", "work", "private")
STATUSES = ("active", "dormant", "done")

REQUIRED = ("schema", "id", "title", "type", "classification", "status", "hook")
LIST_FIELDS = ("repos", "paths", "branches", "links")
OPTIONAL = ("updated", *LIST_FIELDS)
KNOWN_KEYS = frozenset(REQUIRED + OPTIONAL)

# A project note is hot state plus a pointer to its history, so its cap is
# deliberately tight and the overflow belongs in an archive. A feedback,
# reference, or doctrine note is a standing rule with no history in it, so
# capping it low would push the rule itself into an archive and defeat the
# point. Measured against a real store of 104 notes, 3,500 keeps every rule
# whole while still bounding a runaway one.
CAPS = {
    "project": 3000,
    "feedback": 3500,
    "reference": 3500,
    "doctrine": 3500,
    "session": 1000,
}


@dataclass(frozen=True)
class Note:
    """A parsed vault note.

    ``id`` is opaque to the parser, but later consumers use it as a file name,
    so it may not contain ``/`` or ``\\`` and may not be a path traversal
    component. ``load_note`` rejects an id that breaks those rules.

    ``size`` is the count of Unicode code points in the complete input, which
    includes the frontmatter and both delimiter lines, not the body alone.
    """

    id: str
    title: str
    type: str
    classification: str
    status: str
    hook: str
    updated: str
    repos: tuple[str, ...]
    paths: tuple[str, ...]
    branches: tuple[str, ...]
    links: tuple[str, ...]
    body: str
    size: int

    @property
    def within_cap(self) -> bool:
        """Whether the note fits its type cap. The cap is inclusive.

        A note whose ``size`` equals ``CAPS[self.type]`` is within cap.
        """
        return self.size <= CAPS[self.type]


def load_note(text: str) -> Note:
    parsed = parse_note(text)
    fields = parsed.fields

    for key in fields:
        if key not in KNOWN_KEYS:
            raise FrontmatterError(
                f"unknown field {key!r}; allowed fields are "
                f"{', '.join(sorted(KNOWN_KEYS))}"
            )

    for key in REQUIRED:
        if key not in fields:
            raise FrontmatterError(f"missing required field {key!r}")

    schema = _scalar(fields, "schema")
    if schema != SCHEMA_VERSION:
        raise FrontmatterError(f"unsupported schema version {schema!r}")

    note_id = _note_id(fields)
    note_type = _one_of(fields, "type", TYPES)
    classification = _one_of(fields, "classification", CLASSIFICATIONS)
    status = _one_of(fields, "status", STATUSES)

    return Note(
        id=note_id,
        title=_scalar(fields, "title"),
        type=note_type,
        classification=classification,
        status=status,
        hook=_scalar(fields, "hook"),
        updated=_scalar(fields, "updated") if "updated" in fields else "",
        repos=_list(fields, "repos"),
        paths=_list(fields, "paths"),
        branches=_list(fields, "branches"),
        links=_list(fields, "links"),
        body=parsed.body,
        size=len(text),
    )


def _note_id(fields: dict[str, str | list[str]]) -> str:
    value = _scalar(fields, "id")
    for separator in ("/", "\\"):
        if separator in value:
            raise FrontmatterError(
                f"field 'id' must not contain {separator!r}, got {value!r}"
            )
    if value in (".", ".."):
        raise FrontmatterError(
            f"field 'id' must not be a path traversal component, got {value!r}"
        )
    return value


def _scalar(fields: dict[str, str | list[str]], key: str) -> str:
    value = fields[key]
    if not isinstance(value, str):
        raise FrontmatterError(f"field {key!r} must be a scalar")
    return value


def _one_of(
    fields: dict[str, str | list[str]], key: str, allowed: tuple[str, ...]
) -> str:
    value = _scalar(fields, key)
    if value not in allowed:
        raise FrontmatterError(
            f"field {key!r} must be one of {', '.join(allowed)}, got {value!r}"
        )
    return value


def _list(fields: dict[str, str | list[str]], key: str) -> tuple[str, ...]:
    if key not in fields:
        return ()
    value = fields[key]
    if not isinstance(value, list):
        raise FrontmatterError(f"field {key!r} must be a list")
    return tuple(value)
