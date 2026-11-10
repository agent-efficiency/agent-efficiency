"""Read host settings as untrusted input, and find settings that split the store.

Hooks, the status line, and the terminal command share one store only when
they use one data folder. A Claude Code settings file can send writes
elsewhere in two ways: ``env.AGENT_EFFICIENCY_DATA``, which hooks and the
status line inherit, and a ``--data-dir`` argument or ``AGENT_EFFICIENCY_DATA=``
prefix in ``statusLine.command``. ``doctor`` names every such setting that
points away from its own data folder. It never edits a settings file.

Settings files are untrusted. A file is read only when it resolves to a place
inside its own folder, is a regular file, and is at most
``MAX_SETTINGS_BYTES`` long. A file that fails those checks or is not a JSON
object is reported as one that could not be read.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent_efficiency.claude_folder import local_settings_root

MAX_SETTINGS_BYTES = 1_048_576
DATA_VARIABLE = "AGENT_EFFICIENCY_DATA"
TOOL_COMMANDS = {"agent-efficiency", "agent_efficiency", "agent_efficiency_cli.py"}
_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
OTHER_HOSTS_NOT_CHECKED = {
    "codex": (
        "not checked: this check does not know whether Codex settings can set "
        "the data folder for its hooks"
    ),
    "cursor": (
        "not checked: this check does not know whether Cursor settings can set "
        "the data folder for its hooks"
    ),
}
SHELL_NOT_CHECKED = (
    "a data folder set only in the shell that starts the host is not seen here"
)


def read_settings(path: Path, base: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Return a settings file's JSON object, or None and why it was not read.

    ``(None, None)`` means there is no such file.
    """

    try:
        target = path.resolve(strict=True)
    except FileNotFoundError:
        return None, None
    except (OSError, RuntimeError):
        return None, "could not be read"
    inside = base.resolve()
    if inside not in target.parents:
        return None, f"could not be read: it is a link to a file outside {base}"
    try:
        descriptor = os.open(target, _OPEN_FLAGS)
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, "could not be read"
    chunks: list[bytes] = []
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None, "could not be read: it is not a regular file"
        remaining = MAX_SETTINGS_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 65_536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return None, "could not be read"
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if len(data) > MAX_SETTINGS_BYTES:
        return None, f"could not be read: it is larger than {MAX_SETTINGS_BYTES} bytes"
    try:
        document = json.loads(data.decode("utf-8"))
    except ValueError:
        return None, "could not be read: it is not valid JSON"
    if not isinstance(document, dict):
        return None, "could not be read: it is not a JSON object"
    return document, None


def claude_config_folders(
    environ: Mapping[str, str], home: Path, extra: Sequence[str | Path] = ()
) -> list[Path]:
    """Return each Claude Code config folder to check, once, in order.

    That is the folder ``CLAUDE_CONFIG_DIR`` names, ``~/.claude``, and each
    folder given to doctor. Other profile folders are not searched for.
    """

    folders: list[Path] = []
    for value in (environ.get("CLAUDE_CONFIG_DIR"), home / ".claude", *extra):
        if not value:
            continue
        folder = Path(value).expanduser().resolve()
        if folder not in folders:
            folders.append(folder)
    return folders


def project_settings_files(folder: Path, home: Path) -> list[Path]:
    """Return the project and local settings files Claude Code reads in ``folder``.

    The order and the place of the local file follow ``claude_folder``.
    """

    folder = folder.expanduser().resolve()
    local_root = local_settings_root(folder, home)
    files = [folder / ".claude" / "settings.json"]
    if local_root != folder:
        files.append(folder / ".claude" / "settings.local.json")
    files.append(local_root / ".claude" / "settings.local.json")
    return files


def data_folder_settings(
    root: Path, config_folders: Sequence[Path], folder: Path, home: Path
) -> dict[str, Any]:
    """Report each setting that sends this tool's writes away from ``root``."""

    root = root.expanduser().resolve()
    candidates = [(config / "settings.json", config) for config in config_folders]
    candidates += [
        (path, path.parent.parent) for path in project_settings_files(folder, home)
    ]
    checked: list[str] = []
    findings: list[dict[str, str]] = []
    problems: list[dict[str, str]] = []
    for path, base in candidates:
        document, problem = read_settings(path, base)
        if problem:
            problems.append({"file": str(path), "status": problem})
            continue
        if document is None:
            continue
        checked.append(str(path))
        for setting, value, fix in _data_settings(document):
            target = _folder_named(value, home)
            if target is None:
                problems.append(
                    {
                        "file": str(path),
                        "status": (
                            f"{setting} does not name a folder doctor can "
                            "resolve, so it was not checked"
                        ),
                    }
                )
            elif target != root:
                findings.append(
                    {
                        "file": str(path),
                        "setting": setting,
                        "folder": str(target),
                        "fix": fix.format(file=path, root=root),
                    }
                )
    if findings:
        summary = (
            f"{len(findings)} setting(s) send writes to a data folder other than "
            f"{root}, so hooks, the status line, and doctor do not share one store"
        )
    else:
        summary = f"no checked setting sends writes to a folder other than {root}"
    return {
        "ok": not findings,
        "data_dir": str(root),
        "summary": summary,
        "checked": checked,
        "findings": findings,
        "problems": problems,
        "not_checked": {**OTHER_HOSTS_NOT_CHECKED, "shell": SHELL_NOT_CHECKED},
    }


def _data_settings(document: dict[str, Any]) -> list[tuple[str, Any, str]]:
    """Return (setting, value, fix template) for each data folder setting."""

    found: list[tuple[str, Any, str]] = []
    environment = document.get("env")
    if isinstance(environment, dict) and DATA_VARIABLE in environment:
        found.append(
            (
                f"env.{DATA_VARIABLE}",
                environment[DATA_VARIABLE],
                f"change env.{DATA_VARIABLE} in {{file}} to {{root}}",
            )
        )
    status_line = document.get("statusLine")
    command = status_line.get("command") if isinstance(status_line, dict) else None
    if isinstance(command, str):
        found.extend(_status_line_settings(command))
    return found


def _status_line_settings(command: str) -> list[tuple[str, Any, str]]:
    try:
        words = shlex.split(command)
    except ValueError:
        words = []
    if not any(Path(word).name in TOOL_COMMANDS for word in words):
        if "agent-efficiency" in command or "agent_efficiency" in command:
            # The command runs this tool in a form this check cannot read.
            return [("statusLine.command", None, "")]
        return []
    found: list[tuple[str, Any, str]] = []
    for index, word in enumerate(words):
        if word.startswith(f"{DATA_VARIABLE}="):
            found.append(
                (
                    f"statusLine.command {DATA_VARIABLE}= prefix",
                    word.partition("=")[2],
                    f"change the {DATA_VARIABLE}= value in statusLine.command "
                    "in {file} to {root}",
                )
            )
        elif word == "--data-dir" or word.startswith("--data-dir="):
            value = (
                word.partition("=")[2]
                if "=" in word
                else (words[index + 1] if index + 1 < len(words) else None)
            )
            found.append(
                (
                    "statusLine.command --data-dir",
                    value,
                    "change the --data-dir value in statusLine.command in {file} "
                    "to {root}",
                )
            )
    return found


def _folder_named(value: Any, home: Path) -> Path | None:
    """Return the absolute folder a setting names, or None when it is unclear.

    ``~`` and ``$HOME`` are read as the home folder. Any other variable, a
    relative path, or a value that is not text cannot be resolved here.
    """

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.replace("${HOME}", str(home)).replace("$HOME", str(home))
    if "$" in text:
        return None
    if text == "~" or text.startswith("~/"):
        text = str(home) + text[1:]
    path = Path(text)
    if not path.is_absolute():
        return None
    return path.resolve()
