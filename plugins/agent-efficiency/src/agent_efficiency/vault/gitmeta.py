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

Remote URLs are read as written in the config. ``url.<base>.insteadOf``
rewrites are not applied, so a remote written in a rewritten short form may
not normalize to a known repository, and that repository then matches by
path only.
"""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from pathlib import Path

from agent_efficiency.vault.root import VaultRootError, normalize_remote

# The section keyword and the key are case-insensitive in git; the remote
# name is not. A comment may follow the closing bracket.
_REMOTE_SECTION = re.compile(r'^\[\s*(?i:remote)\s+"([^"]+)"\s*\]\s*(?:[#;].*)?$')
_URL = re.compile(r"^url\s*=\s*(.+)$", re.IGNORECASE)
_COMMENT = re.compile(r"\s[;#].*$")
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class GitMetaError(OSError):
    """Raised when a git directory exists but its metadata cannot be read.

    ``root`` is the directory holding the ``.git`` marker that could not be
    read, or ``None`` when the failure came before any marker was found.
    """

    def __init__(self, message: str, root: Path | None = None) -> None:
        super().__init__(message)
        self.root = root


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
    """Return the nearest repository at or above ``start``, or ``None``.

    Every failure to read what is there is raised as ``GitMetaError``, so the
    caller handles one error type.
    """

    try:
        return _find(start)
    except GitMetaError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise GitMetaError(str(exc)) from None


def _find(start: Path) -> Repository | None:
    """Walk upward to the first ``.git`` marker.

    A marker is found with ``lstat``, so one that exists but cannot be
    followed still marks a repository. A directory whose marker cannot be
    checked, or whose marker cannot be followed or read, raises with that
    directory as the boundary. The walk never continues past it, because a
    parent repository must not be mistaken for the nearest one.
    """

    current = start.expanduser().resolve()
    for candidate in (current, *current.parents):
        marker = candidate / ".git"
        try:
            marker.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            raise GitMetaError(
                f"{marker} could not be checked: {exc}", root=candidate
            ) from None
        try:
            is_dir = stat.S_ISDIR(marker.stat().st_mode)
            git_dir = marker if is_dir else _gitdir_from_file(candidate, marker)
            return _load(candidate, git_dir)
        except (OSError, ValueError, RuntimeError) as exc:
            raise GitMetaError(str(exc), root=candidate) from None
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
                found[current] = _config_value(match.group(1))
    return tuple(sorted(found.items()))


def _config_value(raw: str) -> str:
    """Return a config value without its quotes or a trailing comment."""

    value = raw.strip()
    if value.startswith('"'):
        end = value.find('"', 1)
        return value[1:end] if end > 0 else value
    return _COMMENT.sub("", value).strip()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise GitMetaError(f"{path} could not be read: {exc}") from None
