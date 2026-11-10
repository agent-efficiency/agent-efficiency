"""Keyed one-way digests for the fingerprints this tool stores.

The key belongs to this tool alone. It lives in the data folder as
``digest.key``: 64 lowercase hex characters and one newline, in a regular
file owned by the current user that no one else can read. The first use
creates it. The tool never replaces, repairs, or deletes a key file.

When the key is missing and cannot be created, or is not valid, digests use a
fixed built-in key instead. Fingerprints then still match each other, so call
pairing and repeat counts work the same, but they are no harder to guess than
an unkeyed hash. ``doctor`` reports which key is in use and why.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import os
import re
import secrets
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agent_efficiency.paths import PRIVATE_FILE_MODE, make_private_dir

KEY_NAME = "digest.key"
KEY_SIZE = 65
KEY_PATTERN = re.compile(rb"[0-9a-f]{64}\n")
LABEL_PREFIX = b"agent-efficiency/v1/"
CALL_LABEL = b"agent-efficiency/v1/call"
# Public by design. It is used only when the key file cannot be used, so the
# digests it makes are as guessable as an unkeyed hash, and no worse.
FALLBACK_KEY = b"agent-efficiency/v1/built-in-fallback-key"
VALID = "valid"
MISSING = "missing"
_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


@dataclass(frozen=True, slots=True)
class DigestKey:
    """The key to use, and the state of the key file it came from."""

    key: bytes
    state: str


def keyed_digest(key: bytes, label: bytes, value: bytes) -> str:
    """Return HMAC-SHA256 of ``label``, a zero byte, and ``value``, as hex."""

    if not label.startswith(LABEL_PREFIX):
        raise ValueError("a digest label must start with agent-efficiency/v1/")
    return hmac.new(key, label + b"\x00" + value, hashlib.sha256).hexdigest()


def read_key(root: str | Path) -> DigestKey:
    """Read the key file without creating it.

    The checks run on the open file, not the path, so a file swapped in after
    a check cannot pass it. A file that fails a check gives the fallback key
    and the reason.
    """

    path = Path(root) / KEY_NAME
    try:
        descriptor = os.open(path, _OPEN_FLAGS)
    except FileNotFoundError:
        return DigestKey(FALLBACK_KEY, MISSING)
    except OSError as exc:
        # A link refuses to open with O_NOFOLLOW; a socket refuses to open.
        if exc.errno in {errno.ELOOP, errno.EMLINK, errno.ENXIO}:
            return DigestKey(FALLBACK_KEY, "file type")
        return DigestKey(FALLBACK_KEY, "unreadable")
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return DigestKey(FALLBACK_KEY, "file type")
        # Where there is no effective user id, file owners and permission bits
        # do not work this way, so those two checks are skipped.
        geteuid = getattr(os, "geteuid", None)
        if geteuid is not None:
            if info.st_uid != geteuid():
                return DigestKey(FALLBACK_KEY, "owner")
            if info.st_mode & 0o077:
                return DigestKey(FALLBACK_KEY, "mode")
        if info.st_size != KEY_SIZE:
            return DigestKey(FALLBACK_KEY, "size")
        data = os.read(descriptor, KEY_SIZE + 1)
    except OSError:
        return DigestKey(FALLBACK_KEY, "unreadable")
    finally:
        os.close(descriptor)
    if not KEY_PATTERN.fullmatch(data):
        return DigestKey(FALLBACK_KEY, "content")
    return DigestKey(bytes.fromhex(data[:64].decode("ascii")), VALID)


def load_key(root: str | Path) -> bytes:
    """Return the key for this data folder, creating the key file on first use.

    This never raises. A key file that exists but is not valid is left as it
    is, and the fallback key is returned.
    """

    try:
        folder = Path(root)
        found = read_key(folder)
        if found.state != MISSING:
            return found.key
        try:
            _create_key(folder)
        except OSError:
            # A folder that cannot hold the key still gets working digests.
            pass
        return read_key(folder).key
    except Exception:
        # Hooks must keep working, so any other failure also gives the
        # fallback key.
        return FALLBACK_KEY


def _create_key(folder: Path) -> None:
    """Publish a new key file unless one appears first.

    The key is written to a private temporary file and then hard linked into
    place. A link never replaces an existing name, so the process that loses
    a race keeps the winner's file, and a reader never sees a partial key.
    """

    make_private_dir(folder)
    target = folder / KEY_NAME
    descriptor, staged = tempfile.mkstemp(
        dir=folder, prefix=f".{KEY_NAME}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(secrets.token_hex(32).encode("ascii") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(staged, target)
        except FileExistsError:
            return
        except OSError:
            # Without hard links, claim the name with an exclusive create.
            _write_exclusive(target, Path(staged).read_bytes())
    finally:
        Path(staged).unlink(missing_ok=True)


def _write_exclusive(target: Path, data: bytes) -> None:
    # This writes the key in place. On a file system without hard links, a
    # reader can see a partial file for a moment. That reader then uses the
    # built-in fallback key for that one hook.
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            PRIVATE_FILE_MODE,
        )
    except FileExistsError:
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
