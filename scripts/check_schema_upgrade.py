#!/usr/bin/env python3
"""Rehearse schema 7 to 8 migration and rollback with synthetic data only."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = "plugins/agent-efficiency"
DATABASE = "agent-efficiency.db"

SEED = """
import sys
from agent_efficiency.store import Store
s = Store(sys.argv[1])
for session, host in [('fixture-claude', 'claude'), ('fixture-cursor', 'cursor')]:
    s.ensure_session(session, host=host, cwd='/fixture/project')
    s.start_turn(session, 'turn', prompt_chars=0, task_type='unknown', risk='normal')
    for event, outcome in [('PreToolUse', 'pending'), ('PostToolUseFailure', 'failure')]:
        s.record_event(session, 'turn', event_name=event, tool_name='Bash',
                       signature='fixture-digest', outcome=outcome,
                       count_tool=True, count_failure=outcome == 'failure')
s.record_vault_receipt('fixture-claude', cause='new', vault_revision='a' * 16,
                       payload_digest='b' * 16, notes_selected=1, head_chars=80,
                       chars_emitted=120, chars_omitted=0, disposition='delivered')
s.record_verification_receipt({
    'check_id': 'fixture-check', 'check_digest': 'sha256:' + '0' * 64,
    'workspace_digest': None, 'git_commit': None, 'dirty': False,
    'started_at': '2026-01-01T00:00:00Z', 'finished_at': '2026-01-01T00:00:01Z',
    'duration_ms': 1000, 'exit_code': 1, 'result': 'fail',
    'runner_version': 'fixture', 'schema_version': 1, 'reason_code': 'exit_nonzero',
}, project_key='fixture-project')
"""

OPEN_STORE = """
import sys
from agent_efficiency.store import Store
Store(sys.argv[1]).ensure_current_schema()
"""

NEW_ACTIVITY = """
import sys
from agent_efficiency.store import Store
s = Store(sys.argv[1])
s.ensure_session('fixture-after-upgrade', host='codex', cwd='/fixture/project')
s.start_turn('fixture-after-upgrade', 'turn', prompt_chars=0,
             task_type='unknown', risk='normal')
s.record_event('fixture-after-upgrade', 'turn', event_name='PostToolUse',
               tool_name='Read', signature='new-fixture-digest',
               outcome='success', count_tool=True)
"""


class RehearsalError(Exception):
    """A rehearsal requirement was not met."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RehearsalError(message)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def folder_digest(folder: Path, *, sidecars: bool = True) -> dict[str, dict[str, Any]]:
    """Include every retained file and its permissions in a folder comparison."""

    return {
        str(path.relative_to(folder)): {
            "sha256": digest(path),
            "mode": path.stat().st_mode & 0o777,
        }
        for path in sorted(folder.rglob("*"))
        if path.is_file()
        and (
            sidecars
            or str(path.relative_to(folder))
            not in {
                f"{name}{suffix}"
                for name in (DATABASE, f"{DATABASE}.schema-7.bak")
                for suffix in ("-shm", "-wal")
            }
        )
    }


def quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def snapshot(database: Path, reference: dict | None = None) -> dict[str, Any]:
    """Compare stored values in the original columns, including historical counts."""

    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        require(
            conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)],
            "database integrity check failed",
        )
        require(
            not conn.execute("PRAGMA foreign_key_check").fetchall(),
            "database foreign key check failed",
        )
        schema = conn.execute(
            "SELECT value FROM settings WHERE key = 'schema_version'"
        ).fetchone()[0]
        names = (
            reference["tables"]
            if reference
            else [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
        )
        tables = {}
        for name in names:
            columns = (
                reference["tables"][name]["columns"]
                if reference
                else [
                    row[1] for row in conn.execute(f"PRAGMA table_info({quoted(name)})")
                ]
            )
            sql = f"SELECT {', '.join(map(quoted, columns))} FROM {quoted(name)}"
            if name == "settings":
                sql += " WHERE key != 'schema_version'"
            rows = sorted(
                json.dumps(list(row), separators=(",", ":"))
                for row in conn.execute(sql)
            )
            tables[name] = {
                "columns": columns,
                "count": len(rows),
                "sha256": hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
            }
    return {"schema": str(schema), "tables": tables}


def data_state(folder: Path) -> dict[str, Any]:
    # Opening a WAL database can create or remove its coordination files.
    # Compare all logical records, including WAL data, and persistent files.
    return {
        "database": snapshot(folder / DATABASE),
        "files": folder_digest(folder, sidecars=False),
    }


class Runner:
    def __init__(self, repository: Path, artifacts: Path, timeout: float):
        self.repository = repository.resolve()
        self.artifacts = artifacts.absolute()
        if self.artifacts.resolve().is_relative_to(self.repository):
            raise ValueError("the artifact folder must be outside the checkout")
        # mkdir refuses existing folders, files, and links, including broken links.
        self.artifacts.mkdir(mode=0o700)
        self.home = self.artifacts / "home"
        self.home.mkdir(mode=0o700)
        (self.artifacts / "logs").mkdir(mode=0o700)
        self.timeout = timeout
        self.current: dict[str, Any] = {}
        self.report: dict[str, Any] = {
            "format_version": 1,
            "data_kind": "synthetic",
            "result": "running",
            "ok": False,
            "artifacts": str(self.artifacts),
            "started_at": self.now(),
            "runner_sha256": digest(Path(__file__)),
            "python_version": sys.version.split()[0],
            "timeout_seconds": timeout,
            "inputs": {},
            "snapshots": {},
            "steps": [],
        }
        self.save()

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def environment(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", os.defpath),
            "LANG": "C.UTF-8",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }

    def save(self) -> None:
        temporary = self.artifacts / "result.pending.json"
        temporary.write_text(json.dumps(self.report, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.artifacts / "result.json")

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        self.current = {"name": name, "status": "running", "started_at": self.now()}
        self.report["steps"].append(self.current)
        self.save()
        try:
            yield
        except (Exception, KeyboardInterrupt) as exc:
            state = (
                "incomplete"
                if isinstance(exc, (subprocess.TimeoutExpired, KeyboardInterrupt))
                else "failed"
            )
            error = (
                f"process timed out after {self.timeout} seconds"
                if isinstance(exc, subprocess.TimeoutExpired)
                else str(exc) or "interrupted"
            )
            self.current.update(status=state, error=error, finished_at=self.now())
            self.report.update(
                result=state, failed_step=name, error=error, finished_at=self.now()
            )
            self.save()
            raise
        else:
            self.current.update(status="passed", finished_at=self.now())
            self.save()

    def git(self, *args: str) -> bytes:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(self.repository), *args],
            env=self.environment(),
            capture_output=True,
            timeout=self.timeout,
            check=False,
        )
        require(
            result.returncode == 0,
            "git failed: " + result.stderr.decode(errors="replace").strip(),
        )
        return result.stdout

    def export(self, commit: str, label: str) -> Path:
        archive = self.artifacts / f"{label}.tar"
        archive.write_bytes(self.git("archive", "--format=tar", commit, PLUGIN))
        self.report["inputs"][f"{label}_archive_sha256"] = digest(archive)
        target = self.artifacts / f"{label}-source"
        target.mkdir(mode=0o700)
        # Explicit extraction also works on Python 3.11 versions before tar filters.
        with tarfile.open(archive) as tar:
            for member in tar:
                path = PurePosixPath(member.name)
                require(
                    not path.is_absolute() and ".." not in path.parts,
                    "archive contains an unsafe path",
                )
                destination = target.joinpath(*path.parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    require(member.isfile(), "archive contains a link or special file")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source = tar.extractfile(member)
                    require(source is not None, "archive file cannot be read")
                    with source, destination.open("xb") as output:
                        shutil.copyfileobj(source, output)
        return target

    def python(
        self,
        source: Path,
        data: Path,
        *,
        code: str | None = None,
        action: str | None = None,
        expected: int = 0,
    ) -> subprocess.CompletedProcess:
        env = self.environment()
        env["PYTHONPATH"] = str(source / PLUGIN / "src")
        args = (
            ["-c", code, str(data)]
            if code is not None
            else [
                "-m",
                "agent_efficiency",
                "--data-dir",
                str(data),
                "migrate",
                action,
                "--json",
            ]
        )
        prefix = f"logs/{len(self.report['steps']):02}-{self.current['name']}"
        self.current.update(stdout=f"{prefix}.stdout", stderr=f"{prefix}.stderr")
        self.save()

        def write_logs(stdout: str | bytes | None, stderr: str | bytes | None) -> None:
            for key, value in (("stdout", stdout), ("stderr", stderr)):
                if isinstance(value, bytes):
                    value = value.decode(errors="replace")
                (self.artifacts / self.current[key]).write_text(
                    value or "", encoding="utf-8"
                )

        try:
            result = subprocess.run(
                [sys.executable, "-B", "-S", *args],
                cwd=self.home,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            write_logs(exc.stdout, exc.stderr)
            raise
        write_logs(result.stdout, result.stderr)
        self.current["exit_code"] = result.returncode
        require(
            result.returncode == expected,
            f"process exited {result.returncode}; expected {expected}; see retained logs",
        )
        return result


def run_rehearsal(
    repository: Path, baseline_ref: str, artifacts: Path, *, timeout: float = 30
) -> dict[str, Any]:
    runner = Runner(repository, artifacts, timeout)
    report = runner.report
    snapshots = report["snapshots"]
    work = runner.artifacts
    try:
        with runner.step("resolve_inputs"):
            baseline = (
                runner.git(
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    f"{baseline_ref}^{{commit}}",
                )
                .decode()
                .strip()
            )
            candidate = (
                runner.git("rev-parse", "--verify", "HEAD^{commit}").decode().strip()
            )
            report["inputs"].update(
                baseline_commit=baseline, candidate_commit=candidate
            )
            dirty = runner.git(
                "status", "--porcelain", "--untracked-files=all", "--", PLUGIN
            )
            require(
                not dirty,
                "candidate plugin has uncommitted changes; commit them before rehearsing",
            )
        with runner.step("export_baseline"):
            old = runner.export(baseline, "baseline")
        with runner.step("export_candidate"):
            new = runner.export(candidate, "candidate")
        data, backup, upgraded, restored = (
            work / name for name in ("baseline-data", "backup", "upgraded", "restored")
        )
        with runner.step("seed_baseline"):
            runner.python(old, data, code=SEED)
            (data / "rehearsal-marker.json").write_text('{"synthetic": true}\n')
            before = snapshots["baseline"] = snapshot(data / DATABASE)
            require(before["schema"] == "7", "baseline must create schema 7")
            require(
                "resolves" not in before["tables"]["events"]["columns"],
                "baseline already has schema 8 event accounting",
            )
        with runner.step("copy_backup"):
            shutil.copytree(data, backup)
            shutil.copytree(backup, upgraded)
            backup_files = folder_digest(backup)
            report["backup_files"] = backup_files
            require(
                folder_digest(upgraded) == backup_files,
                "full folder copy differs from the backup",
            )
        with runner.step("migration_check"):
            untouched = data_state(upgraded)
            result = runner.python(new, upgraded, action="--check")
            status = json.loads(result.stdout)
            require(
                status["current_schema"] == "7"
                and status["target_schema"] == "8"
                and status["migration_required"] is True
                and status["supported"] is True
                and status["applied"] is False,
                "migration check did not identify schema 7 to 8",
            )
            require(
                data_state(upgraded) == untouched,
                "migration check changed the copied data",
            )
        with runner.step("migrate"):
            result = runner.python(new, upgraded, action="--apply")
            report["migration"] = json.loads(result.stdout)
            require(
                report["migration"].get("applied") is True, "migration was not applied"
            )
        with runner.step("verify_migration"):
            after = snapshots["migrated"] = snapshot(upgraded / DATABASE, before)
            require(after["schema"] == "8", "migration did not produce schema 8")
            require(
                after["tables"] == before["tables"],
                "migration changed historical records or receipts",
            )
            with closing(
                sqlite3.connect((upgraded / DATABASE).as_uri() + "?mode=ro", uri=True)
            ) as conn:
                require(
                    conn.execute(
                        "SELECT COUNT(*) FROM events WHERE resolves != 0"
                    ).fetchone()[0]
                    == 0,
                    "migration recalculated historical event outcomes",
                )
            automatic = upgraded / f"{DATABASE}.schema-7.bak"
            require(automatic.is_file(), "automatic schema backup is missing")
            require(
                snapshot(automatic) == before,
                "automatic backup differs from the original records",
            )
            report["automatic_backup_sha256"] = digest(automatic)
        with runner.step("repeat_migration"):
            unchanged = data_state(upgraded)
            runner.python(new, upgraded, action="--apply")
            require(
                data_state(upgraded) == unchanged,
                "repeated migration changed data or its backup",
            )
        with runner.step("reject_old_runtime"):
            unchanged = data_state(upgraded)
            result = runner.python(old, upgraded, code=OPEN_STORE, expected=1)
            require(
                "unsupported data schema version: 8" in result.stderr,
                "old runtime did not reject schema 8 for the expected reason",
            )
            require(
                data_state(upgraded) == unchanged,
                "old runtime changed the upgraded folder",
            )
        with runner.step("record_new_activity"):
            runner.python(new, upgraded, code=NEW_ACTIVITY)
            snapshots["upgraded"] = snapshot(upgraded / DATABASE, before)
            require(
                snapshots["upgraded"]["tables"]["sessions"]["count"]
                == before["tables"]["sessions"]["count"] + 1,
                "new activity was not recorded after migration",
            )
            upgraded_state = data_state(upgraded)
        with runner.step("restore_backup"):
            require(folder_digest(backup) == backup_files, "original backup changed")
            shutil.copytree(backup, restored)
            runner.python(old, restored, code=OPEN_STORE)
        with runner.step("verify_rollback"):
            snapshots["restored"] = snapshot(restored / DATABASE)
            require(
                snapshots["restored"] == before,
                "restored records differ from the baseline",
            )
            require(
                folder_digest(restored, sidecars=False)
                == folder_digest(backup, sidecars=False),
                "restored folder differs from the backup",
            )
            require(
                folder_digest(backup) == backup_files,
                "original backup changed during rollback",
            )
            require(
                data_state(upgraded) == upgraded_state,
                "rollback changed post-upgrade records",
            )
        report.update(ok=True, result="passed", finished_at=runner.now())
        runner.save()
    except (Exception, KeyboardInterrupt) as exc:
        # A receipt write can fail outside a step. Never report success when
        # the completed receipt could not be saved. The last saved state stays.
        if report["result"] in {"running", "passed"}:
            report.update(
                ok=False,
                result="incomplete",
                failed_step="save_result",
                error=str(exc) or "interrupted",
                finished_at=runner.now(),
            )
        return report
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline-ref",
        required=True,
        help="Local schema 7 source ref. No network fetch is performed.",
    )
    parser.add_argument(
        "--artifacts",
        required=True,
        type=Path,
        help="New output folder outside the checkout. Its parent must exist.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30,
        help="Maximum seconds per subprocess (default: 30).",
    )
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a positive, finite number")
    try:
        report = run_rehearsal(
            ROOT, args.baseline_ref, args.artifacts, timeout=args.timeout
        )
    except (OSError, ValueError) as exc:
        print(f"Rehearsal refused: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "result": report["result"],
                "ok": report["ok"],
                "receipt": str(Path(report["artifacts"]) / "result.json"),
            },
            indent=2,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
