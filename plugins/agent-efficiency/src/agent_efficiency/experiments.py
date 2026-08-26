"""Opt-in capability experiments with explicit accepted-outcome evidence."""

from __future__ import annotations

import math
import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from agent_efficiency.store import Store, utc_now


VALID_COHORTS = ("observe", "advise")
VALID_ACCEPTANCE_EVIDENCE = ("test", "ci", "review", "user-confirmed", "none")
VALID_COMPLETION_EVIDENCE = (
    "current",
    "stale",
    "missing",
    "failed",
    "blocked",
    "inconclusive",
)
VALID_FEEDBACK = ("useful", "neutral", "distracting")
VALID_INVALIDATION_REASONS = (
    "data-entry-error",
    "protocol-deviation",
    "evidence-withdrawn",
)
CANONICAL_CARD_IDS = (
    "core.narrow-review",
    "core.research-boundary",
    "core.work-packet",
)
MIN_ACCEPTED_PER_COHORT = 5
MIN_GUIDANCE_EXPANSION_FEEDBACK = 20
GUIDANCE_EXPANSION_USEFUL_RATE = 0.80
GUIDANCE_EXPANSION_DISTRACTING_RATE = 0.05
RUNTIME_P95_GATES_MS = {"selection": 15.0, "session-start": 75.0}
HOOK_P95_GATES_MS = {"normal-hook": 100.0, "session-start": 150.0}
MIN_RUNTIME_GATE_SAMPLES = 20
IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ExperimentError(ValueError):
    """A closed, user-correctable experiment contract failure."""


def enroll_session(
    store: Store,
    session_id: str,
    *,
    experiment_id: str,
    cohort: str,
    task_class: str,
    task_set_digest: str,
    agent_profile: str,
    blinded: bool,
) -> dict[str, Any]:
    """Enroll once and bind the session mode to its explicit cohort."""

    _ensure_schema(store)
    experiment_id = _identifier(experiment_id, "experiment ID")
    task_class = _identifier(task_class, "task class")
    agent_profile = _identifier(agent_profile, "agent profile")
    if not SHA256_RE.fullmatch(task_set_digest):
        raise ExperimentError("task set requires a lowercase sha256 manifest digest")
    if cohort not in VALID_COHORTS:
        raise ExperimentError(f"cohort must be one of: {', '.join(VALID_COHORTS)}")
    now = utc_now()
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        session = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if session is None:
            raise ExperimentError(f"unknown session: {session_id}")
        existing = conn.execute(
            "SELECT * FROM experiment_enrollments WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if existing:
            current = dict(existing)
            expected = {
                "experiment_id": experiment_id,
                "cohort": cohort,
                "task_class": task_class,
                "task_set_digest": task_set_digest,
                "agent_profile": agent_profile,
                "blinded": int(blinded),
            }
            if any(current[key] != value for key, value in expected.items()):
                raise ExperimentError(
                    "session enrollment is immutable; use a fresh session for a "
                    "different experiment, cohort, task set, task class, agent "
                    "profile, or blinding state"
                )
        else:
            if any(
                int(session[column] or 0) > 0
                for column in ("turn_count", "tool_count", "nudge_count")
            ):
                raise ExperimentError(
                    "enrollment must happen before the session's first task turn, "
                    "tool, or nudge"
                )
            conn.execute(
                """
                INSERT INTO experiment_enrollments(
                    session_id, experiment_id, cohort, task_class,
                    task_set_digest, agent_profile, blinded, enrolled_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    experiment_id,
                    cohort,
                    task_class,
                    task_set_digest,
                    agent_profile,
                    int(blinded),
                    now,
                ),
            )
    store.set_mode(session_id, cohort)
    enrollment = get_enrollment(store, session_id)
    assert enrollment is not None
    return enrollment


def get_enrollment(store: Store, session_id: str) -> dict[str, Any] | None:
    _ensure_schema(store)
    with store.connect() as conn:
        row = conn.execute(
            "SELECT * FROM experiment_enrollments WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def experiment_session_status(store: Store, session_id: str) -> dict[str, Any]:
    """Expose rateable card IDs and structured state without prompt content."""

    _ensure_schema(store)
    with store.connect() as conn:
        receipt_rows = conn.execute(
            """
            SELECT card_id, disposition, score, match_reason, created_at
              FROM knowledge_receipts
             WHERE session_id = ?
               AND disposition IN ('emitted', 'observed')
             ORDER BY id DESC
            """,
            (session_id,),
        ).fetchall()
        feedback_rows = conn.execute(
            """
            SELECT card_id, rating, changed_next_action, recorded_at
              FROM capability_feedback
             WHERE session_id = ?
             ORDER BY card_id
            """,
            (session_id,),
        ).fetchall()
    cards: dict[str, dict[str, Any]] = {}
    for row in receipt_rows:
        card_id = str(row["card_id"])
        cards.setdefault(card_id, dict(row))
    return {
        "session_id": session_id,
        "enrollment": get_enrollment(store, session_id),
        "outcome": get_outcome(store, session_id),
        "rateable_cards": [cards[card_id] for card_id in sorted(cards)],
        "feedback": [dict(row) for row in feedback_rows],
    }


def invalidate_enrollment(
    store: Store,
    session_id: str,
    *,
    reason: str,
) -> dict[str, Any]:
    """Permanently exclude a compromised session without deleting evidence."""

    _ensure_schema(store)
    if reason not in VALID_INVALIDATION_REASONS:
        raise ExperimentError(
            "invalidation reason must be one of: "
            + ", ".join(VALID_INVALIDATION_REASONS)
        )
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM experiment_enrollments WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise ExperimentError("session is not enrolled in an experiment")
        current = dict(row)
        if current["invalidated_at"] is not None:
            if current["invalidation_reason"] != reason:
                raise ExperimentError(
                    "experiment invalidation is immutable; preserve the first reason"
                )
        else:
            conn.execute(
                """
                UPDATE experiment_enrollments
                   SET invalidated_at = ?, invalidation_reason = ?
                 WHERE session_id = ?
                """,
                (utc_now(), reason, session_id),
            )
    enrollment = get_enrollment(store, session_id)
    assert enrollment is not None
    return enrollment


def record_outcome(
    store: Store,
    session_id: str,
    *,
    accepted: bool,
    acceptance_evidence: str,
    acceptance_evidence_digest: str | None,
    completion_evidence_state: str = "inconclusive",
    wall_time_minutes: float,
    correction_turns: int = 0,
    human_review_minutes: float = 0,
    escaped_defects: int = 0,
) -> dict[str, Any]:
    """Record one immutable, structured outcome without free-form task content."""

    _ensure_schema(store)
    if get_enrollment(store, session_id) is None:
        raise ExperimentError("session is not enrolled in an experiment")
    if acceptance_evidence not in VALID_ACCEPTANCE_EVIDENCE:
        raise ExperimentError(
            "acceptance evidence must be one of: "
            + ", ".join(VALID_ACCEPTANCE_EVIDENCE)
        )
    if acceptance_evidence == "none":
        if acceptance_evidence_digest is not None:
            raise ExperimentError(
                "evidence digest must be omitted when evidence is none"
            )
    elif not acceptance_evidence_digest or not SHA256_RE.fullmatch(
        acceptance_evidence_digest
    ):
        raise ExperimentError(
            "non-none evidence requires a lowercase sha256 evidence digest"
        )
    if completion_evidence_state not in VALID_COMPLETION_EVIDENCE:
        raise ExperimentError(
            "completion evidence state must be one of: "
            + ", ".join(VALID_COMPLETION_EVIDENCE)
        )
    if wall_time_minutes <= 0 or not math.isfinite(wall_time_minutes):
        raise ExperimentError("wall time must be a finite value greater than zero")
    if (
        correction_turns < 0
        or human_review_minutes < 0
        or not math.isfinite(human_review_minutes)
        or escaped_defects < 0
    ):
        raise ExperimentError("outcome counts and durations cannot be negative")

    value = {
        "session_id": session_id,
        "accepted": int(accepted),
        "acceptance_evidence": acceptance_evidence,
        "acceptance_evidence_digest": acceptance_evidence_digest,
        "completion_evidence_state": completion_evidence_state,
        "wall_time_minutes": float(wall_time_minutes),
        "correction_turns": int(correction_turns),
        "human_review_minutes": float(human_review_minutes),
        "escaped_defects": int(escaped_defects),
    }
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM session_outcomes WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if existing:
            current = dict(existing)
            if any(current[key] != item for key, item in value.items()):
                raise ExperimentError(
                    "session outcome is immutable; preserve the original evidence"
                )
        else:
            conn.execute(
                """
                INSERT INTO session_outcomes(
                    session_id, accepted, acceptance_evidence,
                    acceptance_evidence_digest, completion_evidence_state,
                    wall_time_minutes, correction_turns, human_review_minutes,
                    escaped_defects, recorded_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*value.values(), utc_now()),
            )
    return get_outcome(store, session_id) or {}


def get_outcome(store: Store, session_id: str) -> dict[str, Any] | None:
    _ensure_schema(store)
    with store.connect() as conn:
        row = conn.execute(
            "SELECT * FROM session_outcomes WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def record_card_feedback(
    store: Store,
    session_id: str,
    card_id: str,
    *,
    rating: str,
    changed_next_action: bool,
) -> dict[str, Any]:
    """Rate an emitted or silently observed card once per enrolled session."""

    _ensure_schema(store)
    if get_enrollment(store, session_id) is None:
        raise ExperimentError("session is not enrolled in an experiment")
    if rating not in VALID_FEEDBACK:
        raise ExperimentError(f"rating must be one of: {', '.join(VALID_FEEDBACK)}")
    with store.connect() as conn:
        receipt = conn.execute(
            """
            SELECT 1 FROM knowledge_receipts
             WHERE session_id = ? AND card_id = ?
               AND disposition IN ('emitted', 'observed')
             LIMIT 1
            """,
            (session_id, card_id),
        ).fetchone()
    if not receipt:
        raise ExperimentError(
            "feedback requires an emitted or silently observed card receipt"
        )

    value = {
        "session_id": session_id,
        "card_id": card_id,
        "rating": rating,
        "changed_next_action": int(changed_next_action),
    }
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """
            SELECT * FROM capability_feedback
             WHERE session_id = ? AND card_id = ?
            """,
            (session_id, card_id),
        ).fetchone()
        if existing:
            current = dict(existing)
            if any(current[key] != item for key, item in value.items()):
                raise ExperimentError(
                    "card feedback is immutable; preserve the original judgment"
                )
        else:
            conn.execute(
                """
                INSERT INTO capability_feedback(
                    session_id, card_id, rating, changed_next_action, recorded_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (*value.values(), utc_now()),
            )
    with store.connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM capability_feedback
             WHERE session_id = ? AND card_id = ?
            """,
            (session_id, card_id),
        ).fetchone()
    return dict(row) if row else {}


def build_experiment_evaluation(
    store: Store,
    *,
    experiment_id: str | None = None,
    days: int = 30,
) -> dict[str, Any]:
    """Compare accepted outcomes only inside exact operational strata."""

    _ensure_schema(store)
    if experiment_id is not None:
        experiment_id = _identifier(experiment_id, "experiment ID")
    rows = _experiment_sessions(store, experiment_id=experiment_id, days=days)
    grouped: dict[
        tuple[str, str, str, str, str, str, str, int], list[dict[str, Any]]
    ] = defaultdict(list)
    for row in rows:
        key = (
            str(row["experiment_id"]),
            str(row["task_class"]),
            str(row["task_set_digest"]),
            str(row["project_key"]),
            str(row["host"]),
            str(row["model"] or "unknown"),
            str(row["agent_profile"]),
            int(row["blinded"]),
        )
        grouped[key].append(row)

    strata: list[dict[str, Any]] = []
    eligible_strata = 0
    for key in sorted(grouped):
        members = grouped[key]
        arms = {
            cohort: [row for row in members if row["cohort"] == cohort]
            for cohort in VALID_COHORTS
        }
        summaries = {cohort: _arm_summary(arm) for cohort, arm in arms.items()}
        valid = {
            cohort: [row for row in arm if row["invalidated_at"] is None]
            for cohort, arm in arms.items()
        }
        accepted = {
            cohort: [row for row in arm if _verified_accepted(row)]
            for cohort, arm in valid.items()
        }
        enough = all(
            len(accepted[cohort]) >= MIN_ACCEPTED_PER_COHORT for cohort in VALID_COHORTS
        )
        enrollment_balanced = len(valid["observe"]) == len(valid["advise"])
        evidence_digests = [
            str(row["acceptance_evidence_digest"])
            for cohort in VALID_COHORTS
            for row in accepted[cohort]
        ]
        evidence_unique = len(evidence_digests) == len(set(evidence_digests))
        outcomes_complete = all(
            row["accepted"] is not None
            for cohort in VALID_COHORTS
            for row in valid[cohort]
        )
        cost_complete = all(
            row["cost_usd"] is not None
            for cohort in VALID_COHORTS
            for row in valid[cohort]
        )
        matched = (
            enough and enrollment_balanced and evidence_unique and outcomes_complete
        )
        eligible = matched and cost_complete
        if eligible:
            eligible_strata += 1
        comparison = _comparison(valid) if matched else None
        strata.append(
            {
                "match": {
                    "experiment_id": key[0],
                    "task_class": key[1],
                    "task_set_digest": key[2],
                    "project_key": key[3],
                    "host": key[4],
                    "model": key[5],
                    "agent_profile": key[6],
                    "blinded": bool(key[7]),
                },
                "cohorts": summaries,
                "comparable": matched,
                "enrollment_balanced": enrollment_balanced,
                "evidence_digests_unique": evidence_unique,
                "outcomes_complete": outcomes_complete,
                "cost_claim_eligible": eligible,
                "cost_claim_reason": (
                    "matched verified accepted outcomes and host cost in both cohorts"
                    if eligible
                    else _claim_gate_reason(
                        accepted,
                        cost_complete,
                        evidence_unique,
                        outcomes_complete,
                        enrollment_balanced,
                    )
                ),
                "observed_difference": comparison,
            }
        )

    capability = local_capability_measurement(store, days=days)
    runtime_health = local_runtime_health(store, days=days)
    pilot = _pilot_decision(rows, strata, runtime_health)
    return {
        "days": days,
        "experiment_id": experiment_id,
        "enrolled_sessions": len(rows),
        "matched_strata": len(strata),
        "cost_claim_gate": {
            "eligible": eligible_strata > 0,
            "eligible_strata": eligible_strata,
            "minimum_verified_accepted_per_cohort": MIN_ACCEPTED_PER_COHORT,
            "statement": (
                "Eligibility permits reporting an observed matched difference; "
                "it does not establish causality."
            ),
        },
        "strata": strata,
        "pilot": pilot,
        "runtime_health": runtime_health,
        "guidance_expansion_gate": capability["guidance_expansion_gate"],
        "claim_boundary": (
            "No savings estimate is produced without comparable verified accepted "
            "outcomes and host-reported cost. Runtime, context, rework, review, "
            "and defect components remain visible rather than collapsed into a "
            "fabricated scalar."
        ),
    }


def _pilot_decision(
    rows: list[dict[str, Any]],
    strata: list[dict[str, Any]],
    runtime_health: dict[str, Any],
) -> dict[str, Any]:
    comparable_matches = {
        (
            item["match"]["experiment_id"],
            item["match"]["task_class"],
            item["match"]["task_set_digest"],
            item["match"]["project_key"],
            item["match"]["host"],
            item["match"]["model"],
            item["match"]["agent_profile"],
            int(item["match"]["blinded"]),
        )
        for item in strata
        if item["comparable"]
    }
    matched = [
        row
        for row in rows
        if row["invalidated_at"] is None
        and (
            str(row["experiment_id"]),
            str(row["task_class"]),
            str(row["task_set_digest"]),
            str(row["project_key"]),
            str(row["host"]),
            str(row["model"] or "unknown"),
            str(row["agent_profile"]),
            int(row["blinded"]),
        )
        in comparable_matches
    ]
    arms = {
        cohort: [row for row in matched if row["cohort"] == cohort]
        for cohort in VALID_COHORTS
    }
    pairs = min(len(arms["observe"]), len(arms["advise"]))
    matched_tasks = len(matched)
    all_valid_matched = matched_tasks == sum(
        row["invalidated_at"] is None for row in rows
    )
    hosts = sorted({str(row["host"]) for row in matched})
    acceptance = {
        cohort: _ratio(
            sum(_verified_accepted(row) for row in arm),
            len(arm),
        )
        for cohort, arm in arms.items()
    }
    stale_missing = {
        cohort: _ratio(
            sum(
                row["completion_evidence_state"] in {"stale", "missing"} for row in arm
            ),
            len(arm),
        )
        for cohort, arm in arms.items()
    }
    actions = {
        cohort: (_median(row["tool_count"] for row in arm) if arm else None)
        for cohort, arm in arms.items()
    }
    evidence_reduction = _relative_reduction(
        stale_missing["observe"], stale_missing["advise"]
    )
    action_reduction = _relative_reduction(actions["observe"], actions["advise"])
    action_overhead = -action_reduction if action_reduction is not None else None
    utility = _intervention_utility(arms["advise"])
    overhead_values = [
        (float(row["hook_runtime_us"]) / 1000)
        / (float(row["wall_time_minutes"]) * 60 * 1000)
        * 100
        for row in arms["advise"]
        if row["wall_time_minutes"]
    ]
    median_overhead = _median(overhead_values) if overhead_values else None
    runtime_gates = [
        runtime_health.get(operation, {}).get("within_p95_gate")
        for operation in sorted(HOOK_P95_GATES_MS)
    ]
    enough_data = (
        matched_tasks >= 24
        and all_valid_matched
        and set(hosts) >= {"claude", "cursor", "codex"}
        and all(value is not None for value in acceptance.values())
        and utility["rated"] >= utility["emitted"]
        and bool(runtime_gates)
        and all(value is not None for value in runtime_gates)
    )
    criteria = {
        "no_verified_acceptance_reduction": (
            acceptance["advise"] >= acceptance["observe"]
            if all(value is not None for value in acceptance.values())
            else None
        ),
        "evidence_or_action_improved": (
            (evidence_reduction is not None and evidence_reduction >= 0.30)
            or (action_reduction is not None and action_reduction >= 0.15)
        ),
        "false_or_unnecessary_below_10_percent": (
            utility["false_or_unnecessary_rate"] < 0.10
            if utility["false_or_unnecessary_rate"] is not None
            else None
        ),
        "median_action_overhead_below_5_percent": (
            action_overhead < 0.05 if action_overhead is not None else None
        ),
        "runtime_within_limits": (all(runtime_gates) if runtime_gates else None),
    }
    passed = enough_data and all(value is True for value in criteria.values())
    return {
        "status": "pass"
        if passed
        else ("fail" if enough_data else "insufficient_data"),
        "matched_tasks": matched_tasks,
        "minimum_matched_tasks": 24,
        "matched_pairs": pairs,
        "all_valid_sessions_matched": all_valid_matched,
        "hosts": hosts,
        "required_hosts": ["claude", "cursor", "codex"],
        "verified_acceptance_rate": acceptance,
        "stale_or_missing_completion_rate": stale_missing,
        "relative_evidence_reduction": _rounded(evidence_reduction)
        if evidence_reduction is not None
        else None,
        "median_actions": actions,
        "relative_action_reduction": _rounded(action_reduction)
        if action_reduction is not None
        else None,
        "relative_action_overhead": _rounded(action_overhead)
        if action_overhead is not None
        else None,
        "intervention_utility": utility,
        "median_hook_overhead_percent": _rounded(median_overhead)
        if median_overhead is not None
        else None,
        "criteria": criteria,
        "stop_guidance_expansion": (
            enough_data and criteria["evidence_or_action_improved"] is False
        ),
        "claim_boundary": (
            "This paired pilot supports a product decision. It does not establish "
            "a broad causal or market claim."
        ),
    }


def _relative_reduction(
    before: float | int | None, after: float | int | None
) -> float | None:
    if before is None or after is None or float(before) <= 0:
        return None
    return (float(before) - float(after)) / float(before)


def local_runtime_health(
    store: Store, *, days: int = 30, session_id: str | None = None
) -> dict[str, Any]:
    """Summarize measured full-hook latency and failures."""

    _ensure_schema(store)
    since = _since(days)
    clause = "recorded_at >= ?"
    params: list[Any] = [since]
    if session_id:
        clause += " AND session_id = ?"
        params.append(session_id)
    with store.connect() as conn:
        rows = conn.execute(
            f"SELECT operation, duration_us, outcome FROM runtime_health "
            f"WHERE {clause} ORDER BY operation, id",
            params,
        ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["operation"])].append(dict(row))
    result: dict[str, Any] = {
        "scope": "session" if session_id else "cohort",
        "session_id": session_id,
    }
    for operation, gate_ms in HOOK_P95_GATES_MS.items():
        samples = grouped.get(operation, [])
        evaluable = len(samples) >= MIN_RUNTIME_GATE_SAMPLES
        p95_ms = (
            _rounded(_p95(item["duration_us"] for item in samples) / 1000)
            if samples
            else None
        )
        result[operation] = {
            "samples": len(samples),
            "failures": sum(item["outcome"] == "failed" for item in samples),
            "p50_ms": (
                _rounded(_median(item["duration_us"] for item in samples) / 1000)
                if samples
                else None
            ),
            "p95_ms": p95_ms,
            "p95_gate_ms": gate_ms,
            "minimum_gate_samples": MIN_RUNTIME_GATE_SAMPLES,
            "gate_evaluable": evaluable,
            "within_p95_gate": p95_ms <= gate_ms if evaluable else None,
        }
    return result


def local_capability_measurement(
    store: Store,
    *,
    days: int = 30,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Summarize utility, context, and measured local runtime overhead."""

    _ensure_schema(store)
    since = _since(days)
    session_clause = " AND session_id = ?" if session_id else ""
    params: tuple[Any, ...] = (since, session_id) if session_id else (since,)
    with store.connect() as conn:
        receipts = conn.execute(
            f"""
            SELECT disposition, COUNT(*) AS count,
                   COALESCE(SUM(emitted_chars), 0) AS emitted_chars
              FROM knowledge_receipts
             WHERE created_at >= ?{session_clause}
             GROUP BY disposition
             ORDER BY disposition
            """,
            params,
        ).fetchall()
        feedback = conn.execute(
            f"""
            SELECT rating, COUNT(*) AS count,
                   SUM(changed_next_action) AS changed
              FROM capability_feedback
             WHERE recorded_at >= ?{session_clause}
             GROUP BY rating
             ORDER BY rating
            """,
            params,
        ).fetchall()
        runtimes = conn.execute(
            f"""
            SELECT operation, duration_us, context_chars
              FROM capability_runtime_samples
             WHERE recorded_at >= ?{session_clause}
             ORDER BY operation, id
            """,
            params,
        ).fetchall()

    by_operation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in runtimes:
        by_operation[str(row["operation"])].append(dict(row))
    runtime_summary: dict[str, dict[str, Any]] = {}
    for operation, samples in sorted(by_operation.items()):
        p95_ms = _rounded(_p95(item["duration_us"] for item in samples) / 1000)
        gate_ms = RUNTIME_P95_GATES_MS.get(operation)
        gate_evaluable = (
            gate_ms is not None and len(samples) >= MIN_RUNTIME_GATE_SAMPLES
        )
        runtime_summary[operation] = {
            "samples": len(samples),
            "p50_ms": _rounded(_median(item["duration_us"] for item in samples) / 1000),
            "p95_ms": p95_ms,
            "p95_gate_ms": gate_ms,
            "minimum_gate_samples": MIN_RUNTIME_GATE_SAMPLES,
            "gate_evaluable": gate_evaluable,
            "within_p95_gate": (
                p95_ms <= gate_ms if gate_evaluable and gate_ms is not None else None
            ),
            "context_chars": sum(int(item["context_chars"]) for item in samples),
        }
    rating_counts = {str(row["rating"]): int(row["count"]) for row in feedback}
    changed = sum(int(row["changed"] or 0) for row in feedback)
    ratings = sum(rating_counts.values())
    return {
        "receipts": {
            str(row["disposition"]): {
                "count": int(row["count"]),
                "emitted_chars": int(row["emitted_chars"]),
            }
            for row in receipts
        },
        "utility": {
            "ratings": ratings,
            "ratings_by_value": rating_counts,
            "changed_next_action": changed,
            "useful_rate": (
                _rounded(rating_counts.get("useful", 0) / ratings) if ratings else None
            ),
            "distracting_rate": (
                _rounded(rating_counts.get("distracting", 0) / ratings)
                if ratings
                else None
            ),
        },
        "runtime": runtime_summary,
        "scope": "session" if session_id else "cohort",
        "session_id": session_id,
        "guidance_expansion_gate_scope": "cohort",
        "guidance_expansion_gate": guidance_expansion_gate(store, days=days),
    }


def guidance_expansion_gate(store: Store, *, days: int = 30) -> dict[str, Any]:
    """Keep source breadth closed until the canonical cards prove precision."""

    _ensure_schema(store)
    since = _since(days)
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT f.rating, COUNT(*) AS count
              FROM capability_feedback f
              JOIN experiment_enrollments e ON e.session_id = f.session_id
             WHERE f.recorded_at >= ?
               AND f.card_id IN (?, ?, ?)
               AND e.invalidated_at IS NULL
               AND e.blinded = 1
             GROUP BY f.rating
            """,
            (since, *CANONICAL_CARD_IDS),
        ).fetchall()
    counts = {str(row["rating"]): int(row["count"]) for row in rows}
    total = sum(counts.values())
    useful_rate = counts.get("useful", 0) / total if total else 0.0
    distracting_rate = counts.get("distracting", 0) / total if total else 0.0
    eligible = (
        total >= MIN_GUIDANCE_EXPANSION_FEEDBACK
        and useful_rate >= GUIDANCE_EXPANSION_USEFUL_RATE
        and distracting_rate <= GUIDANCE_EXPANSION_DISTRACTING_RATE
    )
    return {
        "eligible": eligible,
        "ratings": total,
        "minimum_ratings": MIN_GUIDANCE_EXPANSION_FEEDBACK,
        "useful_rate": _rounded(useful_rate) if total else None,
        "minimum_useful_rate": GUIDANCE_EXPANSION_USEFUL_RATE,
        "distracting_rate": _rounded(distracting_rate) if total else None,
        "maximum_distracting_rate": GUIDANCE_EXPANSION_DISTRACTING_RATE,
        "decision": (
            "eligible for human review of guidance expansion"
            if eligible
            else "keep source set closed"
        ),
    }


def _experiment_sessions(
    store: Store,
    *,
    experiment_id: str | None,
    days: int,
) -> list[dict[str, Any]]:
    clauses = ["e.enrolled_at >= ?"]
    params: list[Any] = [_since(days)]
    if experiment_id is not None:
        clauses.append("e.experiment_id = ?")
        params.append(experiment_id)
    with store.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                e.experiment_id, e.cohort, e.task_class, e.task_set_digest,
                e.agent_profile, e.blinded, e.enrolled_at, e.invalidated_at,
                e.invalidation_reason,
                s.session_id, s.project_key, s.host, s.model, s.cost_usd,
                s.tool_count, s.failure_count, s.validation_count, s.edit_count,
                o.accepted, o.acceptance_evidence,
                o.acceptance_evidence_digest, o.completion_evidence_state,
                o.wall_time_minutes,
                o.correction_turns, o.human_review_minutes, o.escaped_defects,
                (
                    SELECT COALESCE(SUM(r.emitted_chars), 0)
                      FROM knowledge_receipts r
                     WHERE r.session_id = s.session_id
                ) AS guidance_chars,
                (
                    SELECT COALESCE(SUM(duration_us), 0)
                      FROM capability_runtime_samples x
                     WHERE x.session_id = s.session_id
                ) AS layer_runtime_us,
                (
                    SELECT COALESCE(SUM(duration_us), 0)
                      FROM runtime_health h
                     WHERE h.session_id = s.session_id
                ) AS hook_runtime_us,
                (
                    SELECT COUNT(*)
                      FROM turns t
                     WHERE t.session_id = s.session_id
                       AND t.code_edit_count > 0
                ) AS edited_turns,
                (
                    SELECT COUNT(*)
                      FROM turns t
                     WHERE t.session_id = s.session_id
                       AND t.code_edit_count > 0
                       AND t.validation_count > 0
                ) AS verified_edited_turns,
                (
                    SELECT COUNT(*) FROM interventions i
                     WHERE i.session_id = s.session_id
                       AND i.disposition = 'emitted'
                ) AS intervention_emitted,
                (
                    SELECT COUNT(DISTINCT i.id)
                      FROM interventions i
                      JOIN intervention_outcomes x
                        ON x.intervention_id = i.id
                     WHERE i.session_id = s.session_id
                       AND x.outcome = 'suggested_action_observed'
                ) AS intervention_followed,
                (
                    SELECT COUNT(DISTINCT i.id)
                      FROM interventions i
                      JOIN intervention_outcomes x
                        ON x.intervention_id = i.id
                     WHERE i.session_id = s.session_id
                       AND x.outcome = 'user_disabled_advice'
                ) AS intervention_disabled,
                (
                    SELECT COUNT(*) FROM intervention_feedback f
                      JOIN interventions i ON i.id = f.intervention_id
                     WHERE i.session_id = s.session_id
                ) AS intervention_rated,
                (
                    SELECT COUNT(*) FROM intervention_feedback f
                      JOIN interventions i ON i.id = f.intervention_id
                     WHERE i.session_id = s.session_id
                       AND f.judgment = 'false_or_unnecessary'
                ) AS intervention_false
              FROM experiment_enrollments e
              JOIN sessions s ON s.session_id = e.session_id
              LEFT JOIN session_outcomes o ON o.session_id = e.session_id
             WHERE {" AND ".join(clauses)}
             ORDER BY e.experiment_id, e.task_class, e.task_set_digest,
                      s.project_key,
                      s.host, s.model, e.agent_profile, e.blinded,
                      e.cohort, e.enrolled_at
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def _arm_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if row["invalidated_at"] is None]
    comparable = [row for row in valid if _verified_accepted(row)]
    outcomes = [row for row in valid if row["accepted"] is not None]
    return {
        "enrolled": len(rows),
        "valid_enrolled": len(valid),
        "invalidated": sum(row["invalidated_at"] is not None for row in rows),
        "outcomes_recorded": len(outcomes),
        "verified_accepted": len(comparable),
        "excluded_from_comparison": len(rows) - len(comparable),
        "acceptance_rate": _ratio(len(comparable), len(valid)),
        "intervention_utility": _intervention_utility(valid),
        "metrics_per_verified_accepted_task": _metrics(valid),
    }


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    accepted = [row for row in rows if _verified_accepted(row)]
    accepted_count = len(accepted)
    if not rows or not accepted_count:
        return None
    outcomes_complete = all(row["accepted"] is not None for row in rows)
    cost_complete = all(row["cost_usd"] is not None for row in rows)
    return {
        "cohort_acceptance_rate": _ratio(accepted_count, len(rows)),
        "cohort_host_cost_usd_per_accepted": (
            _rounded(sum(float(row["cost_usd"]) for row in rows) / accepted_count)
            if cost_complete
            else None
        ),
        "accepted_host_cost_usd_median": _complete_median(
            row["cost_usd"] for row in accepted
        ),
        "cohort_wall_time_minutes_per_accepted": (
            _rounded(
                sum(float(row["wall_time_minutes"]) for row in rows) / accepted_count
            )
            if outcomes_complete
            else None
        ),
        "accepted_wall_time_minutes_median": _rounded(
            _median(row["wall_time_minutes"] for row in accepted)
        ),
        "accepted_action_count_median": _rounded(
            _median(row["tool_count"] for row in accepted)
        ),
        "completion_with_stale_or_missing_evidence_rate": _ratio(
            sum(
                row["completion_evidence_state"] in {"stale", "missing"} for row in rows
            ),
            len(rows),
        ),
        "median_hook_overhead_percent": _rounded(
            _median(
                (float(row["hook_runtime_us"]) / 1000)
                / (float(row["wall_time_minutes"]) * 60 * 1000)
                * 100
                for row in accepted
            )
        ),
        "cohort_failure_calls_per_accepted": _rounded(
            sum(int(row["failure_count"]) for row in rows) / accepted_count
        ),
        "cohort_correction_turns_per_accepted": (
            _rounded(sum(int(row["correction_turns"]) for row in rows) / accepted_count)
            if outcomes_complete
            else None
        ),
        "cohort_human_review_minutes_per_accepted": (
            _rounded(
                sum(float(row["human_review_minutes"]) for row in rows) / accepted_count
            )
            if outcomes_complete
            else None
        ),
        "cohort_escaped_defects_per_accepted": (
            _rounded(sum(int(row["escaped_defects"]) for row in rows) / accepted_count)
            if outcomes_complete
            else None
        ),
        "cohort_guidance_chars_per_accepted": _rounded(
            sum(int(row["guidance_chars"]) for row in rows) / accepted_count
        ),
        "cohort_layer_runtime_ms_per_accepted": _rounded(
            sum(int(row["layer_runtime_us"]) for row in rows) / accepted_count / 1000
        ),
        "edited_turn_verification_rate": _ratio(
            sum(int(row["verified_edited_turns"]) for row in rows),
            sum(int(row["edited_turns"]) for row in rows),
        ),
    }


def _intervention_utility(rows: list[dict[str, Any]]) -> dict[str, Any]:
    emitted = sum(int(row["intervention_emitted"] or 0) for row in rows)
    followed = sum(int(row["intervention_followed"] or 0) for row in rows)
    disabled = sum(int(row["intervention_disabled"] or 0) for row in rows)
    rated = sum(int(row["intervention_rated"] or 0) for row in rows)
    false = sum(int(row["intervention_false"] or 0) for row in rows)
    return {
        "emitted": emitted,
        "followed": followed,
        "followed_rate": _ratio(followed, emitted),
        "disabled": disabled,
        "disable_rate": _ratio(disabled, emitted),
        "rated": rated,
        "false_or_unnecessary": false,
        "false_or_unnecessary_rate": _ratio(false, rated),
        "claim_boundary": "Observed associations do not establish causation.",
    }


def _comparison(
    rows: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    observe = _metrics(rows["observe"]) or {}
    advise = _metrics(rows["advise"]) or {}
    differences: dict[str, Any] = {}
    for key in sorted(set(observe) & set(advise)):
        before = observe[key]
        after = advise[key]
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            differences[key] = _rounded(float(after) - float(before))
        else:
            differences[key] = None
    return {
        "direction": "advise minus observe",
        "median_component_differences": differences,
        "interpretation": (
            "Negative is lower for cost, time, failures, corrections, review, "
            "defects, context, and runtime; positive is better for acceptance "
            "and edited-turn verification rates."
        ),
    }


def _claim_gate_reason(
    accepted: dict[str, list[dict[str, Any]]],
    cost_complete: bool,
    evidence_unique: bool,
    outcomes_complete: bool,
    enrollment_balanced: bool,
) -> str:
    short = [
        f"{cohort} has {len(accepted[cohort])}/"
        f"{MIN_ACCEPTED_PER_COHORT} verified accepted outcomes"
        for cohort in VALID_COHORTS
        if len(accepted[cohort]) < MIN_ACCEPTED_PER_COHORT
    ]
    if not outcomes_complete:
        short.append("one or more valid enrolled sessions lack an outcome")
    if not enrollment_balanced:
        short.append("observe and advise enrollment counts differ")
    if not cost_complete:
        short.append("host-reported cost is incomplete")
    if not evidence_unique:
        short.append("acceptance evidence digests are reused")
    return "; ".join(short) or "comparison is not eligible"


def _verified_accepted(row: dict[str, Any]) -> bool:
    return bool(
        row["invalidated_at"] is None
        and row["accepted"] == 1
        and row["acceptance_evidence"] not in {None, "none"}
        and row["acceptance_evidence_digest"] is not None
        and row["wall_time_minutes"] is not None
    )


def _ensure_schema(store: Store) -> None:
    try:
        store.ensure_current_schema()
    except sqlite3.OperationalError as exc:
        raise ExperimentError(f"experiment storage is unavailable: {exc}") from exc


def _identifier(value: str, label: str) -> str:
    candidate = value.strip().lower()
    if not IDENTIFIER_RE.fullmatch(candidate):
        raise ExperimentError(
            f"{label} must be 1-64 lowercase letters, digits, dot, dash, or underscore"
        )
    return candidate


def _since(days: int) -> str:
    return (
        (datetime.now(UTC) - timedelta(days=max(1, days)))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _median(values: Iterable[Any]) -> float:
    return float(statistics.median(float(value) for value in values))


def _complete_median(values: Iterable[Any]) -> float | None:
    observed = list(values)
    if not observed or any(value is None for value in observed):
        return None
    return _rounded(statistics.median(float(value) for value in observed))


def _p95(values: Iterable[Any]) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _ratio(numerator: int, denominator: int) -> float | None:
    return _rounded(numerator / denominator) if denominator else None


def _rounded(value: float) -> float:
    return round(float(value), 4)
