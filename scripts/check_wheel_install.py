#!/usr/bin/env python3
"""Build a wheel, install it into a fresh virtual environment, and use it.

This is the install a person gets from ``pipx install`` or ``pip install`` of
the repository: the package alone, with no plugin folder beside it. Every
command runs with a clean environment and a temporary HOME, so nothing on the
machine running the check is read or written.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPY_IGNORE = shutil.ignore_patterns(
    ".git",
    ".venv",
    "venv",
    "build",
    "dist",
    "__pycache__",
    "*.pyc",
    "*.egg-info",
)
NOTE = """---
schema: 1
id: example
title: Example
type: feedback
classification: core
status: active
updated: 2026-09-29
hook: An example note.
---
Body.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-build-isolation",
        action="store_true",
        help="Build with the setuptools already installed, without the network.",
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as raw:
        work = Path(raw)
        wheel = build_wheel(work, isolated=not args.no_build_isolation)
        python = install(work, wheel)
        failures = exercise(work, python)
    if failures:
        print(f"Wheel install check failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("Wheel install check passed.")
    return 0


def build_wheel(work: Path, *, isolated: bool) -> Path:
    # Build from a copy, so the build leaves nothing in the checkout.
    source = work / "source"
    shutil.copytree(ROOT, source, ignore=COPY_IGNORE)
    wheels = work / "wheels"
    command = [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        "--quiet",
        "--no-deps",
        "--disable-pip-version-check",
    ]
    if not isolated:
        command.append("--no-build-isolation")
    run([*command, "--wheel-dir", str(wheels), str(source)], env=dict(os.environ))
    (wheel,) = wheels.glob("agent_efficiency-*.whl")
    print(f"built {wheel.name}")
    return wheel


def install(work: Path, wheel: Path) -> Path:
    environment = work / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / "bin" / "python"
    run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--no-deps",
            "--disable-pip-version-check",
            str(wheel),
        ],
        env=clean_environment(work),
    )
    return python


def exercise(work: Path, python: Path) -> list[str]:
    home = work / "home"
    project = home / "project"
    project.mkdir(parents=True)
    tree = home / "vault-core"
    command = [str(python.parent / "agent-efficiency")]
    steps = [
        ("doctor", [*command, "doctor"]),
        ("doctor --json", [*command, "doctor", "--json"]),
        ("knowledge status", [*command, "knowledge", "status"]),
        ("smoke-test claude", [*command, "smoke-test", "claude"]),
        ("smoke-test codex", [*command, "smoke-test", "codex"]),
        ("smoke-test cursor", [*command, "smoke-test", "cursor"]),
        ("status", [*command, "status"]),
        ("report", [*command, "report"]),
        (
            "vault init",
            [*command, "vault", "init", str(tree), "--classification", "core"],
        ),
        ("vault check", [*command, "vault", "check", str(tree)]),
    ]
    failures = []
    for name, arguments in steps:
        if name == "vault check":
            (tree / "feedback" / "example.md").write_text(NOTE, encoding="utf-8")
            run([*command, "vault", "index", str(tree)], env=clean_environment(work))
        completed = subprocess.run(
            arguments,
            cwd=project,
            env=clean_environment(work),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        output = completed.stdout + completed.stderr
        good = completed.returncode == 0 and "Traceback" not in output
        if name == "doctor --json" and good:
            good = json.loads(completed.stdout).get("ok") is True
        print(f"[{'ok' if good else 'FAILED'}] {name} (exit {completed.returncode})")
        print("    " + output.strip().replace("\n", "\n    "))
        if not good:
            failures.append(name)
    return failures


def clean_environment(work: Path) -> dict[str, str]:
    home = work / "home"
    home.mkdir(exist_ok=True)
    return {
        "HOME": str(home),
        "PATH": os.pathsep.join(("/usr/local/bin", "/usr/bin", "/bin")),
        "LANG": "C.UTF-8",
    }


def run(command: list[str], *, env: dict[str, str]) -> None:
    subprocess.run(command, env=env, check=True, timeout=600, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    raise SystemExit(main())
