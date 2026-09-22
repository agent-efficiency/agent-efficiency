"""Vault tree marker, root discovery, and git remote normalization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

from agent_efficiency.vault.schema import CLASSIFICATIONS

MARKER = ".vault.json"
MARKER_SCHEMA = 1

URL_SCHEMES = {"https": 443, "http": 80, "ssh": 22, "git": 22}


class VaultRootError(ValueError):
    """Raised when a vault tree marker is missing or malformed."""


@dataclass(frozen=True)
class VaultTree:
    root: Path
    classification: str
    preferred_remote: str
    remote_url: str

    @property
    def normalized_remote(self) -> str:
        return normalize_remote(self.remote_url)


def find_tree(start: Path) -> VaultTree:
    """Find the nearest vault tree at or above ``start``.

    Every failure, including an unreadable or malformed marker file, is raised
    as ``VaultRootError``.
    """
    current = start.expanduser().resolve()
    for candidate in (current, *current.parents):
        marker = candidate / MARKER
        if marker.is_file():
            return _load_marker(candidate, marker)
    raise VaultRootError(f"no {MARKER} found at or above {current}")


def _load_marker(root: Path, marker: Path) -> VaultTree:
    try:
        raw = marker.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise VaultRootError(f"{marker} is not valid UTF-8: {exc}") from None
    except OSError as exc:
        raise VaultRootError(f"{marker} could not be read: {exc}") from None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VaultRootError(f"{marker} is not valid JSON: {exc}") from None

    if not isinstance(data, dict):
        raise VaultRootError(f"{marker} must hold a JSON object")

    schema = data.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise VaultRootError(f"{marker} schema must be the integer {MARKER_SCHEMA}")
    if schema != MARKER_SCHEMA:
        raise VaultRootError(f"{marker} has unsupported schema {schema!r}")

    classification = data.get("classification")
    if not isinstance(classification, str) or classification not in CLASSIFICATIONS:
        raise VaultRootError(
            f"{marker} classification must be one of {', '.join(CLASSIFICATIONS)}"
        )

    preferred_remote = data.get("preferred_remote", "origin")
    if not isinstance(preferred_remote, str):
        raise VaultRootError(f"{marker} preferred_remote must be a string")

    remote_url = data.get("remote_url", "")
    if not isinstance(remote_url, str):
        raise VaultRootError(f"{marker} remote_url must be a string")

    return VaultTree(
        root=root,
        classification=classification,
        preferred_remote=preferred_remote,
        remote_url=remote_url,
    )


def normalize_remote(url: str) -> str:
    """Reduce a git remote URL to a canonical ``host/owner/name`` form.

    Equivalent spellings of the same repository produce the same result. The
    https, http, ssh, and git schemes are parsed as URLs, the SCP form
    ``user@host:path`` is accepted for any user name and with no user name, and
    a port that is the default for its scheme is dropped. On a hosted URL a
    trailing ``.git`` and surrounding ``/`` are removed.

    The host is lowercased. A path is never lowercased, so two local checkouts
    that differ only in case stay distinct. A local path, a relative path, and
    a ``file://`` URL keep their path as written, including a ``.git`` ending:
    on a filesystem ``/srv/vault.git`` and ``/srv/vault`` are two directories
    and may hold two different repositories. Only a trailing separator is
    dropped, because a directory is the same directory with or without one.

    Filesystem identity and hosted-URL identity are separate rules. Stripping
    ``.git`` is right for a hosted URL, where it is a suffix convention, and
    wrong for a path, where it is part of the name.

    The result is stable under repeated application: ``normalize_remote`` of an
    already normalized value returns that value unchanged.
    """
    text = url.strip()
    if not text:
        return ""

    scheme, separator, _ = text.partition("://")
    if separator:
        scheme = scheme.lower()
        if scheme in URL_SCHEMES:
            return _from_url(text, scheme)
        if scheme == "file":
            return _strip_slashes(_split(text).path)
        return _strip_git(text)

    host, colon, path = text.partition(":")
    if colon and not _looks_like_path(text):
        if "@" in host:
            host = host.split("@", 1)[1]
        if _is_port_prefixed(path):
            port, _, rest = path.partition("/")
            return _join(f"{host.lower()}:{port}", _strip_path(rest))
        return _join(host.lower(), _strip_path(path))

    return _strip_slashes(text)


def _looks_like_path(text: str) -> bool:
    return text.startswith(("/", ".", "~")) or "/" in text.partition(":")[0]


def _is_port_prefixed(path: str) -> bool:
    head = path.partition("/")[0]
    return head.isdigit()


def _from_url(text: str, scheme: str) -> str:
    parts = _split(text)
    try:
        host = (parts.hostname or "").lower()
    except ValueError:
        raise VaultRootError(f"remote {text!r} has an invalid host") from None
    try:
        port = parts.port
    except ValueError:
        raise VaultRootError(f"remote {text!r} has an invalid port") from None
    if port is not None and port != URL_SCHEMES[scheme]:
        host = f"{host}:{port}"
    return _join(host, _strip_path(parts.path))


def _split(text: str) -> SplitResult:
    """Parse a URL, reporting a malformed one as this module's own error."""

    try:
        return urlsplit(text)
    except ValueError:
        raise VaultRootError(
            f"remote {text!r} is not a URL this tool can read"
        ) from None


def _join(host: str, path: str) -> str:
    return f"{host}/{path}" if path else host


def _strip_slashes(value: str) -> str:
    path = value.strip()
    while path.endswith("/"):
        path = path[:-1]
    return path


def _strip_git(value: str) -> str:
    path = _strip_slashes(value)
    while path.endswith(".git"):
        path = path[:-4]
        while path.endswith("/"):
            path = path[:-1]
    return path


def _strip_path(value: str) -> str:
    path = _strip_git(value)
    return path.strip("/")
