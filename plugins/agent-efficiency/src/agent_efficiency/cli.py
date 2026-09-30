"""Command-line interface for modes, evidence, reports, and capability packs."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Sequence

from agent_efficiency import __version__
from agent_efficiency.adapters import ADAPTERS
from agent_efficiency.capability_pack import (
    capability_pack_digest,
    capability_pack_policies,
    load_bundled_capability_pack,
)
from agent_efficiency.capability_retrieval import (
    capability_status,
    explain_capability,
)
from agent_efficiency.hook import format_session_status
from agent_efficiency.claude_folder import claude_paths, folder_status
from agent_efficiency.host_data import (
    CopyPending,
    settle_data_dir,
    unused_host_stores,
)
from agent_efficiency.models import VALID_MODES
from agent_efficiency.paths import (
    PACKAGE_DIR,
    PLUGIN_ROOT,
    in_plugin_folder,
    is_private,
)
from agent_efficiency.policy import PolicyPack
from agent_efficiency.python_support import (
    MINIMUM_PYTHON,
    unsupported_python_message,
)
from agent_efficiency.report import (
    build_explanation,
    build_report,
    format_explanation,
    format_report,
)
from agent_efficiency.experiments import (
    ExperimentError,
    VALID_ACCEPTANCE_EVIDENCE,
    VALID_COHORTS,
    VALID_COMPLETION_EVIDENCE,
    VALID_FEEDBACK,
    VALID_INVALIDATION_REASONS,
    build_experiment_evaluation,
    enroll_session,
    experiment_session_status,
    invalidate_enrollment,
    record_card_feedback,
    record_outcome,
)
from agent_efficiency.statusline import process_statusline
from agent_efficiency.store import Store, project_identity
from agent_efficiency.vault import cli_commands as vault_commands
from agent_efficiency.verification.config import load_project_config
from agent_efficiency.verification.receipts import reason_text, receipt_state
from agent_efficiency.verification.runner import run_checks
from agent_efficiency.verification.state import workspace_state


CLAUDE_EVENTS = set(ADAPTERS["claude"].native_events)
CURSOR_EVENTS = set(ADAPTERS["cursor"].native_events)
CODEX_EVENTS = set(ADAPTERS["codex"].native_events)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-efficiency",
        description="Local verification and efficiency governance for coding agents.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--data-dir",
        help="Override persistent data directory (also AGENT_EFFICIENCY_DATA).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    vault_commands.register(subparsers)

    for mode_command in ("on", "off", "observe", "advise", "guard"):
        mode_parser = subparsers.add_parser(
            mode_command, help=f"Set the latest local session to {mode_command}."
        )
        mode_parser.add_argument("--session", help="Explicit session ID.")

    mode = subparsers.add_parser("mode", help="Set one session mode.")
    mode.add_argument("mode", choices=VALID_MODES)
    mode.add_argument("--session", help="Explicit session ID.")

    status = subparsers.add_parser("status", help="Show current session state.")
    status.add_argument("--session", help="Explicit session ID.")
    status.add_argument("--json", action="store_true")

    cursor = subparsers.add_parser("cursor", help="Inspect the latest Cursor session.")
    cursor_sub = cursor.add_subparsers(dest="cursor_command", required=True)
    cursor_status = cursor_sub.add_parser(
        "status", help="Show Cursor session signals and checkpoints."
    )
    cursor_status.add_argument("--session", help="Explicit session ID.")
    cursor_status.add_argument("--json", action="store_true")

    default = subparsers.add_parser("default", help="Set the mode for future sessions.")
    default.add_argument("mode", choices=VALID_MODES)

    subparsers.add_parser(
        "init", help="Create an agent-efficiency.toml verification contract."
    )

    check = subparsers.add_parser("check", help="Run a configured verification check.")
    selection = check.add_mutually_exclusive_group(required=True)
    selection.add_argument("check_id", nargs="?")
    selection.add_argument("--changed", action="store_true")

    evidence = subparsers.add_parser(
        "evidence", help="Inspect revision-bound verification receipts."
    )
    evidence_sub = evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_list = evidence_sub.add_parser("list")
    evidence_list.add_argument("--json", action="store_true")
    evidence_show = evidence_sub.add_parser("show")
    evidence_show.add_argument("receipt", type=int)
    evidence_show.add_argument("--json", action="store_true")

    report = subparsers.add_parser("report", help="Summarize observed signals.")
    report.add_argument("--days", type=int, default=30)
    report.add_argument("--session", help="Limit the report to one session ID.")
    report.add_argument("--json", action="store_true")

    explain = subparsers.add_parser(
        "explain", help="Explain a recorded intervention and its observed results."
    )
    explain.add_argument("target", choices=("last",))
    explain.add_argument("--session", help="Limit the lookup to one session ID.")
    explain.add_argument(
        "--rate",
        choices=("useful", "neutral", "false_or_unnecessary"),
        help="Record one immutable human judgment for this intervention.",
    )
    explain.add_argument("--json", action="store_true")

    subparsers.add_parser(
        "statusline", help="Read Claude status JSON on stdin and print one line."
    )

    doctor = subparsers.add_parser("doctor", help="Check local installation.")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument(
        "--cwd",
        help="Folder to check host enablement for. Defaults to the current folder.",
    )

    smoke = subparsers.add_parser(
        "smoke-test",
        help="Run fixture events through this package's hook command.",
    )
    smoke.add_argument("host", choices=("claude", "cursor", "codex"))
    smoke.add_argument("--json", action="store_true")

    migrate = subparsers.add_parser(
        "migrate", help="Inspect or apply the local database migration."
    )
    migration_action = migrate.add_mutually_exclusive_group(required=True)
    migration_action.add_argument("--check", action="store_true")
    migration_action.add_argument("--apply", action="store_true")
    migrate.add_argument("--json", action="store_true")

    experiment = subparsers.add_parser(
        "experiment",
        help="Run explicit observe-versus-advise accepted-outcome experiments.",
    )
    experiment_sub = experiment.add_subparsers(dest="experiment_command", required=True)

    enroll = experiment_sub.add_parser(
        "enroll",
        help="Immutably enroll one session in an observe or advise cohort.",
    )
    enroll.add_argument("--id", required=True, dest="experiment_id")
    enroll.add_argument("--cohort", required=True, choices=VALID_COHORTS)
    enroll.add_argument("--task-class", required=True)
    enroll.add_argument(
        "--task-set-digest",
        required=True,
        help="Lowercase sha256 digest of the preregistered task manifest.",
    )
    enroll.add_argument(
        "--profile",
        required=True,
        dest="agent_profile",
        help="Bounded label for the fixed effort and agent/tool configuration.",
    )
    blinding = enroll.add_mutually_exclusive_group(required=True)
    blinding.add_argument("--blinded", action="store_true")
    blinding.add_argument("--not-blinded", action="store_true")
    enroll.add_argument("--session", help="Explicit session ID.")
    enroll.add_argument("--json", action="store_true")

    experiment_status = experiment_sub.add_parser(
        "status", help="Show one session's enrollment and outcome."
    )
    experiment_status.add_argument("--session", help="Explicit session ID.")
    experiment_status.add_argument("--json", action="store_true")

    invalidate = experiment_sub.add_parser(
        "invalidate",
        help="Permanently exclude one enrolled session without deleting evidence.",
    )
    invalidate.add_argument("--session", help="Explicit session ID.")
    invalidate.add_argument(
        "--reason",
        required=True,
        choices=VALID_INVALIDATION_REASONS,
    )
    invalidate.add_argument("--json", action="store_true")

    outcome = experiment_sub.add_parser(
        "outcome",
        help="Record one immutable structured outcome for an enrolled session.",
    )
    outcome.add_argument("--session", help="Explicit session ID.")
    accepted = outcome.add_mutually_exclusive_group(required=True)
    accepted.add_argument("--accepted", action="store_true")
    accepted.add_argument("--not-accepted", action="store_true")
    outcome.add_argument(
        "--evidence",
        required=True,
        choices=VALID_ACCEPTANCE_EVIDENCE,
    )
    outcome.add_argument(
        "--evidence-digest",
        help="Lowercase sha256 reference required for non-none evidence.",
    )
    outcome.add_argument(
        "--completion-evidence",
        choices=VALID_COMPLETION_EVIDENCE,
        default="inconclusive",
        help="Evidence state at completion. Defaults to inconclusive.",
    )
    outcome.add_argument("--wall-minutes", required=True, type=float)
    outcome.add_argument("--corrections", type=int, default=0)
    outcome.add_argument("--review-minutes", type=float, default=0)
    outcome.add_argument("--escaped-defects", type=int, default=0)
    outcome.add_argument("--json", action="store_true")

    feedback = experiment_sub.add_parser(
        "rate",
        help="Rate an emitted or silently observed card once.",
    )
    feedback.add_argument("card_id")
    feedback.add_argument("rating", choices=VALID_FEEDBACK)
    feedback.add_argument("--session", help="Explicit session ID.")
    changed = feedback.add_mutually_exclusive_group(required=True)
    changed.add_argument("--changed-next-action", action="store_true")
    changed.add_argument("--did-not-change-next-action", action="store_true")
    feedback.add_argument("--json", action="store_true")

    evaluate = experiment_sub.add_parser(
        "evaluate",
        help="Compare exact matched strata with explicit claim gates.",
    )
    evaluate.add_argument("--id", dest="experiment_id")
    evaluate.add_argument("--days", type=int, default=30)
    evaluate.add_argument("--json", action="store_true")

    knowledge = subparsers.add_parser(
        "knowledge", help="Inspect bundled guidance and session pins."
    )
    knowledge_sub = knowledge.add_subparsers(dest="knowledge_command", required=True)

    knowledge_status = knowledge_sub.add_parser(
        "status", help="Show capability-pack, pin, and retrieval status."
    )
    knowledge_status.add_argument("--session", help="Explicit session ID.")
    knowledge_status.add_argument("--json", action="store_true")

    explain = knowledge_sub.add_parser(
        "explain", help="Explain one canonical capability card."
    )
    explain.add_argument("card_id")
    explain.add_argument("--session", help="Explicit session ID.")
    explain.add_argument("--json", action="store_true")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "statusline":
        return _statusline(args)
    if args.command == "init":
        return _init_project()
    if args.command == "smoke-test":
        return _smoke_test(args)
    if args.command == "vault":
        # No vault command reads the database, so dispatch before the store is
        # built. A git hook must still work when the data directory does not.
        return vault_commands.dispatch(args)
    try:
        store = Store(settle_data_dir(args.data_dir))
    except CopyPending as exc:
        if args.command == "doctor":
            folder = Path(args.cwd) if args.cwd else Path.cwd()
            result = _doctor(None, folder, root=exc.target, copy_error=str(exc))
            _emit(result if args.json else _format_doctor(result), args.json)
            return 1
        print(str(exc), file=sys.stderr)
        return 2
    if args.command in {"on", "off", "observe", "advise", "guard", "mode"}:
        return _mode(args, store)
    if args.command == "status":
        return _status(args, store)
    if args.command == "cursor":
        return _cursor_status(args, store)
    if args.command == "default":
        store.set_default_mode(args.mode)
        print(f"Default mode for future sessions: {args.mode}")
        return 0
    if args.command == "check":
        return _check_project(args, store)
    if args.command == "evidence":
        return _evidence(args, store)
    if args.command == "report":
        try:
            report = build_report(store, args.days, session_id=args.session)
        except ValueError as exc:
            result = {"ok": False, "error": str(exc)}
            _emit(result if args.json else _format_key_values(result), args.json)
            return 1
        _emit(report if args.json else format_report(report), args.json)
        return 0
    if args.command == "explain":
        if args.session and not store.get_session(args.session):
            result = {"ok": False, "error": f"no recorded session {args.session}"}
            _emit(result if args.json else _format_key_values(result), args.json)
            return 1
        explanation = build_explanation(store, session_id=args.session, cwd=Path.cwd())
        if args.rate:
            intervention = explanation.get("intervention")
            if not intervention:
                result = {"ok": False, "error": "no intervention is available"}
                _emit(result if args.json else _format_key_values(result), args.json)
                return 1
            try:
                store.record_intervention_feedback(int(intervention["id"]), args.rate)
            except ValueError as exc:
                result = {"ok": False, "error": str(exc)}
                _emit(result if args.json else _format_key_values(result), args.json)
                return 1
            explanation = build_explanation(
                store, session_id=args.session, cwd=Path.cwd()
            )
        # Nothing recorded yet is an answer, not an error.
        _emit(explanation if args.json else format_explanation(explanation), args.json)
        return 0
    if args.command == "doctor":
        result = _doctor(store, Path(args.cwd) if args.cwd else Path.cwd())
        _emit(result if args.json else _format_doctor(result), args.json)
        return 0 if result["ok"] else 1
    if args.command == "migrate":
        return _migrate(args, store)
    if args.command == "experiment":
        return _experiment(args, store)
    if args.command == "knowledge":
        return _knowledge(args, store)
    return 2


def _mode(args: argparse.Namespace, store: Store) -> int:
    selected = (
        args.mode
        if args.command == "mode"
        else "advise"
        if args.command == "on"
        else args.command
    )
    guard_policy_digest = None
    if selected == "guard":
        try:
            config = load_project_config(Path.cwd())
            if config.mode != "guard":
                raise ValueError(
                    'guard mode requires mode = "guard" in agent-efficiency.toml'
                )
            guard_policy_digest = config.digest
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    session = (
        store.get_session(args.session)
        if args.session
        else store.latest_session(str(Path.cwd()))
    )
    if not session:
        print(
            "No recorded session in this project. In an active agent chat, send "
            f"`$agent-efficiency {selected}`; use `agent-efficiency default "
            f"{selected}` for future sessions.",
            file=sys.stderr,
        )
        return 1
    invalidated = store.set_mode(
        str(session["session_id"]),
        selected,
        guard_policy_digest=guard_policy_digest,
    )
    suffix = (
        " The enrolled experiment session is now invalidated." if invalidated else ""
    )
    print(
        f"Agent Efficiency is {selected} for session {session['session_id']}.{suffix}"
    )
    return 0


def _status(args: argparse.Namespace, store: Store) -> int:
    session = (
        store.get_session(args.session)
        if args.session
        else store.latest_session(str(Path.cwd())) or store.latest_session()
    )
    if not session:
        value: Any = {
            "mode": store.default_mode(),
            "session": None,
            "message": "No session recorded yet.",
        }
        _emit(
            value
            if args.json
            else f"Default mode: {store.default_mode()}; no session recorded.",
            args.json,
        )
        return 0
    limitation = "an observed fetch does not prove remote freshness"
    if args.json:
        value = dict(session)
        value["fetch_observation_limit"] = limitation
        _emit(value, True)
    else:
        print(format_session_status(store, str(session["session_id"])))
        print(f"Fetch policy limit: {limitation}.")
    return 0


def _cursor_status(args: argparse.Namespace, store: Store) -> int:
    session = (
        store.get_session(args.session)
        if args.session
        else store.latest_session(str(Path.cwd()), host="cursor")
        or store.latest_session(host="cursor")
    )
    if not session:
        value: Any = {
            "host": "cursor",
            "session": None,
            "message": "No Cursor session recorded yet.",
        }
        _emit(
            value if args.json else "No Cursor session recorded yet.",
            args.json,
        )
        return 0
    value = {
        "host": "cursor",
        "session_id": session["session_id"],
        "mode": session["mode"],
        "project_name": session["project_name"],
        "turns": session["turn_count"],
        "tools": session["tool_count"],
        "failures": session["failure_count"],
        "edits": session["edit_count"],
        "validated": session["validation_count"],
        "nudges": session["nudge_count"],
        "repeated_actions": session["repeated_action_count"],
        "broad_scans": session["broad_scan_count"],
        "subagents": session["subagent_count"],
        "compactions": session["compaction_count"],
    }
    if args.json:
        _emit(value, True)
    else:
        print(
            f"Cursor session {value['session_id']} | mode {value['mode']} | "
            f"turns {value['turns']} | tools {value['tools']} | "
            f"failures {value['failures']}"
        )
        print(
            f"Edits {value['edits']} | validations {value['validated']} | "
            f"nudges {value['nudges']} | repeated actions "
            f"{value['repeated_actions']} | broad scans {value['broad_scans']}"
        )
    return 0


def _statusline(args: argparse.Namespace) -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("status input must be an object")
        store = Store(settle_data_dir(args.data_dir))
        print(process_statusline(payload, store))
    except Exception:
        print("AE unavailable")
    return 0


def _experiment(args: argparse.Namespace, store: Store) -> int:
    command = args.experiment_command
    try:
        if command == "evaluate":
            result = build_experiment_evaluation(
                store,
                experiment_id=args.experiment_id,
                days=args.days,
            )
        else:
            session = _selected_session(store, args.session)
            session_id = str(session["session_id"])
            if command == "enroll":
                result = enroll_session(
                    store,
                    session_id,
                    experiment_id=args.experiment_id,
                    cohort=args.cohort,
                    task_class=args.task_class,
                    task_set_digest=args.task_set_digest,
                    agent_profile=args.agent_profile,
                    blinded=bool(args.blinded),
                )
            elif command == "status":
                result = experiment_session_status(store, session_id)
            elif command == "invalidate":
                result = invalidate_enrollment(
                    store,
                    session_id,
                    reason=args.reason,
                )
            elif command == "outcome":
                result = record_outcome(
                    store,
                    session_id,
                    accepted=bool(args.accepted),
                    acceptance_evidence=args.evidence,
                    acceptance_evidence_digest=args.evidence_digest,
                    completion_evidence_state=args.completion_evidence,
                    wall_time_minutes=args.wall_minutes,
                    correction_turns=args.corrections,
                    human_review_minutes=args.review_minutes,
                    escaped_defects=args.escaped_defects,
                )
            elif command == "rate":
                result = record_card_feedback(
                    store,
                    session_id,
                    args.card_id,
                    rating=args.rating,
                    changed_next_action=bool(args.changed_next_action),
                )
            else:
                return 2
    except ExperimentError as exc:
        result = {"ok": False, "error": str(exc)}
        _emit(result if args.json else _format_key_values(result), args.json)
        return 1
    result["ok"] = True
    _emit(result if args.json else _format_key_values(result), args.json)
    return 0


def _selected_session(store: Store, session_id: str | None) -> dict[str, Any]:
    session = (
        store.get_session(session_id)
        if session_id
        else store.latest_session(str(Path.cwd())) or store.latest_session()
    )
    if not session:
        raise ExperimentError("no recorded session is available")
    return session


def _knowledge(args: argparse.Namespace, store: Store) -> int:
    command = args.knowledge_command
    if command == "status":
        session = (
            store.get_session(args.session)
            if args.session
            else store.latest_session(str(Path.cwd())) or store.latest_session()
        )
        session_id = str(session["session_id"]) if session else None
        result = capability_status(store, session_id)
        _emit(result if args.json else _format_knowledge_status(result), args.json)
        return 0
    if command == "explain":
        session = (
            store.get_session(args.session)
            if args.session
            else store.latest_session(str(Path.cwd())) or store.latest_session()
        )
        session_id = str(session["session_id"]) if session else None
        result = explain_capability(
            store,
            args.card_id,
            session_id=session_id,
        )
        _emit(result if args.json else _format_capability_explain(result), args.json)
        return 0
    return 2


def _init_project() -> int:
    path = Path.cwd() / "agent-efficiency.toml"
    if path.exists():
        print(f"Verification config already exists: {path}", file=sys.stderr)
        return 1
    path.write_text(
        """version = 1
mode = "advise"

[[checks]]
id = "unit"
command = ["python", "-m", "unittest", "discover", "-s", "tests"]
applies_to = ["**/*.py"]
required_for = ["code"]
timeout_seconds = 300
""",
        encoding="utf-8",
    )
    print(f"Created {path}")
    return 0


def _check_project(args: argparse.Namespace, store: Store) -> int:
    try:
        config = load_project_config(Path.cwd())
        if args.changed:
            checks = _checks_for_changed_files(config)
        else:
            check = config.get(str(args.check_id))
            if check is None:
                raise ValueError(f"unknown check: {args.check_id}")
            checks = [check]
        if not checks:
            print("No configured checks apply to the current changes.")
            return 0
        receipts = run_checks(config, checks, store)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for receipt in receipts:
        print(
            f"Agent Efficiency check {receipt['check_id']}: "
            f"{receipt['result']} (receipt {receipt['receipt_id']})"
        )
        reason = reason_text(receipt)
        if reason:
            print(f"  reason: {receipt.get('reason_code') or 'unknown'}: {reason}")
    return 0 if all(item["result"] == "pass" for item in receipts) else 1


def _checks_for_changed_files(config: Any) -> list[Any]:
    try:
        completed = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            cwd=config.root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError("cannot select changed checks outside a Git workspace")
    changed = [line[3:] for line in completed.stdout.splitlines() if len(line) > 3]
    selected = []
    for check in config.checks:
        if not check.applies_to or any(
            _path_matches(path, pattern)
            for path in changed
            for pattern in check.applies_to
        ):
            selected.append(check)
    return selected


def _path_matches(path: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern) or (
        pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:])
    )


def _evidence(args: argparse.Namespace, store: Store) -> int:
    if args.evidence_command == "show":
        receipt = store.verification_receipt(args.receipt)
        if receipt is None:
            result = {"ok": False, "error": "verification receipt not found"}
            _emit(result if args.json else result["error"], args.json)
            return 1
        receipt["reason_code"] = receipt.get("reason_code")
        receipt["reason"] = reason_text(receipt)
        _emit(receipt if args.json else _format_key_values(receipt), args.json)
        return 0
    try:
        config = load_project_config(Path.cwd())
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    project_key, _ = project_identity(str(config.root))
    state = workspace_state(config.root, config.digest)
    values = []
    for check in config.checks:
        receipts = store.verification_receipts(project_key, check_id=check.check_id)
        latest = receipts[0] if receipts else None
        values.append(
            {
                "check_id": check.check_id,
                "state": receipt_state(
                    latest,
                    check_digest=check.digest,
                    workspace_digest=state.digest,
                    max_age_seconds=check.max_age_seconds,
                ),
                "receipt": latest,
            }
        )
    _emit(values if args.json else _format_evidence(values), args.json)
    return 0


def _format_evidence(values: list[dict[str, Any]]) -> str:
    if not values:
        return "No verification checks are configured."
    lines = []
    for value in values:
        line = f"{value['check_id']}: {value['state']}"
        receipt = value["receipt"]
        if receipt and value["state"] == receipt.get("result"):
            reason = reason_text(receipt)
            if reason:
                line = f"{line}: {reason}"
        lines.append(line)
    return "\n".join(lines)


def _smoke_test(args: argparse.Namespace) -> int:
    if in_plugin_folder(PLUGIN_ROOT):
        # The same command the hosts run: the plugin's hook script, without
        # the site directory.
        root = PLUGIN_ROOT
        command = [sys.executable, "-S", str(root / "scripts" / "agent_efficiency_hook.py")]
        described = str(command[-1])
    else:
        # An installed package has no plugin folder and no hook script. Its
        # hook entry point runs the same dispatcher.
        root = PACKAGE_DIR
        command = [sys.executable, "-m", "agent_efficiency.hook_entry"]
        described = (
            "python -m agent_efficiency.hook_entry (this installed package has "
            "no plugin folder)"
        )
    with tempfile.TemporaryDirectory() as temporary:
        environment = dict(os.environ)
        environment["AGENT_EFFICIENCY_DATA"] = str(Path(temporary) / "data")
        for key in (
            "CLAUDE_PLUGIN_ROOT",
            "CLAUDE_PLUGIN_DATA",
            "PLUGIN_ROOT",
            "PLUGIN_DATA",
        ):
            environment.pop(key, None)
        if args.host == "claude":
            environment["CLAUDE_PLUGIN_ROOT"] = str(root)
            identity = {"session_id": "smoke-claude"}
            events = ("SessionStart", "UserPromptSubmit")
        elif args.host == "codex":
            environment["PLUGIN_ROOT"] = str(root)
            identity = {"session_id": "smoke-codex"}
            events = ("SessionStart", "UserPromptSubmit")
        else:
            identity = {
                "conversation_id": "smoke-cursor",
                "cursor_version": "smoke",
                "workspace_roots": [temporary],
            }
            events = ("sessionStart", "beforeSubmitPrompt")
        base = {**identity, "cwd": temporary}
        started = _run_smoke_event(
            command,
            {**base, "hook_event_name": events[0]},
            environment,
        )
        status = _run_smoke_event(
            command,
            {
                **base,
                "hook_event_name": events[1],
                "prompt": "$agent-efficiency status",
            },
            environment,
        )
    if args.host == "cursor":
        shape_valid = isinstance(status.get("user_message"), str)
    else:
        shape_valid = isinstance(status.get("stopReason"), str)
    result = {
        "ok": started[0] == 0 and status.get("returncode") == 0 and shape_valid,
        "host": args.host,
        # The hook command of the package this command runs from. It is not
        # necessarily the copy a host installed; doctor checks that copy.
        "command_under_test": described,
        "installed_copy": (
            "not tested here; run agent-efficiency doctor to check the copy "
            "each host installed"
        ),
        "session_event_exit": started[0],
        "native_response_shape_valid": shape_valid,
    }
    _emit(result if args.json else _format_key_values(result), args.json)
    return 0 if result["ok"] else 1


def _run_smoke_event(
    command: list[str],
    payload: dict[str, Any],
    environment: dict[str, str],
) -> tuple[int, dict[str, Any]] | dict[str, Any]:
    completed = subprocess.run(
        command,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=environment,
        timeout=10,
        check=False,
    )
    output: dict[str, Any] = {"returncode": completed.returncode}
    if completed.stdout.strip():
        parsed = json.loads(completed.stdout)
        if not isinstance(parsed, dict):
            raise ValueError("smoke test hook output must be an object")
        output.update(parsed)
    return (
        (completed.returncode, output)
        if payload["hook_event_name"] in {"SessionStart", "sessionStart"}
        else output
    )


def _migrate(args: argparse.Namespace, store: Store) -> int:
    result = store.migration_status()
    if args.apply:
        if not result["supported"]:
            _emit(result if args.json else _format_key_values(result), args.json)
            return 1
        store.ensure_current_schema()
        result = store.migration_status()
        result["applied"] = True
    else:
        result["applied"] = False
    _emit(result if args.json else _format_key_values(result), args.json)
    return 0 if result["supported"] else 1


HOST_SPECS = {
    "claude": (CLAUDE_EVENTS, "${CLAUDE_PLUGIN_ROOT}"),
    "cursor": (CURSOR_EVENTS, "${CURSOR_PLUGIN_ROOT}"),
    "codex": (CODEX_EVENTS, "${PLUGIN_ROOT}"),
}
HOST_NAMES = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor"}
PLUGIN_ID_PREFIX = "agent-efficiency@"
ALL_EFFECT_CAPABILITIES = {
    "add_context",
    "notify",
    "deny_action",
    "continue_turn",
    "replace_action",
}


def _plugin_folder_status(root: Path, host: str) -> dict[str, Any]:
    """Check one plugin folder as ``host`` loads it."""

    expected_events, root_variable = HOST_SPECS[host]
    manifest_path = root / f".{host}-plugin" / "plugin.json"
    expected_hooks = f"./hooks/{host}-hooks.json"
    hooks_path = root / "hooks" / f"{host}-hooks.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # The manifest must name this host's hook file. Without it, a host
        # falls back to the default hooks/hooks.json, which is not shipped.
        manifest_ready = (
            manifest.get("name") == "agent-efficiency"
            and manifest.get("hooks") == expected_hooks
        )
        version = manifest.get("version")
    except (OSError, ValueError, AttributeError):
        manifest_ready = False
        version = None
    try:
        document = json.loads(hooks_path.read_text(encoding="utf-8"))
        hooks = document.get("hooks")
        event_names = set(hooks) if isinstance(hooks, dict) else set()
        commands = hooks.values() if isinstance(hooks, dict) else ()
        command_text = json.dumps(list(commands))
        hooks_ready = event_names == expected_events and root_variable in command_text
    except (OSError, ValueError, AttributeError):
        event_names = set()
        hooks_ready = False
    not_ready_reasons: list[str] = []
    if not root.is_dir():
        not_ready_reasons.append(f"the plugin folder {root} does not exist")
    else:
        if not manifest_ready:
            not_ready_reasons.append(
                f"{manifest_path} must be named agent-efficiency and set hooks "
                f"to {expected_hooks}"
            )
        if not hooks_ready:
            not_ready_reasons.append(
                f"{hooks_path} must list the {host} events and use {root_variable}"
            )
        # Claude Code loads hooks/hooks.json in addition to the file its
        # manifest names. A copy left over from an older release runs there
        # with an empty plugin root and fails on every event. No release ships
        # the file, so its presence is always stale.
        stale = root / "hooks" / "hooks.json"
        if stale.exists():
            not_ready_reasons.append(
                f"remove the stale default hook file {stale}: the host runs it "
                "in addition to its own hook file, with an empty plugin root"
            )
    return {
        "version": version,
        "manifest_ready": manifest_ready,
        "hooks_ready": hooks_ready,
        "event_count": len(event_names),
        "ready": not not_ready_reasons,
        "not_ready_reasons": not_ready_reasons,
    }


def _host_package_status() -> dict[str, dict[str, Any]]:
    """Check the plugin files this command runs from, for every host.

    An installed wheel has no plugin folder, so there are no host files to
    check; each host then reports ready as None rather than failing.
    """

    in_plugin = in_plugin_folder(PLUGIN_ROOT)
    result: dict[str, dict[str, Any]] = {}
    for host in HOST_SPECS:
        if in_plugin:
            status = _plugin_folder_status(PLUGIN_ROOT, host)
            status.pop("version")
        else:
            status = {
                "manifest_ready": None,
                "hooks_ready": None,
                "event_count": 0,
                "ready": None,
                "not_ready_reasons": [],
            }
        supported_capabilities = sorted(
            {
                capability
                for capabilities in ADAPTERS[host].capabilities.values()
                for capability in capabilities
            }
        )
        result[host] = {
            "cli_version": _tool_version(host),
            **status,
            "supported_capabilities": supported_capabilities,
            "unsupported_capabilities": sorted(
                ALL_EFFECT_CAPABILITIES - set(supported_capabilities)
            ),
        }
    return result


def _installed_host_status(folder: Path) -> dict[str, dict[str, Any]]:
    """Check the plugin each host actually installed, not this package."""

    home = Path.home()
    claude = _claude_installs(
        Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    )
    if claude["installs"]:
        user_settings, trust_file = claude_paths(os.environ, home)
        here = folder_status(
            folder,
            claude["installs"],
            user_settings=user_settings,
            trust_file=trust_file,
            home=home,
        )
        claude["this_folder"] = here
        # Only the install used here and the user install, which every
        # folder falls back to, decide readiness. A broken install made for
        # another folder is shown as a warning.
        warnings: list[str] = []
        ready = bool(here["ready"])
        for index, install in enumerate(claude["installs"]):
            install["applies_here"] = index == here["install_index"]
            if install["ready"]:
                continue
            if install["applies_here"] or install["scope"] == "user":
                ready = False
            else:
                where = install.get("project") or install["path"]
                warnings.extend(
                    f"{install['scope']} install for {where}: {reason}"
                    for reason in install["not_ready_reasons"]
                )
        claude["ready"] = ready
        claude["status"] = "ready" if ready else "not ready"
        claude["warnings"] = warnings
    return {
        "claude": claude,
        "codex": _codex_installs(Path(os.environ.get("CODEX_HOME") or home / ".codex")),
        "cursor": _cursor_installs(home / ".cursor"),
    }


def _claude_installs(config: Path) -> dict[str, Any]:
    record_path = config / "plugins" / "installed_plugins.json"
    installs: list[dict[str, Any]] = []
    try:
        document = json.loads(record_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _host_summary(installs)
    except (OSError, ValueError) as exc:
        return _host_summary(
            installs, error=f"{record_path} could not be read: {exc}"
        )
    plugins = document.get("plugins") if isinstance(document, dict) else None
    for plugin_id, records in (plugins or {}).items():
        if not str(plugin_id).startswith(PLUGIN_ID_PREFIX):
            continue
        for record in records if isinstance(records, list) else [records]:
            if not isinstance(record, dict):
                continue
            path = Path(str(record.get("installPath") or ""))
            status = _plugin_folder_status(path, "claude")
            install = {
                "plugin": plugin_id,
                "scope": record.get("scope") or "user",
                "path": str(path),
                "version": record.get("version") or status["version"],
                "ready": status["ready"],
                "not_ready_reasons": status["not_ready_reasons"],
            }
            if record.get("projectPath"):
                install["project"] = str(record["projectPath"])
            installs.append(install)
    return _host_summary(installs)


def _codex_installs(codex_home: Path) -> dict[str, Any]:
    """Codex records an enabled plugin in config.toml and caches its files.

    The cache holds one folder per installed version. The newest folder is the
    one Codex loads, so that is the one checked.
    """

    config_path = codex_home / "config.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _host_summary([])
    except (OSError, ValueError) as exc:
        return _host_summary([], error=f"{config_path} could not be read: {exc}")
    plugins = config.get("plugins")
    installs: list[dict[str, Any]] = []
    disabled = False
    for plugin_id, settings in (plugins if isinstance(plugins, dict) else {}).items():
        if not str(plugin_id).startswith(PLUGIN_ID_PREFIX):
            continue
        if isinstance(settings, dict) and settings.get("enabled") is False:
            disabled = True
            continue
        name, _, marketplace = str(plugin_id).partition("@")
        cache = codex_home / "plugins" / "cache" / marketplace / name
        try:
            versions = [path for path in cache.iterdir() if path.is_dir()]
        except OSError:
            versions = []
        if not versions:
            installs.append(
                {
                    "plugin": plugin_id,
                    "scope": "user",
                    "path": str(cache),
                    "version": None,
                    "ready": False,
                    "not_ready_reasons": [
                        f"Codex lists {plugin_id} as enabled, but {cache} holds "
                        "no installed copy; reinstall the plugin"
                    ],
                }
            )
            continue
        active = max(versions, key=lambda path: path.stat().st_mtime)
        status = _plugin_folder_status(active, "codex")
        installs.append(
            {
                "plugin": plugin_id,
                "scope": "user",
                "path": str(active),
                "version": status["version"] or active.name,
                "ready": status["ready"],
                "not_ready_reasons": status["not_ready_reasons"],
            }
        )
    summary = _host_summary(installs)
    if disabled and not installs:
        summary.update(installed=True, status="disabled")
    return summary


def _cursor_installs(cursor_home: Path) -> dict[str, Any]:
    local = cursor_home / "plugins" / "local" / "agent-efficiency"
    if not local.exists() and not local.is_symlink():
        return _host_summary([])
    status = _plugin_folder_status(local, "cursor")
    return _host_summary(
        [
            {
                "plugin": "agent-efficiency (local)",
                "scope": "user",
                "path": str(local),
                "version": status["version"],
                "ready": status["ready"],
                "not_ready_reasons": status["not_ready_reasons"],
            }
        ]
    )


def _host_summary(
    installs: list[dict[str, Any]], *, error: str | None = None
) -> dict[str, Any]:
    if error:
        return {
            "installed": True,
            "ready": False,
            "status": "install record unreadable",
            "installs": [],
            "error": error,
        }
    if not installs:
        return {
            "installed": False,
            "ready": None,
            "status": "not installed",
            "installs": [],
        }
    ready = all(install["ready"] for install in installs)
    return {
        "installed": True,
        "ready": ready,
        "status": "ready" if ready else "not ready",
        "installs": installs,
    }


def _doctor(
    store: Store | None,
    folder: Path | None = None,
    *,
    root: Path | None = None,
    copy_error: str | None = None,
) -> dict[str, Any]:
    """Check the installation. ``store`` is None while an old store waits to
    be copied; doctor then reports why and creates nothing."""

    root = store.paths.root if store is not None else root
    assert root is not None
    database = root / "agent-efficiency.db"
    try:
        capability_pack = load_bundled_capability_pack()
        capability_pack_policies(capability_pack)
        embedded_capability_pack_ready = True
        embedded_capability_pack_error = None
        embedded_capability_pack_id = capability_pack["pack_id"]
        embedded_capability_pack_sequence = capability_pack["sequence"]
        embedded_capability_pack_card_count = capability_pack["card_count"]
        embedded_capability_pack_digest = capability_pack_digest(capability_pack)
    except (OSError, ValueError) as exc:
        embedded_capability_pack_ready = False
        embedded_capability_pack_error = str(exc)
        embedded_capability_pack_id = None
        embedded_capability_pack_sequence = None
        embedded_capability_pack_card_count = 0
        embedded_capability_pack_digest = None

    hosts = _host_package_status()
    installed = _installed_host_status(folder or Path.cwd())
    minimum_python = ".".join(str(part) for part in MINIMUM_PYTHON)
    hook_path, hook_version = _hook_python()
    hook_python_supported = hook_version is None or (
        unsupported_python_message(
            tuple(int(part) for part in hook_version.split(".")[:3])
        )
        is None
    )
    if hook_path is None:
        hook_python = (
            "python3 is not on PATH here; hooks run python3 from the host's PATH "
            f"and need Python {minimum_python} or newer"
        )
    elif hook_version is None:
        hook_python = f"{hook_path} did not report its version"
    elif hook_python_supported:
        hook_python = f"{hook_path} is Python {hook_version}, supported"
    else:
        hook_python = (
            f"{hook_path} is Python {hook_version}; hooks need Python "
            f"{minimum_python} or newer. Put a newer python3 on PATH."
        )
    data_dir_private = is_private(root) and is_private(database)
    checks: dict[str, Any] = {
        "runtime_version": __version__,
        "python": platform.python_version(),
        "python_minimum": minimum_python,
        "python_supported": unsupported_python_message(sys.version_info) is None,
        "hook_python": hook_python,
        "data_dir": str(root),
        "data_dir_writable": os.access(root, os.W_OK),
        "data_dir_private": data_dir_private,
        "data_dir_permissions": (
            "only you can read the data folder"
            if data_dir_private
            else (
                "other users can read the data folder. Agent Efficiency does "
                "not change the mode of a folder it did not create. To make it "
                f"private, run: chmod -R go-rwx {root}"
            )
        ),
        "data_copy": copy_error,
        "unused_data_dirs": unused_host_stores(
            root, os.environ, copy_error=copy_error
        ),
        "database": str(database),
        "database_ready": database.is_file(),
        "default_mode": store.default_mode() if store is not None else None,
        "active_policy_count": len(PolicyPack.load(root).ids()),
        "embedded_capability_pack_ready": embedded_capability_pack_ready,
        "embedded_capability_pack_error": embedded_capability_pack_error,
        "embedded_capability_pack_id": embedded_capability_pack_id,
        "embedded_capability_pack_sequence": embedded_capability_pack_sequence,
        "embedded_capability_pack_card_count": embedded_capability_pack_card_count,
        "embedded_capability_pack_digest": embedded_capability_pack_digest,
        "claude": hosts["claude"]["cli_version"],
        "cursor": hosts["cursor"]["cli_version"],
        "codex": hosts["codex"]["cli_version"],
        "package_root": str(PLUGIN_ROOT) if in_plugin_folder(PLUGIN_ROOT) else None,
        "hosts": hosts,
        "host_packages_ready": (
            all(host["ready"] for host in hosts.values())
            if in_plugin_folder(PLUGIN_ROOT)
            else None
        ),
        "installed_hosts": installed,
        "installed_hosts_ready": all(
            host["ready"] is not False for host in installed.values()
        ),
        "fetch_observation_limit": (
            "an observed fetch does not prove remote freshness"
        ),
        "privacy": "prompt bodies, raw commands, code, outputs, and transcripts are not stored",
    }
    checks["hook_python_supported"] = hook_python_supported
    checks["ok"] = bool(
        checks["python_supported"]
        and checks["hook_python_supported"]
        and checks["data_dir_writable"]
        and checks["database_ready"]
        and checks["active_policy_count"]
        and checks["embedded_capability_pack_ready"]
        and checks["host_packages_ready"] is not False
        and checks["installed_hosts_ready"]
    )
    return checks


def _hook_python() -> tuple[str | None, str | None]:
    """Return the python3 that hooks would find on this PATH, and its version."""

    executable = shutil.which("python3")
    if not executable:
        return None, None
    try:
        completed = subprocess.run(
            [
                executable,
                "-S",
                "-c",
                "import sys; print('.'.join(map(str, sys.version_info[:3])))",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return executable, None
    version = completed.stdout.strip()
    if completed.returncode != 0 or not all(
        part.isdigit() for part in version.split(".")
    ):
        return executable, None
    return executable, version


def _tool_version(command: str) -> str | None:
    executable = shutil.which(command)
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (completed.stdout or completed.stderr).strip().splitlines()
    return output[0] if output else None


# The keys the text form of doctor prints, in order. Every key here must be one
# that _doctor returns; a test holds that true.
DOCTOR_TEXT_KEYS = (
    "runtime_version",
    "python",
    "python_minimum",
    "hook_python",
    "data_dir",
    "data_dir_permissions",
    "data_copy",
    "unused_data_dirs",
    "database",
    "default_mode",
    "active_policy_count",
    "embedded_capability_pack_ready",
    "embedded_capability_pack_id",
    "embedded_capability_pack_sequence",
    "embedded_capability_pack_card_count",
    "embedded_capability_pack_digest",
    "claude",
    "cursor",
    "codex",
    "hosts",
    "installed_hosts",
    "fetch_observation_limit",
    "privacy",
)


def _format_doctor(result: dict[str, Any]) -> str:
    lines = [f"Agent Efficiency doctor: {'OK' if result['ok'] else 'FAILED'}"]
    for key in DOCTOR_TEXT_KEYS:
        if key not in result:
            continue
        value = result[key]
        if key == "hosts":
            lines.extend(_format_package(result.get("package_root"), value))
        elif key == "installed_hosts":
            lines.extend(_format_installed(value))
        elif key == "unused_data_dirs":
            if not value:
                lines.append(f"{key}: none")
            else:
                lines.append(f"{key}:")
                lines.extend(f"  {item['path']}: {item['status']}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _format_package(root: Any, hosts: dict[str, Any]) -> list[str]:
    if root is None:
        return [
            "Package files: none here. This is an installed Python package "
            "with no plugin folder, so there are no host files to check; the "
            "copy each host installed is checked below."
        ]
    ready = all(host["ready"] for host in hosts.values())
    lines = [f"Package files at {root}: {'ready' if ready else 'not ready'}"]
    for host, status in hosts.items():
        lines.extend(
            f"  {HOST_NAMES[host]}: {reason}" for reason in status["not_ready_reasons"]
        )
    return lines


def _format_installed(installed: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for host, summary in installed.items():
        name = HOST_NAMES[host]
        if summary.get("error"):
            lines.append(f"{name}: {summary['status']}: {summary['error']}")
            continue
        if not summary["installs"]:
            lines.append(f"{name}: {summary['status']}")
            continue
        for install in summary["installs"]:
            state = "ready" if install["ready"] else "not ready"
            lines.append(
                f"{name}: {state}, {install['version']} "
                f"({install['scope']} scope) at {install['path']}"
            )
            label = (
                "warning"
                if install.get("applies_here") is False
                and install["scope"] != "user"
                else "fix"
            )
            lines.extend(
                f"  {label}: {reason}" for reason in install["not_ready_reasons"]
            )
        here = summary.get("this_folder")
        if here:
            lines.extend(_format_folder(name, here))
    return lines


def _format_folder(name: str, here: dict[str, Any]) -> list[str]:
    trust = (
        "workspace trust is saved"
        if here["trusted"]
        else "workspace trust is not saved, so Claude Code ignores this folder's "
        "permission rules, but not its plugin settings"
    )
    if here["ready"]:
        return [
            f"{name} in {here['folder']}: enabled by {here['decided_by']}, "
            f"{here['scope']} scope install; {trust}",
            f"  note: {here['not_checked']}",
        ]
    return [
        f"{name} in {here['folder']}: {here['status']}: {here['reason']}",
        *(f"  fix: {fix}" for fix in here["fixes"]),
        f"  note: {here['not_checked']}",
    ]


def _format_key_values(value: dict[str, Any]) -> str:
    return "\n".join(f"{key}: {item}" for key, item in value.items())


def _format_knowledge_status(result: dict[str, Any]) -> str:
    pin = result["session_pin"]
    pin_text = (
        f"{pin['pack_id']} seq {pin['sequence']} {pin['pack_digest']}"
        if pin
        else "no recorded session pin"
    )
    cards = result["cards"]
    receipts = result["receipts"]
    verification = result["verification"]
    return "\n".join(
        (
            f"Guidance pack: {result['channel_state']}",
            (
                f"bundled: {result['pack_id']} seq {result['sequence']} "
                f"{result['digest']}"
            ),
            f"session pin: {pin_text}",
            f"cards: {cards['active']} active, {cards['expired']} expired",
            (
                f"session receipts: {receipts['emitted']} emitted, "
                f"{receipts['observed']} silently observed, "
                f"{receipts['suppressed']} suppressed, "
                f"{receipts['emitted_chars']} characters"
            ),
            (
                f"verification: {verification['method']}; "
                f"runtime network: {verification['runtime_network']}"
            ),
        )
    )


def _format_capability_explain(result: dict[str, Any]) -> str:
    principles = ", ".join(result["principles"])
    reviewers = ", ".join(result["review"]["reviewers"])
    applicability = json.dumps(
        result["applicability"], sort_keys=True, separators=(",", ":")
    )
    risks = "; ".join(result["risks"])
    return "\n".join(
        (
            f"{result['card_id']}: {result['title']}",
            f"directive: {result['directive']}",
            f"authority: {result['authority']}",
            f"why matched: {result['why_matched']}",
            f"principles: {principles}",
            f"reviewers: {reviewers}",
            f"review reason: {result['review']['reason']}",
            f"published: {result['published_at']}",
            f"expires: {result['expires_at']}",
            f"risks: {risks}",
            f"applicability: {applicability}",
            f"supersedes: {result['supersedes']}",
            (
                f"pack: {result['pack']['pack_id']} seq "
                f"{result['pack']['sequence']} {result['pack']['digest']}"
            ),
        )
    )


def _emit(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        print(value)
