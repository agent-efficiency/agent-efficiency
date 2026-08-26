#!/usr/bin/env python3
"""Build or verify the project-owned operational policy pack."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "plugins" / "agent-efficiency" / "policies" / "bundled" / "core.json"
REVIEWED_AT = "2026-08-26T00:00:00Z"
EXPIRES_AT = "2036-08-26T00:00:00Z"
SOURCE_URL = "https://github.com/agent-efficiency/agent-efficiency"


def policy(
    *,
    policy_id: str,
    title: str,
    event: str,
    kind: str,
    message: str,
    principle: str,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "id": policy_id,
        "title": title,
        "event": event,
        "kind": kind,
        **fields,
        "message": message,
        "status": "reviewed",
        "reviewed_by": "agent-efficiency maintainers",
        "reviewed_at": REVIEWED_AT,
        "expires_at": EXPIRES_AT,
        "constitutional_floor": True,
        "source": {
            "id": "agent-efficiency",
            "url": SOURCE_URL,
            "refs": [principle],
        },
    }


def build_pack() -> dict[str, Any]:
    policies = [
        policy(
            policy_id="core.root-cause",
            title="Distinguish causes before changing code",
            event="UserPromptSubmit",
            kind="task_guidance",
            task_types=["diagnose"],
            trigger_keywords=[],
            message=(
                "Diagnosis checkpoint: capture the exact failure state, name the "
                "leading cause, and choose one focused check that can distinguish "
                "it from the next plausible cause."
            ),
            principle="AE.DIAGNOSE.DISTINGUISH",
        ),
        policy(
            policy_id="core.break-failure-loop",
            title="Change the conditions after repeated failure",
            event="PostToolUse",
            kind="failure_loop",
            threshold=2,
            message=(
                "Efficiency signal: this action has failed repeatedly. Do not run "
                "the same action again until the input, environment, or hypothesis "
                "changes."
            ),
            principle="AE.FAILURE.CHANGE-INPUT",
        ),
        policy(
            policy_id="core.repeat-action",
            title="Require new evidence from a repeated action",
            event="PostToolUse",
            kind="repeat_action",
            threshold=3,
            message=(
                "Efficiency signal: the same action is repeating. State what new "
                "evidence another run can produce. Reuse the prior result or change "
                "the target when it cannot."
            ),
            principle="AE.REPEAT.NEW-EVIDENCE",
        ),
        policy(
            policy_id="core.narrow-search",
            title="Search the smallest useful scope",
            event="PostToolUse",
            kind="broad_scan",
            threshold=1,
            message=(
                "Efficiency signal: this scan covers more of the filesystem than "
                "the current question requires. Start with the repository or likely "
                "subsystem, then widen only if the focused search is insufficient."
            ),
            principle="AE.SEARCH.FOCUSED",
        ),
        policy(
            policy_id="core.repeat-install",
            title="Check environment state before reinstalling",
            event="PostToolUse",
            kind="repeat_class",
            threshold=2,
            command_class="install",
            message=(
                "Efficiency signal: dependency installation is repeating in this "
                "turn. Check the previous result and environment before another "
                "install."
            ),
            principle="AE.INSTALL.CHECK-STATE",
        ),
        policy(
            policy_id="core.subagent-sprawl",
            title="Give each subagent an independent question",
            event="PostToolUse",
            kind="subagent_count",
            threshold=4,
            message=(
                "Efficiency signal: subagent count is growing. Add another agent "
                "only for an independent, bounded question with a distinct output."
            ),
            principle="AE.DELEGATE.BOUNDED",
        ),
        policy(
            policy_id="core.verify-change",
            title="Verify changed behavior before completion",
            event="Stop",
            kind="unverified_change",
            threshold=1,
            message=(
                "Quality checkpoint: code changed, but no validation was observed. "
                "Run the smallest relevant behavior check, or state the concrete "
                "reason validation is blocked."
            ),
            principle="AE.VERIFY.CHANGED-BEHAVIOR",
        ),
        policy(
            policy_id="core.fetch-before-branch",
            title="Fetch before creating a branch",
            event="PreToolUse",
            kind="branch_without_observed_fetch",
            threshold=1,
            message=(
                "Repository checkpoint: no fetch was observed before branch "
                "creation. Fetch the intended remote and inspect the target branch. "
                "This observation does not prove the remote is current."
            ),
            principle="AE.GIT.FETCH-BEFORE-BRANCH",
        ),
        policy(
            policy_id="core.compact-failure-loop",
            title="Preserve unresolved failure state before compaction",
            event="PreCompact",
            kind="compaction_during_failure_loop",
            threshold=2,
            message=(
                "Context checkpoint: repeated failures are unresolved. Before "
                "compaction, preserve the failing state, current hypothesis, and "
                "next focused check."
            ),
            principle="AE.CONTEXT.PRESERVE-FAILURE",
        ),
    ]
    return {
        "schema_version": 1,
        "pack_id": "agent-efficiency-core",
        "generated_at": REVIEWED_AT,
        "policies": policies,
    }


def encoded_pack() -> bytes:
    return json.dumps(build_pack(), ensure_ascii=False, indent=2).encode("utf-8") + b"\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    payload = encoded_pack()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_bytes() != payload:
            raise SystemExit("bundled operational policy pack is stale")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
