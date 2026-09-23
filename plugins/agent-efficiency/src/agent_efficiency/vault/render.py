"""Render the selected vault notes into one bounded block of session context.

The allowance bounds the whole block, because the whole block is what a session
pays for. The matched project head renders in full. Every other note renders as
one line, its path without ``.md`` and its hook, and the agent opens the file
when the line applies.

Lines fill in a fixed order: the tree notes of each tree in scope, core first,
then the other active projects. Whatever does not fit is counted in a closing
line, so an omission is visible rather than silent.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from agent_efficiency.vault.frontmatter import FrontmatterError
from agent_efficiency.vault.root import VaultTree
from agent_efficiency.vault.schema import CAPS, load_note
from agent_efficiency.vault.select import Selection

ALLOWANCE = 9000
# Held back once anything must be dropped, so the omission line always fits.
OMISSION_RESERVE = 160
# Held back from every rendering, whatever the cause, so the line a reselection
# adds after the header still fits the allowance and the digest of the rendered
# text does not depend on the cause. It covers that line and its newline.
RESELECT_RESERVE = 100
HEAD_CAP = CAPS["project"]
TITLE_CAP = 100
LINE_TYPES = ("feedback", "doctrine", "reference")

HEADER = (
    "Vault context: project state and standing notes from the user's vault. "
    "Each line below a heading is a note file in that folder, written without "
    "its .md ending. Open the file when its line applies to the task."
)
UNMAPPED = "No vault project matches this directory, so only core notes are listed."
CUT_NOTICE = "Vault context was cut to fit its allowance."
HEAD_UNREADABLE = (
    "The project note for this directory could not be loaded. "
    "Run agent-efficiency vault check on its tree."
)


class RenderError(ValueError):
    """Raised when a selected note cannot be read or parsed."""


@dataclass(frozen=True)
class Rendered:
    text: str
    notes_selected: int
    head_chars: int
    chars_omitted: int
    omitted: tuple[tuple[str, int], ...]
    truncated: bool
    head_error: bool
    over_cap: bool


ReadBody = Callable[[VaultTree, dict], str]


def read_body(tree: VaultTree, entry: dict) -> str:
    """Load one note and return its body. The caller rechecks the cap."""

    try:
        root = tree.root.resolve()
        path = (root / str(entry["path"])).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise RenderError(f"{entry['path']} could not be resolved: {exc}") from None
    if root not in path.parents:
        raise RenderError(f"{entry['path']} is outside its tree")
    try:
        return load_note(path.read_text(encoding="utf-8")).body.strip()
    except (OSError, UnicodeDecodeError, FrontmatterError) as exc:
        raise RenderError(f"{path} could not be loaded: {exc}") from None


def render(
    selection: Selection,
    trees: Mapping[str, VaultTree],
    indexes: Mapping[str, dict],
    *,
    allowance: int = ALLOWANCE,
    read: ReadBody = read_body,
) -> Rendered:
    lead = [HEADER]
    head_chars = 0
    head_omitted = 0
    truncated = False
    head_error = False
    head_loaded = False
    over_cap = False
    matched: tuple[str, str] | None = None

    if selection.outcome == "matched" and selection.entry and selection.classification:
        entry = selection.entry
        tree = trees[selection.classification]
        matched = (selection.classification, str(entry["id"]))
        try:
            body = read(tree, entry)
        except RenderError:
            head_error = True
            lead.append(HEAD_UNREADABLE)
        else:
            head_loaded = True
            # The cap is on the whole file, so a body that fits can still
            # belong to a note that is over its cap.
            over_cap = entry.get("within_cap") is False or len(body) > HEAD_CAP
            if len(body) > HEAD_CAP:
                cut = _cut(body, HEAD_CAP)
                head_omitted = len(body) - len(cut)
                body = cut
                truncated = True
            head_chars = len(body)
            lead.append(
                f"## Project: {str(entry['title'])[:TITLE_CAP]}\n"
                f"File: {tree.root / str(entry['path'])}\n{body}"
            )
            if over_cap:
                lead.append(
                    f"The project note is over its {HEAD_CAP} character cap"
                    + (", so the rest is not shown" if truncated else "")
                    + ". Move its history into the archive."
                )
    elif selection.outcome == "ambiguous":
        lead.append(
            "Several vault projects match this directory equally "
            f"({', '.join(selection.tied)}), so none was loaded. "
            "Add paths or branches to one of them."
        )
    else:
        lead.append(UNMAPPED)

    groups = _groups(selection, trees, indexes, matched)
    first = "\n\n".join(lead)
    used = len(first)
    needed = sum(
        2 + len(heading) + sum(len(line) + 1 for line in lines)
        for _, heading, lines in groups
    )
    budget = allowance if used + needed <= allowance else allowance - OMISSION_RESERVE

    parts = [first]
    selected = 1 if head_loaded else 0
    omitted: Counter[str] = Counter()
    # The part of a head cut to its cap was left out too.
    chars_omitted = head_omitted
    stopped = False
    for category, heading, lines in groups:
        accepted: list[str] = []
        for line in lines:
            # The first line of a group also pays for the blank-line separator
            # and the heading.
            cost = len(line) + 1 + (0 if accepted else 2 + len(heading))
            if not stopped and used + cost <= budget:
                accepted.append(line)
                used += cost
            else:
                stopped = True
                omitted[category] += 1
                chars_omitted += len(line) + 1
        if accepted:
            parts.append(heading + "\n" + "\n".join(accepted))
            selected += len(accepted)
    if omitted:
        counts = ", ".join(f"{count} {name}" for name, count in sorted(omitted.items()))
        parts.append(
            f"Omitted for space: {counts}. Each tree's INDEX.md lists every note."
        )
    text = "\n\n".join(parts)
    if len(text) > allowance:
        # The lead is not budgeted line by line, so a very long tie list or
        # head can still overflow. Cut it at a line and say so.
        text = _fit(text, allowance)
        truncated = True
    return Rendered(
        text=text,
        notes_selected=selected,
        head_chars=head_chars,
        chars_omitted=chars_omitted,
        omitted=tuple(sorted(omitted.items())),
        truncated=truncated,
        head_error=head_error,
        over_cap=over_cap,
    )


def _groups(
    selection: Selection,
    trees: Mapping[str, VaultTree],
    indexes: Mapping[str, dict],
    matched: tuple[str, str] | None,
) -> list[tuple[str, str, list[str]]]:
    groups: list[tuple[str, str, list[str]]] = []
    for name in selection.scope:
        lines = [
            _line(entry)
            for entry in indexes[name]["notes"]
            if entry.get("type") in LINE_TYPES and entry.get("status") == "active"
        ]
        if lines:
            groups.append(
                ("notes", f"## {name.capitalize()} notes in {trees[name].root}", lines)
            )
    for name in selection.scope:
        lines = [
            _line(entry)
            for entry in indexes[name]["notes"]
            if entry.get("type") == "project"
            and entry.get("status") == "active"
            and (name, str(entry.get("id"))) != matched
        ]
        if lines:
            groups.append(
                (
                    "other projects",
                    f"## Other active projects in {trees[name].root}",
                    lines,
                )
            )
    return groups


def _line(entry: dict) -> str:
    path = str(entry["path"])
    stem = path.removesuffix(".md")
    return f"- {stem}: {entry['hook']}"


def _fit(text: str, allowance: int) -> str:
    """Cut ``text`` at a line so it and the cut notice fit the allowance."""

    limit = allowance - len(CUT_NOTICE) - 2
    kept = text[: max(text.rfind("\n", 0, limit + 1), 0)].rstrip()
    return f"{kept}\n\n{CUT_NOTICE}" if kept else CUT_NOTICE


def _cut(body: str, limit: int) -> str:
    head = body[:limit]
    newline = head.rfind("\n")
    return head[:newline].rstrip() if newline > 0 else head
