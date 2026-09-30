"""Whether Claude Code runs Agent Efficiency in one folder.

An install record says where the plugin files are. Whether Claude Code loads
them in a session is decided by the enabledPlugins entry in the settings it
reads for that session, the last file that names the plugin winning. This
follows what Claude Code 2.1.280 does, checked with live sessions:

1. the user settings, ``settings.json`` in the Claude Code config folder;
2. ``.claude/settings.json`` in the folder the session starts in. A file of
   that name in a parent folder or at the git root is not read;
3. ``.claude/settings.local.json`` in the session folder, when that folder is
   not where the local file is kept;
4. ``.claude/settings.local.json`` where Claude Code keeps the local file: the
   root of the main checkout of the git repository (for a worktree, the
   repository it belongs to), unless that root is the home folder or is not
   owned by the current user, in which case it is the session folder;
5. the managed settings, ``managed-settings.json`` and the ``.json`` files in
   ``managed-settings.d`` in the managed folder. Managed settings delivered by
   a server or device management are not files here and are not checked.

If no file names the plugin, Claude Code does not load it in that session.

Workspace trust is reported too. Claude Code keeps it in ``.claude.json``, as
``projects[<folder>].hasTrustDialogAccepted`` for the folder or a parent up
to the repository root. Claude Code 2.1.280 drops an untrusted folder's
project permission rules but still reads its enabledPlugins, so trust does not
change the result here.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

FOLDER_SCOPES = ("project", "local")
ENABLED = "enabled"
DISABLED = "installed but disabled in this folder"
NOT_ENABLED = "installed but not enabled in this folder"
# Where Claude Code looks for managed settings files on each platform.
MANAGED_DIRS = {
    "linux": Path("/etc/claude-code"),
    "darwin": Path("/Library/Application Support/ClaudeCode"),
    "win32": Path("C:/Program Files/ClaudeCode"),
}
NOT_CHECKED = (
    "managed settings delivered by a server or by device management are not "
    "checked here; they can also turn the plugin off"
)


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
    home: Path | None = None,
) -> dict[str, Any]:
    """Describe what Claude Code does with this plugin in ``folder``."""

    folder = folder.expanduser().resolve()
    repository = git_root(folder)
    local_root = local_settings_root(folder, home or Path.home())
    record = _applying_record(installs, folder, local_root)
    plugin = str(record["plugin"]) if record else _any_plugin(installs)
    enabled: bool | None = None
    decided_by: Path | None = None
    decided_scope: str | None = None
    for scope, path in settings_layers(folder, local_root, user_settings):
        value = _enabled_value(path, plugin)
        if value is not None:
            enabled, decided_by, decided_scope = value, path, scope
    status = {
        "folder": str(folder),
        "local_settings_folder": str(local_root),
        "plugin": plugin,
        "scope": record["scope"] if record else None,
        "install_path": record["path"] if record else None,
        "install_index": (
            next(index for index, item in enumerate(installs) if item is record)
            if record is not None
            else None
        ),
        "decided_by": str(decided_by) if decided_by else None,
        "trusted": is_trusted(folder, repository, trust_file),
        "trust_file": str(trust_file),
        "not_checked": NOT_CHECKED,
    }
    if enabled is True:
        status.update(status=ENABLED, ready=True, reason=None, fixes=[])
    elif enabled is False:
        status.update(
            status=DISABLED,
            ready=False,
            reason=f"{decided_by} sets {plugin} to false in enabledPlugins",
            fixes=_disabled_fixes(plugin, decided_scope, record, folder),
        )
    else:
        status.update(
            status=NOT_ENABLED,
            ready=False,
            reason=f"no settings file enables {plugin} in {folder}",
            fixes=[
                f"to use it in every folder, run: claude plugin install {plugin}",
                (
                    f"to use it only here, run this in {folder}: claude plugin "
                    f"install {plugin} --scope project"
                ),
            ],
        )
    return status


def settings_layers(
    folder: Path, local_root: Path, user_settings: Path
) -> list[tuple[str, Path]]:
    """Return the settings files Claude Code reads for a session, lowest first."""

    layers = [
        ("user", user_settings),
        ("project", folder / ".claude" / "settings.json"),
    ]
    if local_root != folder:
        layers.append(("local", folder / ".claude" / "settings.local.json"))
    layers.append(("local", local_root / ".claude" / "settings.local.json"))
    managed = MANAGED_DIRS.get(sys.platform)
    if managed is not None:
        layers.append(("managed", managed / "managed-settings.json"))
        try:
            drop_ins = sorted(
                path
                for path in (managed / "managed-settings.d").iterdir()
                if path.suffix == ".json" and not path.name.startswith(".")
            )
        except OSError:
            drop_ins = []
        layers.extend(("managed", path) for path in drop_ins)
    return layers


def local_settings_root(folder: Path, home: Path) -> Path:
    """Return the folder whose ``.claude/settings.local.json`` Claude Code keeps.

    That is the root of the main checkout, so every worktree shares one file,
    unless the root is the session folder, is the home folder, or is not owned
    by the current user. Then it is the session folder.
    """

    root = main_checkout_root(folder)
    if root is None or root == folder:
        return folder
    try:
        if root == home.resolve():
            return folder
    except OSError:
        return folder
    owner = getattr(os, "geteuid", None)
    if owner is None:
        return folder
    try:
        uid = owner()
        claude = root / ".claude"
        owned = (
            root.stat().st_uid == uid
            and (root / ".git").lstat().st_uid == uid
            and (not claude.exists() or claude.lstat().st_uid == uid)
        )
    except OSError:
        return folder
    return root if owned else folder


def main_checkout_root(folder: Path) -> Path | None:
    """Return the root of the main checkout of the repository holding ``folder``."""

    root = git_root(folder)
    if root is None:
        return None
    marker = root / ".git"
    if marker.is_dir():
        return root
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return root
    if not text.startswith("gitdir:"):
        return root
    git_dir = Path(text[len("gitdir:") :].strip())
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    try:
        common = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return root
    common_dir = (git_dir / common).resolve()
    return common_dir.parent if common_dir.name == ".git" else root


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
    installs: list[dict[str, Any]], folder: Path, local_root: Path
) -> dict[str, Any] | None:
    """Return the install whose files a session in ``folder`` most likely uses.

    Enablement comes from settings, not from the record, so this only names
    the copy: a project or local install made for this folder, then the user
    install, then any install.
    """

    for install in installs:
        project = install.get("project")
        if install.get("scope") in FOLDER_SCOPES and project:
            if Path(project).expanduser().resolve() in (folder, local_root):
                return install
    for install in installs:
        if install.get("scope") == "user":
            return install
    return installs[0] if installs else None


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
    plugin: str,
    scope: str | None,
    record: dict[str, Any] | None,
    folder: Path,
) -> list[str]:
    if scope == "managed":
        return [
            "managed settings turn it off; ask the administrator of this "
            "machine to allow it"
        ]
    fixes = [
        f"enable it again, in {folder}: claude plugin enable {plugin} "
        f"--scope {scope or 'user'}"
    ]
    if (
        scope in FOLDER_SCOPES
        and record is not None
        and record.get("scope") in FOLDER_SCOPES
    ):
        fixes.append(
            "or remove the folder install so the user install applies, in "
            f"{folder}: claude plugin uninstall {plugin} --scope "
            f"{record['scope']} --keep-data"
        )
    return fixes
