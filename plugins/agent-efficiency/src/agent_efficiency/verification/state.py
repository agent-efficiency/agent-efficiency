"""Compute a content-bound Git workspace identity."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


# Cache folders that test and lint tools write into the workspace they run in.
# Untracked files inside them are not part of the workspace identity, so a
# check that fills them is still a check of the same files.
CACHE_FOLDERS = frozenset(
    {b"__pycache__", b".pytest_cache", b".mypy_cache", b".ruff_cache"}
)


def is_cache_path(root: Path, relative: bytes) -> bool:
    """Whether a path git reports lies inside a real tool cache folder.

    Only content beneath a folder with a cache name counts, and every such
    folder on the way must be a real directory. A file or a symlink that only
    carries a cache name is an ordinary workspace change, and a symlink still
    gets the check that it stays inside the workspace.
    """

    parts = relative.split(b"/")
    found = False
    for depth, part in enumerate(parts[:-1], start=1):
        if part not in CACHE_FOLDERS:
            continue
        names = (item.decode("utf-8", "surrogateescape") for item in parts[:depth])
        folder = root.joinpath(*names)
        if folder.is_symlink() or not folder.is_dir():
            return False
        found = True
    return found


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    digest: str | None
    commit: str | None
    dirty: bool
    result: str
    changed_paths: tuple[str, ...] | None = None


def workspace_state(root: Path, config_digest: str) -> WorkspaceState:
    try:
        git_root = Path(
            _git(root, "rev-parse", "--show-toplevel").decode().strip()
        ).resolve()
        if git_root != root.resolve():
            return WorkspaceState(None, None, False, "inconclusive")
        commit = _git(root, "rev-parse", "HEAD").decode().strip()
        if len(commit) != 40:
            return WorkspaceState(None, None, False, "inconclusive")
        tracked = _git(root, "diff", "--binary", "HEAD", "--")
        tracked_names = _git(root, "diff", "--name-only", "-z", "HEAD", "--").split(
            b"\0"
        )
        untracked_names = [
            name
            for name in _git(
                root, "ls-files", "--others", "--exclude-standard", "-z"
            ).split(b"\0")
            if not is_cache_path(root, name)
        ]
        digest = hashlib.sha256()
        digest.update(b"agent-efficiency-workspace-v1\0")
        digest.update(commit.encode())
        digest.update(b"\0")
        digest.update(config_digest.encode())
        digest.update(b"\0tracked\0")
        digest.update(tracked)
        has_untracked = False
        for raw_name in sorted(name for name in untracked_names if name):
            try:
                relative = raw_name.decode("utf-8")
                candidate = (root / relative).resolve()
                candidate.relative_to(root)
                if not candidate.is_file():
                    continue
                content = candidate.read_bytes()
            except (OSError, UnicodeDecodeError, ValueError):
                return WorkspaceState(None, commit, True, "inconclusive")
            has_untracked = True
            digest.update(b"\0untracked\0")
            digest.update(hashlib.sha256(raw_name).digest())
            digest.update(hashlib.sha256(content).digest())
        try:
            changed_paths = tuple(
                sorted(
                    name.decode("utf-8")
                    for name in {*tracked_names, *untracked_names}
                    if name
                )
            )
        except UnicodeDecodeError:
            return WorkspaceState(None, commit, True, "inconclusive")
        return WorkspaceState(
            "sha256:" + digest.hexdigest(),
            commit,
            bool(tracked) or has_untracked,
            "ready",
            changed_paths,
        )
    except (OSError, subprocess.SubprocessError):
        return WorkspaceState(None, None, False, "inconclusive")


def _git(root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=10,
    )
    return completed.stdout
