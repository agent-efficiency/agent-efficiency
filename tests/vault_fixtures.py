"""Builders shared by the vault tests: git repositories, trees, and notes."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from agent_efficiency.vault.index import write_index
from agent_efficiency.vault.schema import load_note

# Isolate every git call from the developer's own configuration, which may
# rewrite identities or sign commits.
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Vault Test",
    "GIT_AUTHOR_EMAIL": "vault-test@example.invalid",
    "GIT_COMMITTER_NAME": "Vault Test",
    "GIT_COMMITTER_EMAIL": "vault-test@example.invalid",
}

TYPE_DIRS = {
    "project": "projects",
    "feedback": "feedback",
    "reference": "reference",
    "doctrine": "doctrine",
    "session": "sessions",
}


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_repo(
    path: Path,
    *,
    remotes: dict[str, str] | None = None,
    commit: bool = True,
    branch: str = "main",
) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    for name, url in (remotes or {}).items():
        git(path, "remote", "add", name, url)
    if commit:
        (path / "README").write_text("fixture\n", encoding="utf-8")
        git(path, "add", "README")
        git(path, "commit", "-q", "-m", "fixture")
    return path


def note_text(
    note_id: str,
    *,
    note_type: str = "project",
    classification: str = "private",
    status: str = "active",
    title: str | None = None,
    hook: str | None = None,
    repos: tuple[str, ...] = (),
    paths: tuple[str, ...] = (),
    branches: tuple[str, ...] = (),
    body: str = "Body.",
) -> str:
    lines = [
        "---",
        "schema: 1",
        f"id: {note_id}",
        f"title: {title or note_id + ' title'}",
        f"type: {note_type}",
        f"classification: {classification}",
        f"status: {status}",
        "updated: 2026-09-22",
        f"hook: {hook or note_id + ' hook'}",
    ]
    for key, values in (("repos", repos), ("paths", paths), ("branches", branches)):
        if values:
            lines.append(f"{key}: [{', '.join(values)}]")
    lines.append("---")
    return "\n".join(lines) + "\n" + body + "\n"


def make_vault_tree(
    root: Path, classification: str, notes: list[str], *, commit: bool = True
) -> Path:
    """Write a tree with its marker and notes, index it, and optionally commit."""

    root.mkdir(parents=True, exist_ok=True)
    (root / ".vault.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "classification": classification,
                "preferred_remote": "origin",
                "remote_url": f"git@example.invalid:owner/vault-{classification}.git",
            }
        ),
        encoding="utf-8",
    )
    for directory in TYPE_DIRS.values():
        (root / directory).mkdir(exist_ok=True)
    for text in notes:
        note = load_note(text)
        (root / TYPE_DIRS[note.type] / f"{note.id}.md").write_text(
            text, encoding="utf-8"
        )
    write_index(root)
    if commit:
        git(root, "init", "-q", "-b", "main")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "vault")
    return root


def recommit(root: Path) -> None:
    """Reindex a committed tree after a note edit and commit the change."""

    write_index(root)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "update")


def register_trees(data_root: Path, *trees: Path) -> Path:
    """Write the tree list directly, bypassing registration checks."""

    data_root.mkdir(parents=True, exist_ok=True)
    config = data_root / "vault.json"
    config.write_text(
        json.dumps({"schema": 1, "trees": [str(tree) for tree in trees]}),
        encoding="utf-8",
    )
    return config
