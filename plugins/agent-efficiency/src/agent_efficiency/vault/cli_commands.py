"""Argparse wiring and handlers for the vault command group.

No vault command touches the database, so the group is dispatched before the
store is built. That keeps ``vault check`` usable from a git hook on a machine
where the runtime data directory is missing or unwritable. ``register`` and
``show`` read the data folder, so they first let an older host store be
copied into it; see ``host_data``.

Exit codes:

* 0, the command did what was asked and the tree is clean.
* 1, the guard ran and found something. Only ``check`` returns this.
* 2, the command could not run: bad arguments, a path that is not a vault
  tree, a tree that cannot be indexed, a marker that contradicts the
  arguments, or a proposal that cannot be applied as written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from agent_efficiency.host_data import settle_data_dir
from agent_efficiency.vault import gitcontent
from agent_efficiency.vault.config import (
    VaultConfigError,
    load_trees,
    register_tree,
    stale_entries,
)
from agent_efficiency.vault.gitcontent import GitError
from agent_efficiency.vault.guards import (
    Finding,
    GuardError,
    check_content,
    check_history_classification,
    check_tree,
    is_scannable,
    scan_secrets,
)
from agent_efficiency.vault.index import IndexError_, cap_overflow, write_index
from agent_efficiency.vault.migrate import MigrationError, apply_proposal, propose
from agent_efficiency.vault.prepare import prepare
from agent_efficiency.vault.render import ALLOWANCE
from agent_efficiency.vault.root import (
    MARKER,
    MARKER_SCHEMA,
    VaultRootError,
    VaultTree,
    find_tree,
    normalize_remote,
)
from agent_efficiency.vault.schema import CLASSIFICATIONS
from agent_efficiency.vault.select import SelectionError, load_index
from agent_efficiency.vault.walk import WalkError

# Every error type the vault modules declare. The CLI is the one place that has
# to know all of them: a type that escapes here reaches a person as a traceback
# instead of a message, and from a git hook it reaches them as a broken commit.
VAULT_ERRORS = (
    GuardError,
    GitError,
    IndexError_,
    MigrationError,
    SelectionError,
    VaultConfigError,
    VaultRootError,
    WalkError,
)

DIRECTORIES = ("projects", "feedback", "reference", "doctrine", "sessions")
HOOKS_DIRECTORY = ".githooks"

PRE_COMMIT = """#!/bin/sh
# Installed by: agent-efficiency vault init
# --staged checks the content of the git index, which is what this commit will
# hold. The working copy is a different thing and is not what is being
# committed.
set -e
root=$(git rev-parse --show-toplevel)
exec agent-efficiency vault check "$root" --staged
"""

PRE_PUSH = """#!/bin/sh
# Installed by: agent-efficiency vault init
# git passes the remote name as $1, the destination URL as $2, and the refs
# being pushed on standard input. --push reads those refs and checks the
# commits and blobs the push would send.
set -e
root=$(git rev-parse --show-toplevel)
exec agent-efficiency vault check "$root" --push --remote "$2"
"""

HOOKS = (("pre-commit", PRE_COMMIT), ("pre-push", PRE_PUSH))


def register(subparsers: argparse._SubParsersAction) -> None:
    vault = subparsers.add_parser("vault", help="Manage the cross-session vault.")
    vault_sub = vault.add_subparsers(dest="vault_command", required=True)

    init = vault_sub.add_parser(
        "init",
        help="Create a vault tree, or bring an existing one up to date.",
    )
    init.add_argument("root")
    init.add_argument("--classification", choices=CLASSIFICATIONS, required=True)
    init.add_argument(
        "--remote",
        default="",
        help="Git remote this tree may be pushed to.",
    )

    index = vault_sub.add_parser("index", help="Regenerate INDEX.md and index.json.")
    index.add_argument("root")

    check = vault_sub.add_parser(
        "check",
        help="Run the vault guards. Exit 1 when anything is found.",
    )
    check.add_argument("root")
    check.add_argument(
        "--remote",
        default=None,
        help="Destination of a push, to check against the registered remote.",
    )
    source = check.add_mutually_exclusive_group()
    source.add_argument(
        "--staged",
        action="store_true",
        help="Check the staged content rather than the working copy.",
    )
    source.add_argument(
        "--push",
        action="store_true",
        help=(
            "Check what a push would send. Ref lines are read from standard "
            "input in the form git gives a pre-push hook."
        ),
    )
    check.add_argument("--json", action="store_true")

    migrate = vault_sub.add_parser(
        "migrate",
        help="Move the existing memory and handoff stores into vault trees.",
    )
    migrate_sub = migrate.add_subparsers(dest="migrate_command", required=True)

    drafting = migrate_sub.add_parser(
        "propose",
        help="Write a proposal file for a person to review.",
    )
    drafting.add_argument("--memory-dir", default=None)
    drafting.add_argument("--remember-dir", default=None)
    drafting.add_argument("--out", required=True)
    drafting.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing proposal file, losing any edits in it.",
    )

    applying = migrate_sub.add_parser(
        "apply",
        help="Write the notes a reviewed proposal describes.",
    )
    applying.add_argument("proposal")
    applying.add_argument(
        "--tree",
        action="append",
        required=True,
        metavar="CLASSIFICATION=PATH",
        help="Repeatable, for example --tree work=~/notes/vault-work",
    )
    applying.add_argument(
        "--force",
        action="store_true",
        help="Replace notes that are already in a tree.",
    )

    registering = vault_sub.add_parser(
        "register",
        help="Register a vault tree so sessions can load it.",
    )
    registering.add_argument("root")

    showing = vault_sub.add_parser(
        "show",
        help="Print the vault context a session would receive in a directory.",
    )
    showing.add_argument("--cwd", default=None)


def dispatch(args: argparse.Namespace) -> int:
    handler = {
        "init": _init,
        "index": _index,
        "check": _check,
        "migrate": _migrate,
        "register": _register,
        "show": _show,
    }.get(args.vault_command)
    if handler is None:
        return _fail(f"unknown vault command {args.vault_command!r}")
    return handler(args)


def _init(args: argparse.Namespace) -> int:
    """Create a tree, or reconcile the arguments with the tree already there.

    Safe to run twice. Notes are never touched. An existing marker is never
    reclassified and an existing remote is never replaced: both are refused
    with a message, because either change silently redirects where private
    material ends up.
    """

    root = Path(args.root).expanduser()
    marker = root / MARKER

    if marker.exists():
        outcome = _reconcile(root, args)
        if outcome != 0:
            return outcome
    else:
        try:
            root.mkdir(parents=True, exist_ok=True)
            _write_marker(marker, args.classification, "origin", args.remote)
        except OSError as exc:
            return _fail(f"{root} could not be created: {exc}")

    try:
        for name in DIRECTORIES:
            (root / name).mkdir(parents=True, exist_ok=True)
        _write_hooks(root / HOOKS_DIRECTORY)
    except OSError as exc:
        return _fail(f"{root} could not be prepared: {exc}")

    try:
        write_index(root)
    except VAULT_ERRORS as exc:
        return _fail(f"tree created, but the index could not be written: {exc}")

    print(f"Vault tree ready at {root} ({args.classification})")
    print(f"Run: git init && git config --local core.hooksPath {HOOKS_DIRECTORY}")
    return 0


def _reconcile(root: Path, args: argparse.Namespace) -> int:
    try:
        tree = find_tree(root)
    except VAULT_ERRORS as exc:
        return _fail(f"{root} already holds a marker that cannot be read: {exc}")

    if tree.classification != args.classification:
        return _fail(
            f"{root} is already a {tree.classification!r} tree. Refusing to "
            f"reclassify it as {args.classification!r}. Edit {MARKER} by hand if "
            "that is what you intend."
        )

    if args.remote and tree.remote_url:
        try:
            differs = normalize_remote(args.remote) != tree.normalized_remote
        except VaultRootError as exc:
            return _fail(str(exc))
        if differs:
            return _fail(
                f"{root} already records the remote {tree.remote_url}. Refusing to "
                f"replace it with {args.remote}. Edit {MARKER} by hand if that is "
                "what you intend."
            )
        return 0

    if args.remote:
        try:
            _write_marker(
                root / MARKER, tree.classification, tree.preferred_remote, args.remote
            )
        except OSError as exc:
            return _fail(f"{root / MARKER} could not be updated: {exc}")
    return 0


def _index(args: argparse.Namespace) -> int:
    try:
        index = write_index(Path(args.root).expanduser())
    except VAULT_ERRORS as exc:
        return _fail(str(exc))
    print(f"Index written for {args.root}")
    for message in cap_overflow(index):
        print(f"over cap: {message}")
    return 0


def _check(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser()
    try:
        if args.staged:
            findings = _check_staged(root)
        elif args.push:
            findings = _check_push(root, args.remote)
        else:
            findings = check_tree(root, remote_url=args.remote)
    except VAULT_ERRORS as exc:
        return _fail(str(exc), as_json=args.json)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not findings,
                    "findings": [asdict(item) for item in findings],
                },
                indent=2,
            )
        )
    elif findings:
        for item in findings:
            print(f"{item.rule}: {item.location}: {item.detail}")
            print(f"  fix: {item.remedy}")
    else:
        print("No findings.")
    return 1 if findings else 0


def _check_staged(root: Path) -> list[Finding]:
    """Check the content of the git index, which is what a commit will hold."""

    tree, repository, prefix = _repository(root)
    items = gitcontent.staged_items(repository, prefix, keep=is_scannable)
    return check_content(tree, items)


def _check_push(root: Path, remote_url: str | None) -> list[Finding]:
    """Check what a push would send, reading git's ref lines from stdin.

    Three rules, and they are not the same rule. Every note rule is applied to
    the tree each pushed ref would leave at the remote, because that is the
    state the remote ends up in. Classification and the secret scan are also
    applied to every blob the push would send, history and archives included,
    because content that was committed and then corrected still travels with
    the push and still reaches the remote.
    """

    tree, repository, prefix = _repository(root)
    refs = gitcontent.parse_refs(sys.stdin.read())

    findings = check_content(tree, [], remote_url=remote_url)
    plan = gitcontent.push_plan(repository, refs, prefix, keep=is_scannable)

    carried: set[bytes] = set()
    for _ref, items in plan.tips:
        for _path, data in items:
            carried.add(hashlib.sha256(data).digest())
        findings.extend(check_content(tree, items))

    findings.extend(
        check_history_classification(
            tree,
            [
                (location, data)
                for location, data in plan.objects
                if hashlib.sha256(data).digest() not in carried
            ],
        )
    )

    for location, data in plan.objects:
        if hashlib.sha256(data).digest() in carried:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(scan_secrets(text, location))
    return _unique(findings)


def _unique(findings: list[Finding]) -> list[Finding]:
    """Drop repeats, keeping the first of each. Two refs can carry one note."""

    seen: set[tuple[str, str, str]] = set()
    kept = []
    for item in findings:
        identity = (item.rule, item.location, item.detail)
        if identity in seen:
            continue
        seen.add(identity)
        kept.append(item)
    return kept


def _repository(root: Path) -> tuple[VaultTree, Path, str]:
    tree = find_tree(root)
    repository = gitcontent.toplevel(tree.root)
    return tree, repository, gitcontent.relative_prefix(repository, tree.root)


def _migrate(args: argparse.Namespace) -> int:
    if args.migrate_command == "propose":
        return _propose(args)
    if args.migrate_command == "apply":
        return _apply(args)
    return _fail(f"unknown migrate command {args.migrate_command!r}")


def _propose(args: argparse.Namespace) -> int:
    """Write a proposal for review.

    An existing proposal is never replaced without ``--force``. A person edits
    that file by hand, and those edits are the only thing that decides where a
    note lands.
    """

    if not args.memory_dir and not args.remember_dir:
        return _fail("give --memory-dir, --remember-dir, or both")

    out = Path(args.out).expanduser()
    if out.exists() and not args.force:
        return _fail(
            f"{out} already exists. It is reviewed by hand, so it is not "
            "replaced. Move it aside, or pass --force to lose the edits in it."
        )

    try:
        proposal = propose(
            memory_dir=Path(args.memory_dir).expanduser() if args.memory_dir else None,
            remember_dir=(
                Path(args.remember_dir).expanduser() if args.remember_dir else None
            ),
        )
    except VAULT_ERRORS as exc:
        return _fail(str(exc))

    try:
        out.write_text(
            json.dumps(proposal, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        return _fail(f"{out} could not be written: {exc}")

    print(f"Proposed {len(proposal['entries'])} notes to {out}")
    print(
        "The classification on each entry is a keyword guess. Review every one "
        "of them, then run: vault migrate apply"
    )
    return 0


def _apply(args: argparse.Namespace) -> int:
    trees = _trees(args.tree)
    if trees is None:
        return 2

    location = Path(args.proposal).expanduser()
    try:
        proposal = json.loads(location.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _fail(f"{location} is not valid JSON: {exc}")
    except UnicodeDecodeError as exc:
        return _fail(f"{location} is not valid UTF-8: {exc}")
    except OSError as exc:
        return _fail(f"{location} could not be read: {exc}")

    try:
        written = apply_proposal(proposal, trees, overwrite=args.force)
    except VAULT_ERRORS as exc:
        return _fail(str(exc))

    print(f"Wrote {len(written)} files from {location}")
    for classification, root in sorted(trees.items()):
        print(f"Next: agent-efficiency vault check {root}  ({classification})")
    return 0


def _trees(pairs: list[str]) -> dict[str, Path] | None:
    """Parse every --tree argument, or report the first one that is wrong.

    A repeated classification is refused rather than resolved by last-wins. A
    typo on this line is how work notes end up in a core tree.
    """

    trees: dict[str, Path] = {}
    for pair in pairs:
        classification, separator, location = pair.partition("=")
        if not separator or not classification or not location:
            _fail(f"--tree expects CLASSIFICATION=PATH, got {pair!r}")
            return None
        if classification not in CLASSIFICATIONS:
            _fail(
                f"--tree names the unknown classification {classification!r}; "
                f"allowed are {', '.join(CLASSIFICATIONS)}"
            )
            return None
        if classification in trees:
            _fail(
                f"--tree names {classification!r} twice, as {trees[classification]} "
                f"and as {location}"
            )
            return None
        trees[classification] = Path(location).expanduser()
    return trees


def _write_marker(
    marker: Path, classification: str, preferred_remote: str, remote_url: str
) -> None:
    marker.write_text(
        json.dumps(
            {
                "schema": MARKER_SCHEMA,
                "classification": classification,
                "preferred_remote": preferred_remote,
                "remote_url": remote_url,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_hooks(hooks: Path) -> None:
    hooks.mkdir(parents=True, exist_ok=True)
    for name, content in HOOKS:
        target = hooks / name
        target.write_text(content, encoding="utf-8")
        os.chmod(target, _permissions(0o777))


def _permissions(bits: int) -> int:
    mask = os.umask(0o022)
    os.umask(mask)
    return bits & ~mask


def _register(args: argparse.Namespace) -> int:
    root = settle_data_dir(args.data_dir, discover=True)
    try:
        stale = stale_entries(root)
        tree = register_tree(root, Path(args.root))
    except VaultConfigError as exc:
        return _fail(str(exc))
    for entry in stale:
        print(f"Dropped a registered tree that no longer exists: {entry}")
    print(f"Registered the {tree.classification} tree at {tree.root}.")
    return 0


def _show(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd or os.getcwd())
    root = settle_data_dir(args.data_dir, discover=True)
    prepared = prepare(cwd, root)
    if prepared.status != "ready":
        print(f"No vault context: {prepared.status} ({prepared.reason}).")
        if prepared.status == "degraded":
            detail = _degraded_detail(root)
            if detail:
                print(detail)
        return 0 if prepared.status == "unavailable" else 2
    rendered = prepared.rendered
    omitted = sum(count for _, count in rendered.omitted)
    print(rendered.text)
    print()
    print(
        f"Summary: {prepared.selection.outcome} | "
        f"{len(rendered.text)} of {ALLOWANCE} characters | "
        f"head {rendered.head_chars} | notes {rendered.notes_selected} | "
        f"omitted {omitted} | reason {prepared.reason or 'none'}"
    )
    return 0


def _degraded_detail(data_root: Path) -> str | None:
    """Rerun the checks that can fail and return the first error message.

    The receipt holds only a reason code, so this is where a person sees which
    entry or index is broken. Nothing here is stored.
    """

    try:
        trees = load_trees(data_root)
    except (VaultConfigError, SelectionError) as exc:
        return str(exc)
    for tree in trees:
        try:
            load_index(tree)
        except (VaultConfigError, SelectionError) as exc:
            return str(exc)
    return None


def _fail(message: str, *, as_json: bool = False) -> int:
    if as_json:
        print(json.dumps({"ok": False, "error": message}, indent=2))
    else:
        print(message, file=sys.stderr)
    return 2
