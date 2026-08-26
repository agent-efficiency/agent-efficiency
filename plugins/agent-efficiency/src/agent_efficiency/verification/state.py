"""Compute a content-bound Git workspace identity."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


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
        untracked_names = _git(
            root, "ls-files", "--others", "--exclude-standard", "-z"
        ).split(b"\0")
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
