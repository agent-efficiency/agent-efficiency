"""Local, deterministic guidance-pack pinning, selection, and explanation."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from agent_efficiency.capability_envelope import (
    MAX_ENVELOPE_CHARS,
    render_advisory_envelope,
)
from agent_efficiency.capability_pack import (
    adapt_capability_pack,
    capability_pack_digest,
    load_bundled_capability_pack,
    load_capability_pack,
    write_capability_pack,
)
from agent_efficiency.models import TaskFacts
from agent_efficiency.store import Store


MIN_SELECTION_SCORE = 8


class CapabilityDecision:
    __slots__ = (
        "card_id",
        "score",
        "match_reason",
        "envelope",
        "suppression_reason",
    )

    def __init__(
        self,
        *,
        card_id: str,
        score: int,
        match_reason: str,
        envelope: str | None,
        suppression_reason: str | None,
    ) -> None:
        self.card_id = card_id
        self.score = score
        self.match_reason = match_reason
        self.envelope = envelope
        self.suppression_reason = suppression_reason


def prepare_current_capability_pack(
    store: Store,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Retain the bundled pack and return immutable pin metadata."""

    pack = load_bundled_capability_pack()
    digest = capability_pack_digest(pack)
    path = _pack_path(store, digest)
    if not path.is_file():
        write_capability_pack(path, pack)
    return pack, {
        "pack_id": str(pack["pack_id"]),
        "pack_digest": digest,
        "sequence": int(pack["sequence"]),
    }


def ensure_session_capability_pack(
    store: Store,
    session_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the immutable pack pinned to a session."""

    pin = store.get_session_capability_pack(session_id)
    if pin:
        digest = str(pin["pack_digest"])
        path = _pack_path(store, digest)
        if path.is_file():
            try:
                pack = load_capability_pack(path)
                if capability_pack_digest(pack) == digest:
                    return pack, pin
            except (OSError, ValueError):
                pass
        bundled = load_bundled_capability_pack()
        if capability_pack_digest(bundled) != digest:
            raise ValueError("session-pinned guidance pack is unavailable")
        write_capability_pack(path, bundled)
        return bundled, pin

    bundled, metadata = prepare_current_capability_pack(store)
    pin = store.pin_capability_pack(
        session_id,
        pack_id=str(metadata["pack_id"]),
        pack_digest=str(metadata["pack_digest"]),
        sequence=int(metadata["sequence"]),
    )
    return bundled, pin


def select_capability(
    pack: Mapping[str, Any],
    prompt: str,
    facts: TaskFacts,
    *,
    host: str,
    event_name: str,
    emitted_card_ids: set[str] | None = None,
    revoked_ids: set[str] | None = None,
    now: datetime | None = None,
) -> CapabilityDecision | None:
    """Select at most one card using only controlled task facts and index terms."""

    normalized = adapt_capability_pack(pack)
    lowered = prompt.casefold()
    matched_by_card: dict[str, list[str]] = {}
    for entry in normalized["retrieval_index"]:
        term = str(entry["term"])
        if not _term_matches(lowered, term):
            continue
        for card_id in entry["card_ids"]:
            matched_by_card.setdefault(str(card_id), []).append(term)
    if not matched_by_card:
        return None

    cards = {str(card["id"]): card for card in normalized["cards"]}
    emitted = emitted_card_ids or set()
    revoked = revoked_ids or set()
    current = now or datetime.now(UTC)
    candidates: list[tuple[int, str, CapabilityDecision]] = []
    for card_id, matched_terms in matched_by_card.items():
        card = cards.get(card_id)
        if card is None:
            continue
        score = _selection_score(card, matched_terms, facts)
        reason = _match_reason(facts, matched_terms)
        suppression = (
            "revoked"
            if card_id in revoked
            else _hard_filter_reason(
                card,
                lowered,
                facts,
                runtime_host=host,
                event_name=event_name,
                emitted=emitted,
                now=current,
            )
        )
        if not suppression and score < MIN_SELECTION_SCORE:
            suppression = "irrelevant"
        envelope = None if suppression else render_advisory_envelope(card, reason)
        if envelope is not None and len(envelope) > MAX_ENVELOPE_CHARS:
            envelope = None
            suppression = "envelope-budget"
        candidates.append(
            (
                score,
                card_id,
                CapabilityDecision(
                    card_id=card_id,
                    score=score,
                    match_reason=reason,
                    envelope=envelope,
                    suppression_reason=suppression,
                ),
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][2]


def capability_status(store: Store, session_id: str | None = None) -> dict[str, Any]:
    """Describe the local bundled guidance state."""

    pack, metadata = prepare_current_capability_pack(store)
    session_pin = store.get_session_capability_pack(session_id) if session_id else None
    receipts = (
        store.knowledge_receipt_summary(session_id)
        if session_id
        else {
            "emitted": 0,
            "observed": 0,
            "suppressed": 0,
            "emitted_chars": 0,
            "suppression_reasons": {},
        }
    )
    return {
        "channel_state": "bundled",
        "active_channel": pack["channel"],
        "pack_id": pack["pack_id"],
        "sequence": pack["sequence"],
        "digest": metadata["pack_digest"],
        "session_id": session_id,
        "session_pin": session_pin,
        "publisher": pack["publication"]["reviewers"],
        "cards": {
            "active": len(pack["cards"]),
            "expired": sum(
                1 for card in pack["cards"] if not _expires_after(card, datetime.now(UTC))
            ),
        },
        "receipts": receipts,
        "verification": {
            "method": "bundled pack and card digests",
            "runtime_network": "none",
        },
    }


def explain_capability(
    store: Store,
    card_id: str,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Explain one bundled card without exposing prompt content."""

    if session_id and store.get_session_capability_pack(session_id):
        pack, pin = ensure_session_capability_pack(store, session_id)
    else:
        pack, _ = prepare_current_capability_pack(store)
        pin = None
    card = next(
        (item for item in pack["cards"] if str(item["id"]) == card_id),
        None,
    )
    if card is None:
        raise KeyError(f"unknown guidance card: {card_id}")
    receipt = (
        store.latest_knowledge_receipt(session_id, card_id) if session_id else None
    )
    return {
        "card_id": card_id,
        "title": card["title"],
        "directive": card["directive"],
        "authority": card["authority"],
        "why_matched": (
            receipt["match_reason"]
            if receipt
            else "No selection receipt is recorded for this session."
        ),
        "selection_receipt": receipt,
        "principles": card["principles"],
        "review": card["review"],
        "publisher": pack["publication"]["reviewers"],
        "verified_at": card["verified_at"],
        "published_at": card["published_at"],
        "expires_at": card["expires_at"],
        "risks": card["risks"],
        "applicability": card["applies_to"],
        "supersedes": card["supersedes"],
        "pack": {
            "pack_id": pack["pack_id"],
            "sequence": pack["sequence"],
            "digest": capability_pack_digest(pack),
            "session_pin": pin,
        },
    }


def _pack_path(store: Store, digest: str) -> Path:
    match = re.fullmatch(r"sha256:([0-9a-f]{64})", digest)
    if not match:
        raise ValueError("invalid guidance pack digest")
    return store.paths.capability_packs / f"{match.group(1)}.json"


def _term_matches(lowered_prompt: str, term: str) -> bool:
    escaped = re.escape(term.casefold())
    return bool(re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", lowered_prompt))


def _selection_score(
    card: Mapping[str, Any],
    matched_terms: list[str],
    facts: TaskFacts,
) -> int:
    tasks = set(str(item) for item in card["applies_to"]["tasks"])
    score = 4 * len(set(matched_terms))
    if facts.task_type in tasks or "any" in tasks:
        score += 4
    if facts.substantial:
        score += 2
    if facts.risk == "high":
        score += 1
    if card["evidence_grade"]["value"] == "external":
        score += 1
    if tasks and "any" not in tasks:
        score += 1
    score -= len(render_advisory_envelope(card, "matched task signals")) // 250
    return score


def _match_reason(facts: TaskFacts, matched_terms: list[str]) -> str:
    scope = "substantial " if facts.substantial else ""
    terms = ", ".join(sorted(set(matched_terms))[:3])
    return f"{scope}{facts.task_type} task matched {terms}"


def _hard_filter_reason(
    card: Mapping[str, Any],
    lowered_prompt: str,
    facts: TaskFacts,
    *,
    runtime_host: str,
    event_name: str,
    emitted: set[str],
    now: datetime,
) -> str | None:
    if card["recommendation"] != "adopt" or card["authority"] != "advisory":
        return "not-adopt"
    if not _expires_after(card, now):
        return "expired"
    applies = card["applies_to"]
    if runtime_host not in set(applies["hosts"]):
        return "incompatible-host"
    if event_name not in set(applies["lifecycle_events"]):
        return "incompatible-event"
    tasks = set(applies["tasks"])
    if facts.task_type not in tasks and "any" not in tasks:
        return "irrelevant-task"
    negative_terms = [
        *card["triggers"]["negative_terms"],
        *card["applies_to"]["excludes"],
    ]
    if any(_term_matches(lowered_prompt, str(term)) for term in negative_terms):
        return "negative-trigger"
    if not facts.substantial:
        return "not-substantial"
    if card["id"] in emitted:
        return "dedupe"
    return None


def _expires_after(card: Mapping[str, Any], now: datetime) -> bool:
    expiry = datetime.fromisoformat(
        str(card["expires_at"]).replace("Z", "+00:00")
    ).astimezone(UTC)
    return expiry > now
