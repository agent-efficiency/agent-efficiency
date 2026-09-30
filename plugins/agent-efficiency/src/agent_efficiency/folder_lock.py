"""The lock that every writer of a new database in a data folder holds.

A database comes into existence in a data folder in only two ways: a fresh
store is created, or an older store is copied in. Both hold this lock and
check again, under it, that no database exists yet. So a copy in progress can
never be replaced by a fresh store, and a fresh store can never be replaced
by a copy.

The lock is an operating system lock on a file in the data folder. It is
released when its holder exits, however it exits. The file itself stays;
removing it could let two processes hold two locks.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt

LOCK_NAME = ".plugin-data-copy.lock"
POLL_SECONDS = 0.02


class FolderBusy(Exception):
    """Another process held the data folder lock past the caller's deadline."""


@contextmanager
def folder_lock(folder: Path, deadline: float | None) -> Iterator[None]:
    """Hold the lock for ``folder``, waiting at most until ``deadline``.

    ``deadline`` is a ``time.monotonic()`` value, or None to wait as long as
    it takes.
    """

    descriptor = os.open(folder / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while not _try_lock(descriptor, blocking=deadline is None):
            if time.monotonic() >= deadline:  # type: ignore[operator]
                raise FolderBusy(f"another process is writing to {folder}")
            time.sleep(POLL_SECONDS)
        yield
    finally:
        # Closing the descriptor releases the lock.
        os.close(descriptor)


def _try_lock(descriptor: int, *, blocking: bool) -> bool:
    if fcntl is not None:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(descriptor, flags)
        except (BlockingIOError, InterruptedError):
            return False
        return True
    while True:  # pragma: no cover - Windows
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            if not blocking:
                return False
            time.sleep(POLL_SECONDS)
