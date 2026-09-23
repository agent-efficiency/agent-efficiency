"""Select and render vault context for one working directory, with no host.

Everything here reads files and returns values. Nothing is written and the
runtime database is never touched, so the CLI can show exactly what a session
would receive.

The time budget is checked between steps. A read that hangs cannot be
interrupted from inside the process; the host's two-second hook timeout is the
final backstop for that case.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_efficiency.vault.config import VaultConfigError, load_trees
from agent_efficiency.vault.gitmeta import GitMetaError, find_repository
from agent_efficiency.vault.render import (
    ALLOWANCE,
    RESELECT_RESERVE,
    Rendered,
    render,
)
from agent_efficiency.vault.root import VaultTree
from agent_efficiency.vault.select import (
    Selection,
    SelectionError,
    load_index,
    select_project,
)

TIME_BUDGET_NS = 1_000_000_000
EMPTY_DIGEST = "0" * 16


@dataclass(frozen=True)
class Prepared:
    status: str
    reason: str | None
    selection: Selection | None
    rendered: Rendered | None
    revision: str
    digest: str


def prepare(
    cwd: Path,
    data_root: Path,
    *,
    clock: Callable[[], int] = time.perf_counter_ns,
) -> Prepared:
    """Return ``ready``, ``unavailable``, or ``degraded`` with one reason code."""

    started = clock()
    try:
        trees = load_trees(data_root)
    except VaultConfigError:
        return stopped("degraded", "parse_error")
    if not trees:
        return stopped("unavailable", "no_trees")
    by_name = {tree.classification: tree for tree in trees}
    try:
        indexes = {name: load_index(tree) for name, tree in by_name.items()}
    except SelectionError:
        return stopped("degraded", "parse_error")
    boundary = None
    try:
        repository = find_repository(cwd)
    except GitMetaError as exc:
        repository = None
        boundary = exc.root
    try:
        selection = select_project(cwd, repository, indexes, boundary=boundary)
        if clock() - started > TIME_BUDGET_NS:
            return stopped("degraded", "timeout")
        rendered = render(
            selection, by_name, indexes, allowance=ALLOWANCE - RESELECT_RESERVE
        )
    except (KeyError, TypeError, OSError, RuntimeError, ValueError):
        # A hand-edited index can drop a field the generator always writes,
        # and the working directory or a note path can fail to resolve.
        return stopped("degraded", "parse_error")
    if clock() - started > TIME_BUDGET_NS:
        return stopped("degraded", "timeout")
    return Prepared(
        status="ready",
        reason=_reason(selection, rendered),
        selection=selection,
        rendered=rendered,
        revision=_revision(by_name, selection.scope),
        digest=hashlib.sha256(rendered.text.encode("utf-8")).hexdigest()[:16],
    )


def stopped(status: str, reason: str) -> Prepared:
    return Prepared(status, reason, None, None, EMPTY_DIGEST, EMPTY_DIGEST)


def _reason(selection: Selection, rendered: Rendered) -> str | None:
    """Pick the one reason code a receipt carries. Earlier checks win."""

    if rendered.head_error:
        return "parse_error"
    if rendered.over_cap:
        return "cap_exceeded"
    if rendered.omitted:
        return "over_budget"
    if selection.outcome in {"unmapped", "ambiguous"}:
        return selection.outcome
    return None


def _revision(trees: Mapping[str, VaultTree], scope: tuple[str, ...]) -> str:
    parts = []
    for name in scope:
        root = trees[name].root.resolve()
        try:
            repository = find_repository(root)
        except GitMetaError:
            repository = None
        head = repository.head if repository and repository.root == root else None
        parts.append(f"{name}={head or 'none'}")
    return hashlib.sha256(";".join(parts).encode("utf-8")).hexdigest()[:16]
