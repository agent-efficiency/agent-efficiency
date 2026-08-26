"""Claude Code status-line telemetry adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_efficiency.hook import model_name
from agent_efficiency.store import Store


def process_statusline(payload: dict[str, Any], store: Store) -> str:
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return "AE unavailable"
    cwd = str(payload.get("cwd") or Path.cwd())
    session = store.ensure_session(
        session_id,
        host="claude",
        cwd=cwd,
        model=model_name(payload.get("model")),
    )
    context = payload.get("context_window")
    context = context if isinstance(context, dict) else {}
    current = context.get("current_usage")
    current = current if isinstance(current, dict) else {}
    cost = payload.get("cost")
    cost = cost if isinstance(cost, dict) else {}
    metrics = {
        "cost_usd": _number(cost.get("total_cost_usd")),
        "input_tokens": _integer(context.get("total_input_tokens")),
        "output_tokens": _integer(context.get("total_output_tokens")),
        "cache_read_tokens": _integer(current.get("cache_read_input_tokens")),
        "cache_creation_tokens": _integer(current.get("cache_creation_input_tokens")),
        "context_used_percent": _number(context.get("used_percentage")),
        "lines_added": _integer(cost.get("total_lines_added")),
        "lines_removed": _integer(cost.get("total_lines_removed")),
    }
    if session.get("mode") != "off":
        store.record_status_sample(session_id, metrics)
        session = store.get_session(session_id) or session
    mode = str(session.get("mode") or "advise")
    cost_value = metrics["cost_usd"]
    context_value = metrics["context_used_percent"]
    cost_text = f"${cost_value:.3f}" if cost_value is not None else "n/a"
    context_text = (
        f"{context_value:.0f}% ctx" if context_value is not None else "ctx n/a"
    )
    return (
        f"AE {mode} | {cost_text} | {context_text} | "
        f"{session.get('tool_count', 0)} tools/"
        f"{session.get('failure_count', 0)} fail | "
        f"{session.get('validation_count', 0)} checks"
    )


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _integer(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None
