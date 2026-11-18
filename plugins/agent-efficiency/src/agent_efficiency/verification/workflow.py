"""Explicit project milestones, evidence explanations and fixed-code rework reports."""

from __future__ import annotations

from collections import Counter
import math

from agent_efficiency.store import project_identity, utc_now
from agent_efficiency.verification.environment import check_identity
from agent_efficiency.verification.jobs import JobLedger
from agent_efficiency.verification.receipts import explain_receipt
from agent_efficiency.verification.state import workspace_state

CAUSES = (
    "missing_prerequisite",
    "environment_mismatch",
    "probe_invalid",
    "product_failure",
    "tooling_failure",
    "scope_changed",
    "plan_order",
    "unknown",
)
KINDS = ("retry", "correction")
RATINGS = ("useful", "neutral", "distracting", "unrated")
PHASES = ("setup", "runtime", "review", "tooling", "unknown")


def evidence_explanations(config, store):
    state = workspace_state(config.root, config.digest)
    key, _ = project_identity(str(config.root))
    latest = {}
    for item in store.verification_receipts(key):
        latest.setdefault(item["check_id"], item)
    return [
        {
            "check_id": check.check_id,
            **explain_receipt(
                latest.get(check.check_id),
                check_digest=check_identity(check, store),
                workspace_digest=state.digest,
                max_age_seconds=check.max_age_seconds,
            ),
        }
        for check in config.checks
    ]


def workflow_status(config, store):
    if not config.milestones:
        return {"plan": "unavailable", "milestones": [], "release_approval": False}
    evidence = {item["check_id"]: item for item in evidence_explanations(config, store)}
    checks = {check.check_id: check for check in config.checks}
    values = []
    prior_ready = True
    for milestone in config.milestones:
        required = set()
        stack = list(milestone.checks)
        while stack:
            check_id = stack.pop()
            if check_id not in required:
                required.add(check_id)
                stack.extend(checks[check_id].depends_on)
        states = {key: evidence[key]["state"] for key in sorted(required)}
        ready = all(value == "current" for value in states.values())
        status = (
            "blocked"
            if not prior_ready
            else "ready"
            if ready
            else "failed"
            if any(
                value in {"fail", "blocked", "inconclusive"}
                for value in states.values()
            )
            else "pending"
        )
        values.append({"id": milestone.milestone_id, "state": status, "checks": states})
        prior_ready = prior_ready and ready
    return {
        "plan": "configured",
        "milestones": values,
        "release_approval": False,
        "next": next((item["id"] for item in values if item["state"] != "ready"), None),
    }


def _table(store):
    store.ensure_current_schema()
    with store.connect() as conn:
        causes = ",".join(repr(x) for x in CAUSES)
        conn.execute(f"""CREATE TABLE IF NOT EXISTS workflow_rework (
            id INTEGER PRIMARY KEY,
            project_key TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('retry','correction')),
            cause TEXT NOT NULL CHECK(cause IN ({causes})),
            duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0 AND duration_ms <= 86400000),
            cost_usd REAL,
            cost_basis TEXT NOT NULL CHECK(cost_basis IN ('estimate','reported','unavailable')),
            recorded_at TEXT NOT NULL,
            CHECK((cost_basis='unavailable' AND cost_usd IS NULL) OR
                  (cost_basis!='unavailable' AND cost_usd IS NOT NULL AND cost_usd >= 0 AND cost_usd <= 1000000000))
        )""")

        conn.execute("""CREATE TABLE IF NOT EXISTS workflow_rework_context (
            event_id INTEGER PRIMARY KEY REFERENCES workflow_rework(id),
            session_id TEXT REFERENCES sessions(session_id),
            rating TEXT NOT NULL CHECK(rating IN ('useful','neutral','distracting','unrated')),
            phase TEXT NOT NULL CHECK(phase IN ('setup','runtime','review','tooling','unknown'))
        )""")


def record_rework(
    store,
    root,
    *,
    kind,
    cause,
    duration_ms=0,
    cost_usd=None,
    cost_basis="unavailable",
    session_id=None,
    rating="unrated",
    phase="unknown",
):
    if rating not in RATINGS or phase not in PHASES:
        raise ValueError("rework requires fixed rating and phase codes")
    if kind not in KINDS or cause not in CAUSES:
        raise ValueError("rework requires fixed kind and cause codes")
    if type(duration_ms) is not int or not 0 <= duration_ms <= 86400000:
        raise ValueError("invalid rework duration")
    if cost_basis not in {"estimate", "reported", "unavailable"}:
        raise ValueError("invalid cost basis")
    if cost_basis == "unavailable":
        if cost_usd is not None:
            raise ValueError("unavailable cost must be omitted")
    elif (
        type(cost_usd) not in (float, int)
        or not math.isfinite(cost_usd)
        or not 0 <= cost_usd <= 1000000000
    ):
        raise ValueError(
            "reported or estimated cost requires a finite nonnegative amount"
        )
    _table(store)
    key, _ = project_identity(str(root))
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if session_id is not None:
            if not isinstance(session_id, str):
                raise ValueError("invalid rework session")
            session = conn.execute(
                "SELECT project_key FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if session is None or session["project_key"] != key:
                raise ValueError("rework session must already belong to this project")
        cursor = conn.execute(
            "INSERT INTO workflow_rework(project_key,kind,cause,duration_ms,cost_usd,cost_basis,recorded_at) VALUES (?,?,?,?,?,?,?)",
            (key, kind, cause, duration_ms, cost_usd, cost_basis, utc_now()),
        )
        event_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO workflow_rework_context VALUES (?,?,?,?)",
            (event_id, session_id, rating, phase),
        )
        return {"event_id": event_id, "kind": kind, "cause": cause}


def workflow_report(store, root):
    _table(store)
    key, _ = project_identity(str(root))
    jobs = JobLedger(store).report(key)
    with store.connect() as conn:
        notes = [
            dict(row)
            for row in conn.execute(
                "SELECT r.*, COALESCE(c.rating,'unrated') AS rating, COALESCE(c.phase,'unknown') AS phase "
                "FROM workflow_rework r LEFT JOIN workflow_rework_context c ON c.event_id=r.id WHERE r.project_key=?",
                (key,),
            )
        ]
    receipts = store.verification_receipts(key)

    def costs(basis):
        values = [note["cost_usd"] for note in notes if note["cost_basis"] == basis]
        return sum(values) if values else None

    return {
        "coverage": {
            "managed_background_results": "supported",
            "external_background_results": None,
            "structured_results": "opt_in",
            "environment_binding": "opt_in",
            "automatic_advice": "disabled",
            "acceptance": "explicit_required",
        },
        "jobs": dict(Counter(job["observed_state"] for job in jobs)),
        "verification_attempts": dict(
            Counter(receipt["result"] for receipt in receipts)
        ),
        "rework": {
            "events": len(notes),
            "causes": dict(Counter(note["cause"] for note in notes)),
            "kinds": dict(Counter(note["kind"] for note in notes)),
            "ratings": dict(Counter(note["rating"] for note in notes)),
            "phases": dict(Counter(note["phase"] for note in notes)),
            "recorded_attempt_duration_ms": sum(note["duration_ms"] for note in notes),
            "cost_estimates_usd": costs("estimate"),
            "reported_cost_usd": costs("reported"),
            "cost_unavailable_events": sum(
                note["cost_basis"] == "unavailable" for note in notes
            ),
        },
        "duration_is_wall_time": False,
        "costs_are_user_annotations": True,
        "savings_claim_supported": False,
        "comparison": "use_existing_matched_task_experiment",
    }


def cohort_annotations(store, session_ids):
    """Supplement existing cohort metrics without adding possibly overlapping costs."""
    identities = sorted(set(session_ids))
    notes = []
    with store.connect() as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='workflow_rework_context'"
        ).fetchone():
            return None
        for offset in range(0, len(identities), 500):
            batch = identities[offset : offset + 500]
            marks = ",".join("?" for _ in batch)
            notes.extend(
                dict(row)
                for row in conn.execute(
                    "SELECT r.*, c.rating, c.phase FROM workflow_rework r "
                    "JOIN workflow_rework_context c ON c.event_id=r.id "
                    f"WHERE c.session_id IN ({marks})",
                    batch,
                )
            )

    def costs(basis):
        values = [row["cost_usd"] for row in notes if row["cost_basis"] == basis]
        return sum(values) if values else None

    return {
        "scope": "all_enrolled_session_annotations",
        "events": len(notes),
        "causes": dict(Counter(row["cause"] for row in notes)),
        "ratings": dict(Counter(row["rating"] for row in notes)),
        "phases": dict(Counter(row["phase"] for row in notes)),
        "recorded_attempt_duration_ms": sum(row["duration_ms"] for row in notes),
        "cost_estimates_usd": costs("estimate"),
        "reported_cost_usd": costs("reported"),
        "already_included_in_host_cost": None,
        "added_to_comparison_cost": False,
        "duration_is_wall_time": False,
    }
