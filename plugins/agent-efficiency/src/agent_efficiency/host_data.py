"""The one-time copy of a store that an earlier release kept in a host folder.

Releases before 0.2.1 let a host's plugin data variable choose the data
folder, so hooks under Claude Code and Codex wrote to the host's plugin data
folder while the terminal command used the default folder. Every entry point
now uses the default folder. To keep the history those hooks recorded, the
first run that finds no database in the data folder copies a host store into
it once.

The copy is safe when several hook processes start together. One process
holds a lock file while it copies, the database goes in through the SQLite
backup API, and every file is written under a temporary name and then linked
into place, which never replaces a file that is already there.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from agent_efficiency.paths import (
    create_private_file,
    data_dir,
    data_dir_is_chosen,
    make_private_dir,
)

DATABASE_NAME = "agent-efficiency.db"
COPY_RECORD = "plugin-data-copy.json"
LOCK_NAME = ".plugin-data-copy.lock"
LOCK_STALE_SECONDS = 60
WAIT_SECONDS = 1.0
POLL_SECONDS = 0.02
# Host variables that name a plugin data folder. They no longer choose the
# data folder; they only say where an older store may be.
HOST_DATA_VARIABLES = ("CLAUDE_PLUGIN_DATA", "PLUGIN_DATA")
SKIPPED_NAMES = {
    DATABASE_NAME,
    f"{DATABASE_NAME}-wal",
    f"{DATABASE_NAME}-shm",
    f"{DATABASE_NAME}-journal",
    COPY_RECORD,
    LOCK_NAME,
}


def settle_data_dir(
    explicit: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    discover: bool = False,
) -> Path:
    """Return the data folder, first copying a host store into it when due.

    A hook passes ``discover=False`` and copies only from the folder its host
    variables name. The terminal command passes ``discover=True`` so it also
    finds a store in the host plugin data folders, and it copies only into the
    default folder, never into one chosen with --data-dir or
    AGENT_EFFICIENCY_DATA. A failed copy never stops the caller.
    """

    env = os.environ if environ is None else environ
    target = data_dir(explicit, env)
    candidates = host_data_dirs(env)
    if discover:
        if data_dir_is_chosen(explicit, env):
            return target
        for folder in installed_host_data_dirs(env):
            if folder not in candidates:
                candidates.append(folder)
    try:
        adopt_host_store(target, env, candidates=candidates)
    except (OSError, sqlite3.Error, ValueError):
        pass
    return target


def host_data_dirs(environ: Mapping[str, str]) -> list[Path]:
    """Return the plugin data folders the host variables name, in order."""

    found: list[Path] = []
    for variable in HOST_DATA_VARIABLES:
        value = environ.get(variable)
        if not value:
            continue
        folder = Path(value).expanduser().resolve()
        if folder not in found:
            found.append(folder)
    return found


def installed_host_data_dirs(environ: Mapping[str, str]) -> list[Path]:
    """Return plugin data folders that Claude Code and Codex keep for this plugin.

    These are found without any host variable, so the terminal command and
    doctor can see a store that only hooks used to write.
    """

    home = Path(environ.get("HOME") or Path.home())
    claude = Path(environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    codex = Path(environ.get("CODEX_HOME") or home / ".codex")
    found: list[Path] = []
    for base in (claude / "plugins" / "data", codex / "plugins" / "data"):
        try:
            children = sorted(base.iterdir())
        except OSError:
            continue
        for child in children:
            if child.name.startswith("agent-efficiency") and child.is_dir():
                folder = child.expanduser().resolve()
                if folder not in found:
                    found.append(folder)
    return found


def adopt_host_store(
    target: Path,
    environ: Mapping[str, str],
    *,
    candidates: list[Path] | None = None,
) -> Path | None:
    """Copy a host store into ``target`` once. Return the folder copied, or None.

    Nothing happens when ``target`` already has a database, or when no
    candidate folder has one. ``candidates`` defaults to the folders the host
    variables name.
    """

    target = target.expanduser().resolve()
    database = target / DATABASE_NAME
    if database.exists():
        return None
    folders = host_data_dirs(environ) if candidates is None else candidates
    source = next(
        (
            folder
            for folder in folders
            if folder != target and (folder / DATABASE_NAME).is_file()
        ),
        None,
    )
    if source is None:
        return None
    make_private_dir(target)
    lock = target / LOCK_NAME
    if not _acquire(lock):
        _wait(database, lock)
        return None
    try:
        if database.exists():
            return None
        _copy_state_files(source, target)
        if not _copy_database(source / DATABASE_NAME, database):
            return None
        _write_record(target, source)
        return source
    finally:
        lock.unlink(missing_ok=True)


def copied_from(target: Path) -> Path | None:
    """Return the folder a store was copied from, if it was copied."""

    try:
        record = json.loads((target / COPY_RECORD).read_text(encoding="utf-8"))
        return Path(str(record["source"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def unused_host_stores(
    target: Path, environ: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Describe each host store that exists but is no longer read."""

    target = target.expanduser().resolve()
    source = copied_from(target)
    unused: list[dict[str, Any]] = []
    folders = host_data_dirs(environ)
    for folder in installed_host_data_dirs(environ):
        if folder not in folders:
            folders.append(folder)
    for folder in folders:
        if folder == target or not (folder / DATABASE_NAME).is_file():
            continue
        copied = folder == source
        unused.append(
            {
                "path": str(folder),
                "copied": copied,
                "status": (
                    f"copied into {target}; nothing reads this folder now"
                    if copied
                    else (
                        f"not copied, because {target} already had its own "
                        "data; nothing reads this folder now"
                    )
                ),
            }
        )
    return unused


def _acquire(lock: Path) -> bool:
    for _ in range(2):
        try:
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if not _clear_stale(lock):
                return False
            continue
        os.close(descriptor)
        return True
    return False


def _clear_stale(lock: Path) -> bool:
    """Remove a lock left by a process that stopped. Return True if removed."""

    try:
        age = time.time() - lock.stat().st_mtime
    except FileNotFoundError:
        return True
    if age < LOCK_STALE_SECONDS:
        return False
    lock.unlink(missing_ok=True)
    return True


def _wait(database: Path, lock: Path) -> None:
    """Give the process holding the lock a moment to finish its copy."""

    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if database.exists() or not lock.exists():
            return
        time.sleep(POLL_SECONDS)


def _copy_database(source: Path, destination: Path) -> bool:
    """Copy through the backup API, which includes changes still in the log."""

    staged = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    _remove_database_files(staged)
    create_private_file(staged)
    try:
        with (
            closing(sqlite3.connect(source, timeout=1.5)) as reader,
            closing(sqlite3.connect(staged)) as writer,
        ):
            reader.backup(writer)
        return _link_new(staged, destination)
    finally:
        _remove_database_files(staged)


def _copy_state_files(source: Path, target: Path) -> None:
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if (
            path.is_symlink()
            or not path.is_file()
            or relative.as_posix() in SKIPPED_NAMES
            or any(part.startswith(".") for part in relative.parts)
        ):
            continue
        destination = target / relative
        if destination.exists():
            continue
        make_private_dir(destination.parent)
        descriptor, staged = tempfile.mkstemp(
            dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(path.read_bytes())
            _link_new(Path(staged), destination)
        finally:
            Path(staged).unlink(missing_ok=True)


def _link_new(staged: Path, destination: Path) -> bool:
    """Put ``staged`` at ``destination`` unless a file is already there."""

    try:
        os.link(staged, destination)
    except FileExistsError:
        return False
    except OSError:
        # A file system without hard links. The lock makes this the only
        # writer, so check and rename.
        if destination.exists():
            return False
        os.replace(staged, destination)
    return True


def _write_record(target: Path, source: Path) -> None:
    record = {
        "source": str(source),
        "copied_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    descriptor, staged = tempfile.mkstemp(
        dir=target, prefix=f".{COPY_RECORD}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(record, indent=2) + "\n")
        os.replace(staged, target / COPY_RECORD)
    finally:
        Path(staged).unlink(missing_ok=True)


def _remove_database_files(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
