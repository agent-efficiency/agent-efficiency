"""Whether Claude Code runs Agent Efficiency in one folder.

An install record says where the plugin files are. Whether Claude Code loads
them in a folder is decided by settings, read in this order, the last file
that names the plugin winning:

1. the user settings, ``settings.json`` in the Claude Code config folder;
2. the folder's ``.claude/settings.json``;
3. the folder's ``.claude/settings.local.json``.

"The folder" here is the root of the git repository that holds it, or the
folder itself outside git. A project or local install record applies in its
own project folder and takes the place of the user record there. If no file
names the plugin, Claude Code does not load it in that folder.

Workspace trust is reported too. Claude Code keeps it in ``.claude.json``, as
``projects[<folder>].hasTrustDialogAccepted`` for the folder or a parent up
to the repository root. Claude Code 2.1.280 drops an untrusted folder's
project permission rules but still reads its enabledPlugins, so trust does not
change the result here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

FOLDER_SCOPES = ("project", "local")
ENABLED = "enabled"
DISABLED = "installed but disabled in this folder"
NOT_ENABLED = "installed but not enabled in this folder"


def claude_paths(environ: Mapping[str, str], home: Path) -> tuple[Path, Path]:
    """Return the user settings file and the file that records trust."""

    config = environ.get("CLAUDE_CONFIG_DIR")
    if config:
        return Path(config) / "settings.json", Path(config) / ".claude.json"
    return home / ".claude" / "settings.json", home / ".claude.json"


def folder_status(
    folder: Path,
    installs: list[dict[str, Any]],
    *,
    user_settings: Path,
    trust_file: Path,
) -> dict[str, Any]:
    """Describe what Claude Code does with this plugin in ``folder``."""

    folder = folder.expanduser().resolve()
    repository = git_root(folder)
    root = repository or folder
    record = _applying_record(installs, root)
    plugin = str(record["plugin"]) if record else _any_plugin(installs)
    layers = [
        ("user", user_settings),
        ("project", root / ".claude" / "settings.json"),
        ("local", root / ".claude" / "settings.local.json"),
    ]
    enabled: bool | None = None
    decided_by: Path | None = None
    decided_scope: str | None = None
    for scope, path in layers:
        value = _enabled_value(path, plugin)
        if value is not None:
            enabled, decided_by, decided_scope = value, path, scope
    status = {
        "folder": str(folder),
        "project_root": str(root),
        "plugin": plugin,
        "scope": record["scope"] if record else None,
        "install_path": record["path"] if record else None,
        "decided_by": str(decided_by) if decided_by else None,
        "trusted": is_trusted(folder, repository, trust_file),
        "trust_file": str(trust_file),
    }
    if enabled is True and record is not None:
        status.update(status=ENABLED, ready=True, reason=None, fixes=[])
    elif enabled is False:
        status.update(
            status=DISABLED,
            ready=False,
            reason=f"{decided_by} sets {plugin} to false in enabledPlugins",
            fixes=_disabled_fixes(plugin, decided_scope, record),
        )
    else:
        status.update(
            status=NOT_ENABLED,
            ready=False,
            reason=(
                f"no install applies to {root}"
                if enabled is True
                else f"no settings file enables {plugin} for {root}"
            ),
            fixes=[
                f"to use it in every folder, run: claude plugin install {plugin}",
                (
                    f"to use it only here, run this in {root}: claude plugin "
                    f"install {plugin} --scope project"
                ),
            ],
        )
    return status


def git_root(folder: Path) -> Path | None:
    """Return the root of the git repository that holds ``folder``."""

    for candidate in (folder, *folder.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def is_trusted(folder: Path, repository: Path | None, trust_file: Path) -> bool:
    """Whether Claude Code has saved workspace trust for this folder.

    Like Claude Code, this looks at the folder and each parent, stopping at
    the repository root inside git.
    """

    try:
        document = json.loads(trust_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    projects = document.get("projects") if isinstance(document, dict) else None
    if not isinstance(projects, dict):
        return False
    for candidate in (folder, *folder.parents):
        entry = projects.get(str(candidate))
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            return True
        if candidate == repository:
            break
    return False


def _applying_record(
    installs: list[dict[str, Any]], root: Path
) -> dict[str, Any] | None:
    for install in installs:
        project = install.get("project")
        if install.get("scope") in FOLDER_SCOPES and project:
            if Path(project).expanduser().resolve() == root:
                return install
    return next((item for item in installs if item.get("scope") == "user"), None)


def _any_plugin(installs: list[dict[str, Any]]) -> str:
    for install in installs:
        if install.get("plugin"):
            return str(install["plugin"])
    return "agent-efficiency@agent-efficiency"


def _enabled_value(path: Path, plugin: str) -> bool | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    plugins = document.get("enabledPlugins") if isinstance(document, dict) else None
    if not isinstance(plugins, dict) or plugin not in plugins:
        return None
    return plugins[plugin] is True


def _disabled_fixes(
    plugin: str, scope: str | None, record: dict[str, Any] | None
) -> list[str]:
    fixes = [
        f"enable it again, in this folder: claude plugin enable {plugin} "
        f"--scope {scope or 'user'}"
    ]
    if record is not None and record.get("scope") in FOLDER_SCOPES:
        fixes.append(
            "or remove the folder install so the user install applies, in "
            f"this folder: claude plugin uninstall {plugin} --scope "
            f"{record['scope']} --keep-data"
        )
    return fixes
