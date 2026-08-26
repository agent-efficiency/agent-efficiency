#!/usr/bin/env python3
"""Build or verify the project-owned bundled guidance pack."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = (
    ROOT
    / "plugins"
    / "agent-efficiency"
    / "capabilities"
    / "bundled"
    / "base-pack.json"
)
PUBLISHED_AT = "2026-08-26T00:00:00Z"
EXPIRES_AT = "2036-08-26T00:00:00Z"


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def digest(value: Any) -> str:
    payload = canonical_bytes(value)[:-1]
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def card(
    *,
    card_id: str,
    title: str,
    directive: str,
    rationale: str,
    tasks: list[str],
    terms: list[str],
    negative_terms: list[str],
    principles: list[str],
    risks: list[str],
    measurement: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": card_id,
        "revision": 1,
        "title": title,
        "authority": "advisory",
        "directive": directive,
        "rationale": rationale,
        "recommendation": "adopt",
        "applies_to": {
            "tasks": tasks,
            "lifecycle_events": ["UserPromptSubmit"],
            "hosts": ["claude", "codex", "cursor"],
            "languages": [],
            "frameworks": [],
            "host_versions": [],
            "project_traits": [],
            "requires": [],
            "excludes": ["documentation only", "mechanical edit", "typo"],
        },
        "triggers": {
            "terms": terms,
            "negative_terms": negative_terms,
        },
        "principles": principles,
        "evidence_grade": {
            "value": "project",
            "reason": (
                "Project-owned guidance with no claim that it improves outcomes "
                "without a controlled comparison."
            ),
        },
        "safety": {
            "risk": "low",
            "may_request_permissions": False,
            "may_weaken_constraints": False,
        },
        "risks": risks,
        "measurement": measurement,
        "review": {
            "reviewers": ["agent-efficiency maintainers"],
            "reason": (
                "Checked for narrow scope, reversible advice, privacy, and a "
                "measurable user outcome."
            ),
        },
        "published_at": PUBLISHED_AT,
        "verified_at": PUBLISHED_AT,
        "expires_at": EXPIRES_AT,
        "supersedes": [],
    }


def build_pack() -> dict[str, Any]:
    cards = [
        card(
            card_id="core.narrow-review",
            title="Review the changed behavior first",
            directive=(
                "Review changed behavior before broad maintainability concerns. "
                "Separate correctness findings from design suggestions, and tie "
                "each finding to code, a trace, or a focused check."
            ),
            rationale=(
                "A narrow first pass finds consequential defects without spending "
                "the review budget on unrelated cleanup."
            ),
            tasks=["review"],
            terms=[
                "audit",
                "diff",
                "findings",
                "review",
                "security",
                "specification",
            ],
            negative_terms=["format only", "spelling only"],
            principles=["AE.REVIEW.CHANGED-BEHAVIOR", "AE.EVIDENCE.MATCH-CLAIM"],
            risks=[
                "A narrow review can miss defects outside the declared change.",
                "A direct code reference does not replace runtime evidence.",
            ],
            measurement=(
                "Useful when it identifies a changed-behavior defect or removes an "
                "unsupported finding."
            ),
        ),
        card(
            card_id="core.research-boundary",
            title="Define the decision before research",
            directive=(
                "State the decision, required output, exclusions, and stopping "
                "condition before research. Prefer current primary sources and "
                "label conclusions that the evidence does not establish."
            ),
            rationale=(
                "A decision-shaped research pass limits unrelated browsing and "
                "keeps unsupported conclusions out of downstream work."
            ),
            tasks=["design", "research"],
            terms=[
                "compare",
                "design",
                "evaluate",
                "research",
                "strategy",
                "verify claim",
            ],
            negative_terms=["known local fact", "mechanical lookup"],
            principles=["AE.RESEARCH.DECISION", "AE.EVIDENCE.MATCH-CLAIM"],
            risks=[
                "A stopping condition can be too narrow for the decision.",
                "Primary sources may not support comparative outcome claims.",
            ],
            measurement=(
                "Useful when it defines a missing output, stops unrelated research, "
                "or narrows an unsupported claim."
            ),
        ),
        card(
            card_id="core.work-packet",
            title="Define success before a substantial change",
            directive=(
                "Before editing, state observable success checks, explicit "
                "boundaries, and the smallest integrated result. Spend planning "
                "time on choices that are costly to reverse."
            ),
            rationale=(
                "A bounded work packet gives the agent a clear stopping point and "
                "tests the main integration path before work expands."
            ),
            tasks=["build", "fix", "migration", "refactor"],
            terms=[
                "architecture",
                "build",
                "feature",
                "implement",
                "migration",
                "refactor",
            ],
            negative_terms=["documentation only", "mechanical edit", "typo"],
            principles=["AE.CHANGE.SUCCESS", "AE.CHANGE.SMALLEST-RESULT"],
            risks=[
                "The checkpoint adds overhead to a genuinely mechanical change.",
                "A weak success check can create false confidence.",
            ],
            measurement=(
                "Useful when it establishes a missing success check, boundary, or "
                "integrated proof before implementation."
            ),
        ),
    ]
    cards.sort(key=lambda item: item["id"])
    index: dict[str, list[str]] = {}
    for item in cards:
        for term in item["triggers"]["terms"]:
            index.setdefault(term, []).append(item["id"])
    retrieval_index = [
        {"term": term, "card_ids": sorted(card_ids)}
        for term, card_ids in sorted(index.items())
    ]
    return {
        "schema_version": 1,
        "kind": "guidance",
        "channel": "embedded",
        "pack_id": "agent-efficiency-guidance",
        "sequence": 1,
        "generated_at": PUBLISHED_AT,
        "valid_until": EXPIRES_AT,
        "requires": {
            "agent_efficiency": {
                "min_inclusive": "0.1.0",
                "max_exclusive": "1.0.0",
            },
            "pack_schema": {
                "min_inclusive": "1.0.0",
                "max_exclusive": "2.0.0",
            },
        },
        "built_from": {
            "project": "agent-efficiency",
            "source": "bundled",
        },
        "publication": {
            "reviewers": ["agent-efficiency maintainers"],
        },
        "card_count": len(cards),
        "card_digests": [
            {
                "id": item["id"],
                "revision": item["revision"],
                "content_sha256": digest(item),
            }
            for item in cards
        ],
        "cards": cards,
        "retrieval_index": retrieval_index,
        "retrieval_tests": [
            {
                "id": "bounded-research",
                "event": "UserPromptSubmit",
                "task": "research",
                "signals": ["compare", "research"],
                "expected_card_ids": ["core.research-boundary"],
            },
            {
                "id": "exact-review",
                "event": "UserPromptSubmit",
                "task": "review",
                "signals": ["diff", "review"],
                "expected_card_ids": ["core.narrow-review"],
            },
            {
                "id": "substantial-build",
                "event": "UserPromptSubmit",
                "task": "build",
                "signals": ["build", "feature"],
                "expected_card_ids": ["core.work-packet"],
            },
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    payload = canonical_bytes(build_pack())
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_bytes() != payload:
            raise SystemExit("bundled guidance pack is stale")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
