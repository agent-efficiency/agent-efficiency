"""Read repository identity from git metadata files, without running git.

The session-start hook has a two-second budget. Starting a git process costs
tens of milliseconds before it does any work, and selection needs only facts
git keeps in plain files: the working tree root, the remotes, the current
branch, and the head commit. This module reads those files directly.

A worktree has its own git directory for HEAD and a shared common directory
for refs and config, so identity always comes from the common directory and
every worktree of one repository resolves the same way.

Repositories that use the reftable ref format are reported as unreadable,
and the caller falls back to matching by path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agent_efficiency.vault.root import VaultRootError, normalize_remote

_REMOTE_SECTION = re.compile(r'^\[\s*remote\s+"([^"]+)"\s*\]$')
_URL = re.compile(r"^url\s*=\s*(.+)$")
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class GitMetaError(OSError):
    """Raised when a git directory exists but its metadata cannot be read."""


@dataclass(frozen=True)
class Repository:
    root: Path
    git_dir: Path
    common_dir: Path
    remotes: tuple[tuple[str, str], ...]
    branch: str | None
    head: str | None

    def identity(self) -> str:
        """Return the normalized preferred remote, or an empty string.

        ``origin`` is preferred. A repository with exactly one remote uses it.
        Several remotes and none named ``origin`` give no identity, so the
        repository is matched by path alone rather than by a guessed remote.
        """

        urls = dict(self.remotes)
        url = urls.get("origin")
        if url is None and len(urls) == 1:
            url = next(iter(urls.values()))
        if not url:
            return ""
        try:
            return normalize_remote(url)
        except VaultRootError:
            return ""


def find_repository(start: Path) -> Repository | None:
    """Return the nearest repository at or above ``start``, or ``None``."""

    current = start.expanduser().resolve()
    for candidate in (current, *current.parents):
        marker = candidate / ".git"
        if marker.is_dir():
            return _load(candidate, marker)
        if marker.is_file():
            return _load(candidate, _gitdir_from_file(candidate, marker))
    return None


def _gitdir_from_file(root: Path, marker: Path) -> Path:
    text = _read(marker).strip()
    if not text.startswith("gitdir:"):
        raise GitMetaError(f"{marker} does not name a git directory")
    target = Path(text[len("gitdir:") :].strip())
    return (target if target.is_absolute() else root / target).resolve()


def _load(root: Path, git_dir: Path) -> Repository:
    common_dir = git_dir
    common_file = git_dir / "commondir"
    if common_file.is_file():
        relative = Path(_read(common_file).strip())
        common_dir = (
            relative if relative.is_absolute() else git_dir / relative
        ).resolve()
    if (common_dir / "reftable").is_dir():
        raise GitMetaError(f"{common_dir} uses reftable refs, which are not read")
    branch, head = _head(git_dir, common_dir)
    return Repository(
        root=root,
        git_dir=git_dir,
        common_dir=common_dir,
        remotes=_remotes(common_dir / "config"),
        branch=branch,
        head=head,
    )


def _head(git_dir: Path, common_dir: Path) -> tuple[str | None, str | None]:
    head_file = git_dir / "HEAD"
    if not head_file.is_file():
        raise GitMetaError(f"{git_dir} has no HEAD file")
    text = _read(head_file).strip()
    if _SHA.match(text):
        return None, text
    if not text.startswith("ref:"):
        raise GitMetaError(f"{head_file} is not a ref or a commit id")
    ref = text[len("ref:") :].strip()
    branch = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else None
    return branch, _resolve_ref(ref, git_dir, common_dir)


def _resolve_ref(ref: str, git_dir: Path, common_dir: Path) -> str | None:
    for directory in (git_dir, common_dir):
        loose = directory / ref
        if loose.is_file():
            value = _read(loose).strip()
            return value if _SHA.match(value) else None
    packed = common_dir / "packed-refs"
    if packed.is_file():
        for line in _read(packed).splitlines():
            sha, _, name = line.partition(" ")
            if name.strip() == ref and _SHA.match(sha):
                return sha
    # An unborn branch names a ref that has no commit yet.
    return None


def _remotes(config: Path) -> tuple[tuple[str, str], ...]:
    if not config.is_file():
        return ()
    found: dict[str, str] = {}
    current: str | None = None
    for raw in _read(config).splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            match = _REMOTE_SECTION.match(line)
            current = match.group(1) if match else None
            continue
        if current is not None and current not in found:
            match = _URL.match(line)
            if match:
                found[current] = match.group(1).strip()
    return tuple(sorted(found.items()))


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise GitMetaError(f"{path} could not be read: {exc}") from None
