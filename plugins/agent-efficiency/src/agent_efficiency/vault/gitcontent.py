"""Reading what git is about to carry, rather than what is on disk.

A hook that checks the working copy checks the wrong thing. Staged content and
the working copy are independent: a note can be staged misclassified, the file
on disk can then be corrected, and a working-copy check passes while the commit
carries the bad content. The same gap exists on push, where what leaves the
machine is a set of commits and blobs that nobody has to have on disk in that
form.

So the hooks read from git. ``staged_items`` returns the content of the git
index, which is exactly what a commit will contain. ``push_plan`` returns the
tip tree of each ref being pushed, and every blob those commits would send.
Both hand back ``(path, bytes)`` pairs for the guards to check, so the hook
path and the filesystem path run the same rules.

Every failure is a ``GitError`` naming what could not be read.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ZERO = "0"
MISSING = b"missing"

Keep = Callable[[str], bool] | None


class GitError(ValueError):
    """Raised when git could not answer, or answered something unreadable."""


@dataclass
class PushPlan:
    """What a push would send.

    ``tips`` holds one ``(ref, items)`` pair per ref being pushed, where the
    items are that ref's whole tree. ``objects`` holds every blob the push
    would send, including blobs that only exist in history and in archives, as
    ``(location, bytes)`` pairs.
    """

    tips: list[tuple[str, list[tuple[str, bytes]]]] = field(default_factory=list)
    objects: list[tuple[str, bytes]] = field(default_factory=list)


def toplevel(start: Path) -> Path:
    """Return the working tree root of the repository holding ``start``."""

    text = _run(start, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    if not text:
        raise GitError(f"{start} is not inside a git repository")
    return Path(text)


def relative_prefix(repository: Path, root: Path) -> str:
    """Return the path of ``root`` inside ``repository``, as git spells it.

    An empty string when the vault tree is the repository itself. A vault tree
    that is not inside the repository is an error: the hook would otherwise
    check a set of files that has nothing to do with the tree it was given.
    """

    real_repository = repository.resolve()
    real_root = root.resolve()
    if real_root == real_repository:
        return ""
    try:
        return real_root.relative_to(real_repository).as_posix() + "/"
    except ValueError:
        raise GitError(
            f"the vault tree at {real_root} is not inside the repository at "
            f"{real_repository}, so git cannot be asked what it holds"
        ) from None


def staged_items(
    repository: Path, prefix: str, keep: Keep = None
) -> list[tuple[str, bytes]]:
    """Return the content of the git index, as paths relative to the vault tree.

    This is what a commit will hold: staged changes over everything else
    already tracked. The whole index is read rather than only the changed
    paths, because a rule such as "no two notes share an id" is a statement
    about the set, and a staged note can collide with a note nobody touched.
    """

    paths = _split(_run(repository, "ls-files", "-z", "--cached"))
    return _read_paths(repository, prefix, paths, keep)


def push_plan(
    repository: Path,
    refs: list[tuple[str, str, str, str]],
    prefix: str,
    keep: Keep = None,
) -> PushPlan:
    """Return the tree of each pushed ref and every blob the push would send.

    A deleted ref sends nothing, so it is skipped. When the remote's current
    value is unknown, every commit reachable from the local value is treated as
    outgoing, which is the safe reading: the alternative is to assume the
    remote already holds history nobody has confirmed.
    """

    plan = PushPlan()
    seen: set[str] = set()
    for local_ref, local_sha, _remote_ref, remote_sha in refs:
        if _is_zero(local_sha):
            continue
        plan.tips.append(
            (local_ref, _tree_items(repository, local_sha, prefix, keep))
        )
        for location, oid in _outgoing(
            repository, local_sha, remote_sha, prefix, keep
        ):
            if oid in seen:
                continue
            seen.add(oid)
            plan.objects.append((location, _read_object(repository, oid)))
    return plan


def parse_refs(text: str) -> list[tuple[str, str, str, str]]:
    """Parse the ref lines git writes to a pre-push hook's standard input."""

    refs = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 4:
            raise GitError(
                f"line {number} of the push input is not four fields: {line!r}"
            )
        refs.append((parts[0], parts[1], parts[2], parts[3]))
    return refs


def _tree_items(
    repository: Path, commit: str, prefix: str, keep: Keep
) -> list[tuple[str, bytes]]:
    entries = [
        (path, oid)
        for path, oid in _ls_tree(repository, commit)
        if _wanted(path, prefix, keep)
    ]
    return [
        (_strip(path, prefix), data)
        for path, data in _read_oids(repository, entries)
    ]


def _outgoing(
    repository: Path, local_sha: str, remote_sha: str, prefix: str, keep: Keep
) -> list[tuple[str, str]]:
    arguments = ["rev-list", "--objects", local_sha]
    if not _is_zero(remote_sha) and _has_object(repository, remote_sha):
        arguments.extend(["--not", remote_sha])

    found: list[tuple[str, str]] = []
    for line in _run(repository, *arguments).decode("utf-8", "replace").splitlines():
        oid, _, path = line.partition(" ")
        if not path or not _wanted(path, prefix, keep):
            continue
        found.append((f"{_strip(path, prefix)} (object {oid[:12]})", oid))
    return found


def _ls_tree(repository: Path, commit: str) -> list[tuple[str, str]]:
    entries = []
    for record in _split(_run(repository, "ls-tree", "-r", "-z", commit)):
        header, tab, path = record.partition("\t")
        if not tab:
            raise GitError(f"git ls-tree wrote a record without a path: {record!r}")
        fields = header.split()
        if len(fields) != 3 or fields[1] != "blob":
            continue
        entries.append((path, fields[2]))
    return entries


def _read_paths(
    repository: Path, prefix: str, paths: list[str], keep: Keep
) -> list[tuple[str, bytes]]:
    chosen = []
    for path in paths:
        if not _wanted(path, prefix, keep):
            continue
        chosen.append((path, f":{path}"))
    contents = _batch(repository, [spec for _, spec in chosen])
    return [
        (_strip(path, prefix), contents[spec]) for path, spec in chosen
    ]


def _read_oids(
    repository: Path, entries: list[tuple[str, str]]
) -> list[tuple[str, bytes]]:
    contents = _batch(repository, [oid for _, oid in entries])
    return [(path, contents[oid]) for path, oid in entries]


def _read_object(repository: Path, oid: str) -> bytes:
    return _batch(repository, [oid])[oid]


def _batch(repository: Path, specs: list[str]) -> dict[str, bytes]:
    """Read every named object in one git call.

    ``git cat-file --batch`` answers a list of names with a header line and the
    bytes of each object. One call for a hundred notes keeps a pre-commit hook
    to a cost a person does not notice.
    """

    if not specs:
        return {}
    for spec in specs:
        if "\n" in spec:
            raise GitError(f"{spec!r} holds a newline, so git cannot be asked for it")

    ordered = list(dict.fromkeys(specs))
    payload = ("\n".join(ordered) + "\n").encode("utf-8")
    output = _run(repository, "cat-file", "--batch", stdin=payload)

    contents: dict[str, bytes] = {}
    cursor = 0
    for spec in ordered:
        end = output.find(b"\n", cursor)
        if end < 0:
            raise GitError(f"git cat-file stopped before it answered for {spec!r}")
        header = output[cursor:end]
        cursor = end + 1
        fields = header.split()
        if len(fields) >= 2 and fields[1] == MISSING:
            raise GitError(f"git has no object for {spec!r}")
        if len(fields) != 3:
            raise GitError("git cat-file wrote a header this tool cannot read")
        try:
            size = int(fields[2])
        except ValueError:
            raise GitError(
                f"git cat-file wrote {fields[2]!r} where a size was expected"
            ) from None
        contents[spec] = output[cursor : cursor + size]
        cursor += size + 1
    return contents


def _has_object(repository: Path, oid: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{oid}^{{commit}}"],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _run(repository: Path, *arguments: str, stdin: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            input=stdin,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise GitError(f"git could not be run: {exc}") from None
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {arguments[0]} failed: {message}")
    return result.stdout


def _split(payload: bytes) -> list[str]:
    records = payload.split(b"\0")
    found = []
    for record in records:
        if not record:
            continue
        try:
            found.append(record.decode("utf-8"))
        except UnicodeDecodeError:
            raise GitError(
                "a tracked path is not valid UTF-8, so it cannot be checked"
            ) from None
    return found


def _under(path: str, prefix: str) -> bool:
    if not prefix:
        return True
    return path.startswith(prefix)


def _wanted(path: str, prefix: str, keep: Keep) -> bool:
    """Whether a repository path belongs to the tree and is worth reading.

    ``keep`` is the caller's rule, applied to the path relative to the vault
    tree. Filtering here rather than after reading means the blobs of files
    nobody checks are never loaded.
    """

    if not _under(path, prefix):
        return False
    return keep is None or keep(_strip(path, prefix))


def _strip(path: str, prefix: str) -> str:
    return path[len(prefix) :] if prefix else path


def _is_zero(sha: str) -> bool:
    return set(sha) == {ZERO}
