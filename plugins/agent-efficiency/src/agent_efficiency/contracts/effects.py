"""Canonical effects selected by policy and rendered by host adapters."""

from __future__ import annotations

from dataclasses import dataclass


EFFECT_NAMES = (
    "none",
    "add_context",
    "notify",
    "deny_action",
    "continue_turn",
    "replace_action",
)


@dataclass(frozen=True, slots=True)
class CanonicalEffect:
    """A host-neutral policy result."""

    effect: str
    policy_id: str
    policy_revision: str
    observed_fact: str
    agent_message: str
    user_message: str | None = None
    maximum_repeats: int = 1
    required_capability: str | None = None
    fallback_effect: str = "none"
    measurement_label: str = ""

    def __post_init__(self) -> None:
        if self.effect not in EFFECT_NAMES:
            raise ValueError(f"unknown canonical effect: {self.effect}")
        if self.fallback_effect not in EFFECT_NAMES:
            raise ValueError(
                f"unknown canonical fallback effect: {self.fallback_effect}"
            )
        if self.maximum_repeats < 0:
            raise ValueError("maximum_repeats cannot be negative")
