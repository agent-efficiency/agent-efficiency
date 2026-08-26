"""Privacy-safe intervention rendering and observed-result labels."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Any

from agent_efficiency.adapters import adapter_for
from agent_efficiency.contracts.effects import CanonicalEffect
from agent_efficiency.store import Store


def render_intervention(
    store: Store,
    session_id: str,
    turn_key: str,
    host: str,
    native_event: str,
    effect: CanonicalEffect,
) -> dict[str, Any] | None:
    """Render an effect and record only controlled metadata about the result."""

    rendered: dict[str, Any] | None = None
    disposition = "failed"
    rendered_effect = effect.effect
    try:
        adapter = adapter_for(host)
        available = set(adapter.capabilities.get(native_event, ()))
        unsupported = bool(
            effect.required_capability and effect.required_capability not in available
        )
        active_effect = effect
        if unsupported:
            rendered_effect = effect.fallback_effect
            active_effect = replace(
                effect,
                effect=effect.fallback_effect,
                required_capability=None,
            )
        if active_effect.effect != "none":
            rendered = adapter.render(active_effect, native_event=native_event)
        disposition = (
            "unsupported" if unsupported else ("emitted" if rendered else "unsupported")
        )
    except (KeyError, TypeError, ValueError):
        rendered = None

    try:
        store.record_intervention(
            session_id,
            turn_key,
            policy_id=effect.policy_id,
            policy_revision=effect.policy_revision,
            triggering_fact=effect.observed_fact,
            requested_effect=effect.effect,
            rendered_effect=rendered_effect if rendered else "none",
            host_capability=effect.required_capability or effect.effect,
            disposition=disposition,
            message_chars=len(effect.agent_message),
        )
    except (OSError, ValueError, sqlite3.Error):
        # Measurement cannot change the host effect or block the agent.
        pass
    return rendered
