"""Human and machine-readable summaries of observed efficiency signals."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_efficiency.experiments import (
    local_capability_measurement,
    local_runtime_health,
)
from agent_efficiency.store import Store, project_identity
from agent_efficiency.verification.config import load_project_config
from agent_efficiency.verification.receipts import receipt_state
from agent_efficiency.verification.state import workspace_state


def build_report(
    store: Store, days: int = 30, *, session_id: str | None = None
) -> dict[str, Any]:
    data = (
        _session_activity(store, session_id, days)
        if session_id
        else store.aggregate(days)
    )
    data["scope"] = "session" if session_id else "cohort"
    data["session_id"] = session_id
    tools = int(data.get("tools") or 0)
    failures = int(data.get("failures") or 0)
    edited = int(data.get("edited_turns") or 0)
    verified = int(data.get("verified_edited_turns") or 0)
    data["failure_rate"] = failures / tools if tools else None
    data["verification_rate"] = verified / edited if edited else None
    data["capability_layer"] = local_capability_measurement(
        store, days=days, session_id=session_id
    )
    data["runtime_health"] = local_runtime_health(
        store, days=days, session_id=session_id
    )
    data["interventions"] = store.intervention_summary(days, session_id=session_id)
    data["vault"] = store.vault_summary(days, session_id=session_id)
    data["claim_boundary"] = (
        "Observed operational signals only. Savings require a comparable baseline."
    )
    return data


def format_report(data: dict[str, Any]) -> str:
    failure_rate = data.get("failure_rate")
    verification_rate = data.get("verification_rate")
    cost = float(data.get("cost_usd") or 0)
    exact_metrics = bool(
        data.get("input_tokens") or data.get("output_tokens") or data.get("cost_usd")
    )
    lines = [
        f"Agent Efficiency report: last {data['days']} days",
        (
            f"Sessions {data['sessions']} | turns {data['turns']} | "
            f"tools {data['tools']} | nudges {data['nudges']}"
        ),
        (
            f"Failures {data['failures']}"
            + (
                f" ({failure_rate:.1%} of observed tools)"
                if failure_rate is not None
                else ""
            )
            + f" | repeated-action signals {data['repeated_actions']} | "
            f"broad scans {data['broad_scans']}"
        ),
        (
            f"Edited turns verified {data['verified_edited_turns']}/"
            f"{data['edited_turns']}"
            + (f" ({verification_rate:.1%})" if verification_rate is not None else "")
        ),
        (
            f"Subagents {data['subagents']} | compactions {data['compactions']} | "
            f"validation calls {data['validations']}"
        ),
    ]
    if exact_metrics:
        lines.append(
            f"Host-reported totals: ${cost:.4f} | input {data['input_tokens']} | "
            f"output {data['output_tokens']} | cache reads {data['cache_read_tokens']}"
        )
    else:
        lines.append(
            "Host-reported token and cost metrics: unavailable "
            "(lifecycle counts are still valid)."
        )
    if data.get("by_mode"):
        mode_bits = [
            f"{row['mode']}={row['sessions']} sessions" for row in data["by_mode"]
        ]
        lines.append("Modes: " + ", ".join(mode_bits))
    capability = data["capability_layer"]
    utility = capability["utility"]
    selection = capability["runtime"].get("selection")
    startup = capability["runtime"].get("session-start")
    emitted = capability["receipts"].get("emitted", {"count": 0, "emitted_chars": 0})
    lines.append(
        f"Capability guidance: {emitted['count']} envelopes | "
        f"{emitted['emitted_chars']} context characters | "
        f"{utility['ratings']} utility ratings"
    )
    if selection or startup:
        overhead = []
        if selection:
            overhead.append(
                f"selection path p95 {selection['p95_ms']:.4f} ms "
                f"({selection['samples']} samples)"
            )
        if startup:
            overhead.append(
                f"startup p95 {startup['p95_ms']:.4f} ms ({startup['samples']} samples)"
            )
        lines.append("Measured local layer runtime: " + " | ".join(overhead))
    health = data["runtime_health"]
    health_bits = []
    for operation in ("normal-hook", "session-start"):
        sample = health[operation]
        p95 = (
            f"{sample['p95_ms']:.4f} ms"
            if sample["p95_ms"] is not None
            else "unavailable"
        )
        health_bits.append(f"{operation} p95 {p95} ({sample['samples']} samples)")
    lines.append("Full hook runtime: " + " | ".join(health_bits))
    source_gate = capability["guidance_expansion_gate"]
    lines.append(
        f"Guidance expansion gate: {source_gate['decision']} "
        f"({source_gate['ratings']}/{source_gate['minimum_ratings']} ratings)"
    )
    interventions = data["interventions"]
    disposition_bits = [
        f"{name}={count}"
        for name, count in sorted(interventions["dispositions"].items())
    ]
    lines.append("Interventions: " + (", ".join(disposition_bits) or "none recorded"))
    followed = interventions.get("followed_rate")
    disabled = interventions.get("disable_rate")
    false_rate = interventions.get("false_or_unnecessary_rate")
    lines.append(
        "Observed association rates: "
        f"followed={_rate_text(followed)} | disabled={_rate_text(disabled)} | "
        f"false or unnecessary={_rate_text(false_rate)}"
    )
    vault = data.get("vault")
    if vault and vault.get("receipts"):
        lines.append(
            f"Vault context: selected {vault['selected']} | "
            f"emitted {vault['emitted']} ({vault['chars_emitted']} characters, "
            f"project heads {vault['head_chars']}) | "
            f"deferred {vault['deferred']} | unavailable {vault['unavailable']} | "
            f"skipped {vault['skipped']} | withheld {vault['withheld']} | "
            f"degraded {vault['degraded']}"
        )
    lines.append(
        "Claim boundary: observed results are associated with interventions. "
        "They do not establish causation or savings."
    )
    return "\n".join(lines)


def build_explanation(
    store: Store,
    *,
    session_id: str | None = None,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    """Explain the last intervention without exposing stored content."""

    intervention = store.latest_intervention(session_id)
    if not intervention:
        return {"found": False, "claim_boundary": "No intervention was recorded."}
    evidence: list[dict[str, Any]] = []
    root = Path(cwd or Path.cwd())
    try:
        config = load_project_config(root)
        state = workspace_state(config.root, config.digest)
        project_key, _ = project_identity(str(config.root))
        session = store.get_session(str(intervention["session_id"]))
        if not session or str(session["project_key"]) != project_key:
            raise ValueError("current project does not match the intervention")
        for check in config.checks:
            receipts = store.verification_receipts(project_key, check_id=check.check_id)
            status = receipt_state(
                receipts[0] if receipts else None,
                check_digest=check.digest,
                workspace_digest=state.digest,
                max_age_seconds=check.max_age_seconds,
            )
            evidence.append({"check_id": check.check_id, "state": status})
    except (OSError, ValueError):
        evidence.append({"check_id": None, "state": "inconclusive"})
    return {
        "found": True,
        "intervention": intervention,
        "evidence": evidence,
        "claim_boundary": (
            "The next events are associated observations. They do not prove "
            "that the intervention caused the result."
        ),
    }


def format_explanation(data: dict[str, Any]) -> str:
    if not data.get("found"):
        return "Agent Efficiency has no recorded intervention to explain."
    item = data["intervention"]
    outcomes = item.get("outcomes") or []
    outcome_text = ", ".join(row["outcome"] for row in outcomes) or "none observed"
    evidence = data.get("evidence") or []
    evidence_text = ", ".join(
        f"{row.get('check_id') or 'project'}={row['state']}" for row in evidence
    )
    feedback = item.get("feedback")
    feedback_text = feedback["judgment"] if feedback else "not rated"
    return "\n".join(
        [
            f"Intervention {item['id']}: {item['policy_id']} revision {item['policy_revision']}",
            f"Observed fact class: {item['triggering_fact']}",
            f"Requested effect: {item['requested_effect']}",
            f"Rendered effect: {item['rendered_effect']} via {item['host_capability']}",
            f"Host disposition: {item['disposition']}",
            f"Observed next results: {outcome_text}",
            f"Current evidence: {evidence_text}",
            f"Human judgment: {feedback_text}",
            f"Claim boundary: {data['claim_boundary']}",
        ]
    )


def _session_activity(store: Store, session_id: str, days: int) -> dict[str, Any]:
    session = store.get_session(session_id)
    if not session:
        raise ValueError("session does not exist")
    with store.connect() as conn:
        turns = conn.execute(
            """
            SELECT COUNT(*) AS turns,
                   SUM(CASE WHEN code_edit_count > 0 THEN 1 ELSE 0 END) AS edited,
                   SUM(CASE WHEN code_edit_count > 0 AND validation_count > 0
                            THEN 1 ELSE 0 END) AS verified
              FROM turns WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
    return {
        "days": days,
        "sessions": 1,
        "turns": int(turns["turns"] or 0),
        "tools": int(session["tool_count"] or 0),
        "failures": int(session["failure_count"] or 0),
        "nudges": int(session["nudge_count"] or 0),
        "compactions": int(session["compaction_count"] or 0),
        "subagents": int(session["subagent_count"] or 0),
        "edits": int(session["edit_count"] or 0),
        "validations": int(session["validation_count"] or 0),
        "repeated_actions": int(session["repeated_action_count"] or 0),
        "broad_scans": int(session["broad_scan_count"] or 0),
        "cost_usd": float(session["cost_usd"] or 0),
        "input_tokens": int(session["input_tokens"] or 0),
        "output_tokens": int(session["output_tokens"] or 0),
        "cache_read_tokens": int(session["cache_read_tokens"] or 0),
        "cache_creation_tokens": int(session["cache_creation_tokens"] or 0),
        "edited_turns": int(turns["edited"] or 0),
        "verified_edited_turns": int(turns["verified"] or 0),
        "by_mode": [{"mode": session["mode"], "sessions": 1}],
    }


def _rate_text(value: float | None) -> str:
    return f"{value:.1%}" if value is not None else "not enough data"
