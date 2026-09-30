"""The one-time copy of a store that an earlier release kept in a host folder.

Releases before 0.2.1 let a host's plugin data variable choose the data
folder, so hooks under Claude Code and Codex wrote to the host's plugin data
folder while the terminal command used the default folder. Every entry point
now uses the default folder. To keep the history those hooks recorded, a store
found in a host plugin data folder is copied into the default folder once.

The copy is pending while an old store exists and the default folder has no
database. The rules that keep old history from being lost:

* While a copy is pending, nothing creates a new database in the default
  folder. A caller that cannot finish the copy gets ``CopyPending`` and must
  not build a store; a hook then records nothing for that event, and the next
  event tries again.
* One process copies at a time, under the data folder lock in
  ``folder_lock``. Creating a fresh store takes the same lock, so neither can
  replace the other, even for a caller that cannot see the old store.
* A hook copies against a deadline well inside its time limit: a short wait
  for the lock, and a stepped SQLite backup that stops at the deadline. The
  terminal command has no deadline, so it can finish a copy hooks could not.
* Everything is written under a temporary name and then linked into place,
  which never replaces a file that is already there. The database goes last,
  so its presence means the copy is complete. Temporary files left by a
  copier that stopped are removed under the lock before the next attempt.

A folder named by --data-dir or AGENT_EFFICIENCY_DATA never receives a copy.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from agent_efficiency.folder_lock import LOCK_NAME, FolderBusy, folder_lock
from agent_efficiency.paths import (
    create_private_file,
    data_dir,
    data_dir_is_chosen,
    make_private_dir,
)

DATABASE_NAME = "agent-efficiency.db"
DATABASE_SIDECARS = ("-wal", "-shm", "-journal")
COPY_RECORD = "plugin-data-copy.json"
STAGING_SUFFIX = ".copy.tmp"
# A hook has two seconds. Starting Python takes a few hundred milliseconds,
# so the lock wait and the copy together get one second.
HOOK_COPY_SECONDS = 1.0
POLL_SECONDS = 0.02
BACKUP_PAGES = 256
# Host variables that name a plugin data folder. They no longer choose the
# data folder; they only say where an older store may be.
HOST_DATA_VARIABLES = ("CLAUDE_PLUGIN_DATA", "PLUGIN_DATA")
SKIPPED_NAMES = {
    DATABASE_NAME,
    *(f"{DATABASE_NAME}{suffix}" for suffix in DATABASE_SIDECARS),
    COPY_RECORD,
    LOCK_NAME,
}


class CopyPending(Exception):
    """An old store is waiting to be copied and the copy did not finish."""

    def __init__(self, source: Path, target: Path, reason: str) -> None:
        self.source = source
        self.target = target
        self.reason = reason
        super().__init__(
            f"Agent Efficiency could not finish copying the earlier data store "
            f"at {source} into {target}: {reason}. Nothing was changed. Run the "
            f"command again; if it keeps failing, move {source} aside to start "
            "with an empty store."
        )


class _DeadlinePassed(Exception):
    """The copy ran out of time. The caller turns this into CopyPending."""


def hook_deadline() -> float:
    """Return the deadline a hook copies against."""

    return time.monotonic() + HOOK_COPY_SECONDS


def settle_data_dir(
    explicit: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    deadline: float | None = None,
) -> Path:
    """Return the data folder, first finishing a pending copy into it.

    Raises ``CopyPending`` when an old store is waiting and the copy could not
    finish, so the caller never builds a new store over missing history. With
    no ``deadline`` the copy waits as long as it needs.
    """

    env = os.environ if environ is None else environ
    target = data_dir(explicit, env)
    if data_dir_is_chosen(explicit, env) or database_present(target / DATABASE_NAME):
        return target
    adopt_host_store(target, env, deadline=deadline)
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

    These are found without any host variable, so every entry point sees a
    store that only hooks of another host used to write.
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


def candidate_sources(target: Path, environ: Mapping[str, str]) -> list[Path]:
    """Return host folders holding a store, the host variables' folders first."""

    folders = host_data_dirs(environ)
    for folder in installed_host_data_dirs(environ):
        if folder not in folders:
            folders.append(folder)
    return [
        folder
        for folder in folders
        if folder != target and (folder / DATABASE_NAME).is_file()
    ]


def adopt_host_store(
    target: Path,
    environ: Mapping[str, str],
    *,
    candidates: list[Path] | None = None,
    deadline: float | None = None,
) -> Path | None:
    """Copy a host store into ``target`` once. Return the folder copied, or None.

    Nothing happens when ``target`` already has a database, or when no
    candidate folder has one. Raises ``CopyPending`` when a copy is due and
    did not finish.
    """

    target = target.expanduser().resolve()
    database = target / DATABASE_NAME
    if database_present(database):
        return None
    sources = (
        candidate_sources(target, environ)
        if candidates is None
        else [folder for folder in candidates if (folder / DATABASE_NAME).is_file()]
    )
    if not sources:
        return None
    source = sources[0]
    try:
        make_private_dir(target)
        with copy_lock(target, deadline):
            if database_present(database):
                return None
            # An empty file is a store whose creation stopped before it
            # wrote anything; it holds no history.
            database.unlink(missing_ok=True)
            _remove_staging(target)
            try:
                _copy_state_files(source, target, deadline)
                if not _copy_database(source / DATABASE_NAME, database, deadline):
                    return None
            finally:
                _remove_staging(target)
    except _DeadlinePassed:
        raise CopyPending(source, target, "it did not finish in time") from None
    except (OSError, sqlite3.Error) as exc:
        raise CopyPending(source, target, str(exc) or type(exc).__name__) from None
    try:
        _write_record(target, source)
    except OSError:
        # The record only dates the copy. Doctor compares the stores
        # themselves, so a missing record does not hide a finished copy.
        pass
    return source


@contextmanager
def copy_lock(target: Path, deadline: float | None) -> Iterator[None]:
    """Hold the data folder lock, turning a timeout into a passed deadline."""

    try:
        with folder_lock(target, deadline):
            yield
    except FolderBusy:
        raise _DeadlinePassed() from None


def _check(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise _DeadlinePassed()


def _copy_database(source: Path, destination: Path, deadline: float | None) -> bool:
    """Copy through the backup API, which includes changes still in the log.

    The backup runs in steps so a deadline can stop it between them, including
    while the source is locked by a writer.
    """

    _check(deadline)
    staged = destination.with_name(f".{destination.name}{STAGING_SUFFIX}")
    create_private_file(staged)

    def progress(status: int, remaining: int, total: int) -> None:
        _check(deadline)

    wait = 1.5 if deadline is None else max(0.0, deadline - time.monotonic())
    with (
        closing(sqlite3.connect(source, timeout=wait)) as reader,
        closing(sqlite3.connect(staged)) as writer,
    ):
        reader.backup(writer, pages=BACKUP_PAGES, progress=progress, sleep=POLL_SECONDS)
    _check(deadline)
    return _link_new(staged, destination)


def _copy_state_files(source: Path, target: Path, deadline: float | None) -> None:
    for path in sorted(source.rglob("*")):
        _check(deadline)
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
        staged = destination.with_name(f".{destination.name}{STAGING_SUFFIX}")
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(path.read_bytes())
        _link_new(staged, destination)


def _link_new(staged: Path, destination: Path) -> bool:
    """Put ``staged`` at ``destination`` unless a file is already there.

    A hard link fails when the name exists, so it never replaces anything. On
    a file system without hard links, the name is first claimed with an
    exclusive create, which also fails when the name exists, and only that
    claimed empty file is then replaced. Both run under the folder lock.
    """

    try:
        os.link(staged, destination)
        return True
    except FileExistsError:
        return False
    except OSError:
        pass
    try:
        claimed = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    os.close(claimed)
    os.replace(staged, destination)
    return True


def _remove_staging(target: Path) -> None:
    """Remove temporary files left by a copier that stopped. Hold the lock."""

    for path in target.rglob(f".*{STAGING_SUFFIX}*"):
        if path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)


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


def database_present(database: Path) -> bool:
    """Whether a store with content exists. An empty file is a stopped create."""

    try:
        return database.stat().st_size > 0
    except FileNotFoundError:
        return False


def copied_from(target: Path) -> Path | None:
    """Return the folder the record says a store was copied from."""

    try:
        record = json.loads((target / COPY_RECORD).read_text(encoding="utf-8"))
        return Path(str(record["source"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def unused_host_stores(
    target: Path,
    environ: Mapping[str, str],
    *,
    copy_error: str | None = None,
) -> list[dict[str, Any]]:
    """Describe each host store that exists but is no longer read.

    The description comes from the stores themselves: a host store counts as
    copied when every session it holds is also in the data folder.
    """

    target = target.expanduser().resolve()
    database = target / DATABASE_NAME
    recorded = copied_from(target)
    unused: list[dict[str, Any]] = []
    for folder in candidate_sources(target, environ):
        if not database.exists():
            reason = f": {copy_error}" if copy_error else ""
            unused.append(
                {
                    "path": str(folder),
                    "copied": False,
                    "status": f"not copied yet into {target}{reason}",
                }
            )
            continue
        try:
            total, missing = _missing_sessions(folder / DATABASE_NAME, database)
        except sqlite3.Error as exc:
            unused.append(
                {
                    "path": str(folder),
                    "copied": False,
                    "status": f"could not be compared with {target}: {exc}",
                }
            )
            continue
        if missing == 0:
            when = " as recorded" if recorded == folder else ""
            status = f"copied into {target}{when}; nothing reads this folder now"
        else:
            status = (
                f"not copied, because {target} already had its own data; it "
                f"lacks {missing} of the {total} sessions here, and nothing "
                "reads this folder now"
            )
        unused.append({"path": str(folder), "copied": missing == 0, "status": status})
    return unused


def _missing_sessions(source: Path, target: Path) -> tuple[int, int]:
    """Count the source's sessions and those the target does not hold."""

    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=1.0)) as old:
        sessions = {row[0] for row in old.execute("SELECT session_id FROM sessions")}
    with closing(sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=1.0)) as new:
        present = {row[0] for row in new.execute("SELECT session_id FROM sessions")}
    return len(sessions), len(sessions - present)
