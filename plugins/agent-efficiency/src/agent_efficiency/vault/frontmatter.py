"""Restricted frontmatter grammar for vault notes.

The runtime carries no third-party dependencies and the standard library has no
YAML parser, so notes use a restricted grammar instead of general YAML. Keys map
to a scalar or a bracketed list. Nesting, multi-line values, anchors, and quoted
escapes are refused by name rather than guessed at.

The grammar:

* A delimiter line is any line whose stripped form is ``---``. The note opens
  with one and the frontmatter ends at the next one. Input is split with
  ``str.splitlines()``, so CR, LF, and CRLF line endings all parse.
* A key line is ``key: value``. The key carries no whitespace, leading
  indentation is refused, and a repeated key is refused.
* A scalar value may not contain ``[``, ``]``, ``{``, or ``}``.
* A list is written ``[a, b]``. Items split on commas, and an item may not
  contain ``"``, ``'``, ``[``, ``]``, ``{``, or ``}``. A value that contains a
  comma is therefore not expressible in a list. That limit is deliberate: this
  grammar has no quoting, so a quoted item would keep its quotes and split at
  the comma inside it, which corrupts the value without any signal.
* The body keeps every retained line exactly as written, including leading
  indentation and interior blank lines. Only leading and trailing blank lines
  are removed, so a body that opens with an indented code block survives.
"""

from __future__ import annotations

from dataclasses import dataclass

DELIMITER = "---"

SCALAR_BANNED = "[]{}"
ITEM_BANNED = "\"'[]{}"


class FrontmatterError(ValueError):
    """Raised when frontmatter does not match the restricted grammar."""


@dataclass(frozen=True)
class ParsedNote:
    fields: dict[str, str | list[str]]
    body: str


def _is_delimiter(line: str) -> bool:
    return line.strip() == DELIMITER


def parse_note(text: str) -> ParsedNote:
    lines = text.splitlines()
    if not lines or not _is_delimiter(lines[0]):
        raise FrontmatterError("line 1: note must open with a --- delimiter line")

    end = None
    for index in range(1, len(lines)):
        if _is_delimiter(lines[index]):
            end = index
            break
    if end is None:
        raise FrontmatterError(
            f"line {len(lines)}: frontmatter has no closing --- delimiter"
        )

    fields: dict[str, str | list[str]] = {}
    for number, line in enumerate(lines[1:end], start=2):
        if not line.strip():
            continue
        if line[:1].isspace():
            raise FrontmatterError(f"line {number}: indentation is not supported")
        key, separator, raw = line.partition(":")
        if not separator:
            raise FrontmatterError(f"line {number}: expected 'key: value'")
        key = key.strip()
        if not key or any(character.isspace() for character in key):
            raise FrontmatterError(f"line {number}: invalid key {key!r}")
        if key in fields:
            raise FrontmatterError(f"line {number}: duplicate key {key!r}")
        fields[key] = _parse_value(raw.strip(), number)

    return ParsedNote(fields=fields, body=_body(lines[end + 1 :]))


def _body(lines: list[str]) -> str:
    start = 0
    stop = len(lines)
    while start < stop and not lines[start].strip():
        start += 1
    while stop > start and not lines[stop - 1].strip():
        stop -= 1
    return "\n".join(lines[start:stop])


def _parse_value(raw: str, number: int) -> str | list[str]:
    if raw.startswith("["):
        if not raw.endswith("]"):
            raise FrontmatterError(f"line {number}: unterminated list")
        inner = raw[1:-1].strip()
        if not inner:
            return []
        items = [item.strip() for item in inner.split(",")]
        for item in items:
            if not item:
                raise FrontmatterError(f"line {number}: empty list item")
            if any(character in item for character in ITEM_BANNED):
                raise FrontmatterError(
                    f"line {number}: unsupported character in list item {item!r}"
                )
        return items
    if not raw:
        raise FrontmatterError(f"line {number}: empty value")
    if any(character in raw for character in SCALAR_BANNED):
        raise FrontmatterError(
            f"line {number}: unsupported character in scalar {raw!r}"
        )
    return raw
