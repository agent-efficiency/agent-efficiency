"""Host-neutral rendering for bounded advisory capability envelopes."""

from __future__ import annotations

from typing import Any, Mapping


MAX_ENVELOPE_CHARS = 500


def render_advisory_envelope(card: Mapping[str, Any], match_reason: str) -> str:
    reference = str(card["principles"][0])
    revision = int(card["revision"])
    return (
        f"Agent Efficiency guidance: {reference}@{revision}.\n"
        f"Why selected: {match_reason}.\n"
        f"Guidance: {card['directive']}\n"
        "User, repository, security, and host permission rules remain authoritative."
    )
