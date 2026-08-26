"""Validation and loading for the bundled Agent Efficiency guidance pack."""

from __future__ import annotations

import copy
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from agent_efficiency import __version__
from agent_efficiency.capability_envelope import (
    MAX_ENVELOPE_CHARS,
    render_advisory_envelope,
)
from agent_efficiency.capability_validation import (
    ContractViolation,
    canonical_json_bytes,
    canonical_sha256,
    read_json_object,
)
from agent_efficiency.paths import PLUGIN_ROOT


BUNDLED_CAPABILITY_PACK = PLUGIN_ROOT / "capabilities" / "bundled" / "base-pack.json"
MAX_PACK_BYTES = 1_048_576
PACK_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
CARD_ID = re.compile(r"^core[.][a-z0-9][a-z0-9.-]{1,94}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
PACK_KEYS = {
    "schema_version",
    "kind",
    "channel",
    "pack_id",
    "sequence",
    "generated_at",
    "valid_until",
    "requires",
    "built_from",
    "publication",
    "card_count",
    "card_digests",
    "cards",
    "retrieval_index",
    "retrieval_tests",
}
CARD_KEYS = {
    "schema_version",
    "id",
    "revision",
    "title",
    "authority",
    "directive",
    "rationale",
    "recommendation",
    "applies_to",
    "triggers",
    "principles",
    "evidence_grade",
    "safety",
    "risks",
    "measurement",
    "review",
    "published_at",
    "verified_at",
    "expires_at",
    "supersedes",
}


def capability_pack_bytes(pack: Mapping[str, Any]) -> bytes:
    """Return the canonical on-disk representation."""

    return canonical_json_bytes(pack) + b"\n"


def capability_pack_digest(pack: Mapping[str, Any]) -> str:
    """Return the digest used for immutable session pins."""

    return canonical_sha256(pack)


def write_capability_pack(path: str | Path, pack: Mapping[str, Any]) -> None:
    """Atomically write a validated canonical pack."""

    validate_capability_pack_document(pack)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(capability_pack_bytes(pack))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass


def load_capability_pack(path: str | Path) -> dict[str, Any]:
    """Load a canonical guidance pack and verify internal digests."""

    source = Path(path)
    if source.stat().st_size > MAX_PACK_BYTES:
        raise ContractViolation(
            "max_bytes",
            str(source),
            f"pack exceeds {MAX_PACK_BYTES} bytes",
        )
    pack = read_json_object(source)
    if source.read_bytes() != capability_pack_bytes(pack):
        raise ContractViolation(
            "noncanonical_pack",
            str(source),
            "pack bytes are not canonical JSON",
        )
    validate_capability_pack_document(pack)
    return pack


def load_bundled_capability_pack() -> dict[str, Any]:
    """Load the reviewed pack installed with the plugin."""

    return load_capability_pack(BUNDLED_CAPABILITY_PACK)


def validate_capability_pack_document(pack: Mapping[str, Any]) -> None:
    """Validate the complete runtime contract for a guidance pack."""

    root = _exact_object(pack, "$", PACK_KEYS)
    if root["schema_version"] != 1 or root["kind"] != "guidance":
        raise ContractViolation(
            "pack_contract",
            "$",
            "guidance pack schema 1 required",
        )
    if root["channel"] != "embedded":
        raise ContractViolation("pack_channel", "$.channel", "embedded required")
    pack_id = _string(root["pack_id"], "$.pack_id", 1, 96)
    if not PACK_ID.fullmatch(pack_id):
        raise ContractViolation("pack_id", "$.pack_id", pack_id)
    _integer(root["sequence"], "$.sequence", 1, 2_147_483_647)
    generated = _timestamp(root["generated_at"], "$.generated_at")
    valid_until = _timestamp(root["valid_until"], "$.valid_until")
    if valid_until <= generated:
        raise ContractViolation(
            "timestamp_order",
            "$.valid_until",
            "must follow generated_at",
        )
    requires = _exact_object(
        root["requires"],
        "$.requires",
        {"agent_efficiency", "pack_schema"},
    )
    _version_range(requires["agent_efficiency"], "$.requires.agent_efficiency")
    _version_range(requires["pack_schema"], "$.requires.pack_schema")
    built_from = _exact_object(
        root["built_from"],
        "$.built_from",
        {"project", "source"},
    )
    if built_from != {"project": "agent-efficiency", "source": "bundled"}:
        raise ContractViolation(
            "pack_origin",
            "$.built_from",
            "project-owned bundled guidance required",
        )
    publication = _exact_object(
        root["publication"],
        "$.publication",
        {"reviewers"},
    )
    reviewers = _string_list(
        publication["reviewers"],
        "$.publication.reviewers",
        1,
        8,
    )
    if reviewers != ["agent-efficiency maintainers"]:
        raise ContractViolation(
            "pack_review",
            "$.publication.reviewers",
            "maintainer review is required",
        )

    cards = _list(root["cards"], "$.cards", 1, 32)
    if root["card_count"] != len(cards):
        raise ContractViolation(
            "card_count",
            "$.card_count",
            "does not match cards",
        )
    by_id: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(cards):
        card = _validate_card(raw, f"$.cards[{index}]")
        card_id = str(card["id"])
        if card_id in by_id:
            raise ContractViolation("duplicate_card", f"$.cards[{index}].id", card_id)
        by_id[card_id] = card
    if list(by_id) != sorted(by_id):
        raise ContractViolation("canonical_order", "$.cards", "sort cards by ID")

    digests = _list(root["card_digests"], "$.card_digests", len(cards), len(cards))
    digest_ids: list[str] = []
    for index, raw in enumerate(digests):
        path = f"$.card_digests[{index}]"
        item = _exact_object(raw, path, {"id", "revision", "content_sha256"})
        card_id = _string(item["id"], f"{path}.id", 3, 96)
        card = by_id.get(card_id)
        if card is None:
            raise ContractViolation("unknown_card", f"{path}.id", card_id)
        if item["revision"] != card["revision"]:
            raise ContractViolation(
                "card_digest_revision",
                f"{path}.revision",
                card_id,
            )
        if item["content_sha256"] != canonical_sha256(card):
            raise ContractViolation(
                "card_digest_mismatch",
                f"{path}.content_sha256",
                card_id,
            )
        digest_ids.append(card_id)
    if digest_ids != sorted(digest_ids):
        raise ContractViolation(
            "canonical_order",
            "$.card_digests",
            "sort digests by card ID",
        )

    index_entries = _list(root["retrieval_index"], "$.retrieval_index", 1, 256)
    terms: list[str] = []
    for index, raw in enumerate(index_entries):
        path = f"$.retrieval_index[{index}]"
        entry = _exact_object(raw, path, {"term", "card_ids"})
        term = _string(entry["term"], f"{path}.term", 1, 80)
        card_ids = _string_list(entry["card_ids"], f"{path}.card_ids", 1, 8)
        if card_ids != sorted(set(card_ids)) or not set(card_ids).issubset(by_id):
            raise ContractViolation(
                "retrieval_index",
                path,
                "card IDs must be known, unique, and sorted",
            )
        terms.append(term)
    if terms != sorted(set(terms)):
        raise ContractViolation(
            "canonical_order",
            "$.retrieval_index",
            "terms must be unique and sorted",
        )

    tests = _list(root["retrieval_tests"], "$.retrieval_tests", 1, 64)
    test_ids: list[str] = []
    for index, raw in enumerate(tests):
        path = f"$.retrieval_tests[{index}]"
        item = _exact_object(
            raw,
            path,
            {"id", "event", "task", "signals", "expected_card_ids"},
        )
        test_ids.append(_string(item["id"], f"{path}.id", 1, 96))
        _string(item["event"], f"{path}.event", 1, 40)
        _string(item["task"], f"{path}.task", 1, 80)
        _string_list(item["signals"], f"{path}.signals", 1, 16)
        expected = _string_list(
            item["expected_card_ids"],
            f"{path}.expected_card_ids",
            1,
            1,
        )
        if not set(expected).issubset(by_id):
            raise ContractViolation("retrieval_test", path, "unknown expected card")
    if test_ids != sorted(set(test_ids)):
        raise ContractViolation(
            "canonical_order",
            "$.retrieval_tests",
            "tests must be unique and sorted",
        )

    for card in cards:
        envelope = render_advisory_envelope(card, "matched task signals")
        if len(envelope) > MAX_ENVELOPE_CHARS:
            raise ContractViolation(
                "envelope_budget",
                f"$.cards[{card['id']}]",
                f"rendered guidance exceeds {MAX_ENVELOPE_CHARS} characters",
            )


def capability_pack_policies(
    pack: Mapping[str, Any],
    *,
    agent_efficiency_version: str = __version__,
) -> list[dict[str, Any]]:
    """Adapt bundled guidance cards to the policy engine."""

    normalized = adapt_capability_pack(pack)
    requires = normalized["requires"]
    if not _version_in_range(
        agent_efficiency_version,
        requires["agent_efficiency"],
    ) or not _version_in_range("1.0.0", requires["pack_schema"]):
        raise ContractViolation(
            "incompatible_pack",
            "$.requires",
            "pack is incompatible with this runtime",
        )
    policies: list[dict[str, Any]] = []
    for card in normalized["cards"]:
        policies.append(
            {
                "id": card["id"],
                "title": card["title"],
                "event": card["applies_to"]["lifecycle_events"][0],
                "kind": "task_guidance",
                "task_types": card["applies_to"]["tasks"],
                "trigger_keywords": card["triggers"]["terms"],
                "message": card["directive"],
                "status": "reviewed",
                "reviewed_by": "agent-efficiency maintainers",
                "reviewed_at": card["published_at"],
                "expires_at": card["expires_at"],
                "constitutional_floor": True,
                "source": {
                    "id": "agent-efficiency",
                    "pack_id": normalized["pack_id"],
                    "pack_sequence": normalized["sequence"],
                    "card_revision": card["revision"],
                    "principle_refs": list(card["principles"]),
                },
            }
        )
    return policies


def adapt_capability_pack(pack: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize the supported bundled pack."""

    if pack.get("schema_version") != 1:
        raise ContractViolation(
            "incompatible_pack",
            "$.schema_version",
            "guidance pack schema 1 required",
        )
    validate_capability_pack_document(pack)
    normalized = copy.deepcopy(dict(pack))
    for card in normalized["cards"]:
        hosts = {
            "claude" if host == "claude-code" else host
            for host in card["applies_to"]["hosts"]
        }
        card["applies_to"]["hosts"] = sorted(hosts)
    return normalized


def _validate_card(raw: Any, path: str) -> Mapping[str, Any]:
    card = _exact_object(raw, path, CARD_KEYS)
    if card["schema_version"] != 1:
        raise ContractViolation("card_schema", path, "schema 1 required")
    card_id = _string(card["id"], f"{path}.id", 3, 96)
    if not CARD_ID.fullmatch(card_id):
        raise ContractViolation("card_id", f"{path}.id", card_id)
    _integer(card["revision"], f"{path}.revision", 1, 2_147_483_647)
    _string(card["title"], f"{path}.title", 1, 160)
    _string(card["directive"], f"{path}.directive", 1, 500)
    _string(card["rationale"], f"{path}.rationale", 1, 1000)
    if card["authority"] != "advisory" or card["recommendation"] != "adopt":
        raise ContractViolation(
            "card_authority",
            path,
            "advisory adopted guidance required",
        )
    applies = _exact_object(
        card["applies_to"],
        f"{path}.applies_to",
        {
            "tasks",
            "lifecycle_events",
            "hosts",
            "languages",
            "frameworks",
            "host_versions",
            "project_traits",
            "requires",
            "excludes",
        },
    )
    _string_list(applies["tasks"], f"{path}.applies_to.tasks", 1, 16)
    _string_list(
        applies["lifecycle_events"],
        f"{path}.applies_to.lifecycle_events",
        1,
        8,
    )
    _string_list(applies["hosts"], f"{path}.applies_to.hosts", 1, 8)
    for key in (
        "languages",
        "frameworks",
        "host_versions",
        "project_traits",
        "requires",
        "excludes",
    ):
        _string_list(applies[key], f"{path}.applies_to.{key}", 0, 32)
    triggers = _exact_object(
        card["triggers"],
        f"{path}.triggers",
        {"terms", "negative_terms"},
    )
    _string_list(triggers["terms"], f"{path}.triggers.terms", 1, 32)
    _string_list(
        triggers["negative_terms"],
        f"{path}.triggers.negative_terms",
        0,
        32,
    )
    principles = _string_list(card["principles"], f"{path}.principles", 1, 8)
    if any(not item.startswith("AE.") for item in principles):
        raise ContractViolation(
            "principle_id",
            f"{path}.principles",
            "Agent Efficiency principle IDs must start with AE.",
        )
    evidence_grade = _exact_object(
        card["evidence_grade"],
        f"{path}.evidence_grade",
        {"value", "reason"},
    )
    if evidence_grade["value"] not in {"project", "external"}:
        raise ContractViolation(
            "evidence_grade",
            f"{path}.evidence_grade.value",
            "project or external required",
        )
    _string(evidence_grade["reason"], f"{path}.evidence_grade.reason", 1, 500)
    safety = _exact_object(
        card["safety"],
        f"{path}.safety",
        {"risk", "may_request_permissions", "may_weaken_constraints"},
    )
    if (
        safety["risk"] not in {"low", "medium"}
        or safety["may_request_permissions"] is not False
        or safety["may_weaken_constraints"] is not False
    ):
        raise ContractViolation("unsafe_capability", f"{path}.safety", "unsafe card")
    _string_list(card["risks"], f"{path}.risks", 1, 16)
    _string(card["measurement"], f"{path}.measurement", 1, 1000)
    review = _exact_object(
        card["review"],
        f"{path}.review",
        {"reviewers", "reason"},
    )
    if review["reviewers"] != ["agent-efficiency maintainers"]:
        raise ContractViolation(
            "card_review",
            f"{path}.review.reviewers",
            "maintainer review required",
        )
    _string(review["reason"], f"{path}.review.reason", 1, 500)
    _timestamp(card["published_at"], f"{path}.published_at")
    _timestamp(card["verified_at"], f"{path}.verified_at")
    _timestamp(card["expires_at"], f"{path}.expires_at")
    _string_list(card["supersedes"], f"{path}.supersedes", 0, 16)
    return card


def _version_in_range(version: str, value: Any) -> bool:
    limits = _exact_object(value, "$.version_range", {"min_inclusive", "max_exclusive"})
    current = _version_tuple(version)
    return (
        _version_tuple(str(limits["min_inclusive"])) <= current
        and current < _version_tuple(str(limits["max_exclusive"]))
    )


def _version_range(value: Any, path: str) -> None:
    limits = _exact_object(value, path, {"min_inclusive", "max_exclusive"})
    if _version_tuple(str(limits["min_inclusive"])) >= _version_tuple(
        str(limits["max_exclusive"])
    ):
        raise ContractViolation("version_range", path, "minimum must precede maximum")


def _version_tuple(value: str) -> tuple[int, int, int, int, int]:
    match = re.fullmatch(
        r"([0-9]+)(?:[.]([0-9]+))?(?:[.]([0-9]+))?"
        r"(?:(a|b|rc)([0-9]+))?(?:[+][A-Za-z0-9.-]+)?",
        value,
    )
    if not match:
        raise ContractViolation("version", "$.version", value)
    stage = {None: 3, "a": 0, "b": 1, "rc": 2}[match.group(4)]
    return (
        int(match.group(1)),
        int(match.group(2) or 0),
        int(match.group(3) or 0),
        stage,
        int(match.group(5) or 0),
    )


def _timestamp(value: Any, path: str) -> datetime:
    text = _string(value, path, 20, 20)
    if not TIMESTAMP.fullmatch(text):
        raise ContractViolation("timestamp", path, text)
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _exact_object(value: Any, path: str, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractViolation("wrong_type", path, "object required")
    observed = set(value)
    if observed != keys:
        raise ContractViolation(
            "object_keys",
            path,
            f"missing={sorted(keys - observed)}, unexpected={sorted(observed - keys)}",
        )
    return value


def _list(value: Any, path: str, minimum: int, maximum: int) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ContractViolation(
            "wrong_type",
            path,
            f"array with {minimum} through {maximum} items required",
        )
    return value


def _string(value: Any, path: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ContractViolation(
            "wrong_type",
            path,
            f"string with {minimum} through {maximum} characters required",
        )
    if value != value.strip() or "\x00" in value:
        raise ContractViolation("invalid_string", path, "clean text required")
    return value


def _string_list(value: Any, path: str, minimum: int, maximum: int) -> list[str]:
    items = _list(value, path, minimum, maximum)
    result = [_string(item, f"{path}[{index}]", 1, 160) for index, item in enumerate(items)]
    if len(result) != len(set(result)):
        raise ContractViolation("duplicate_value", path, "items must be unique")
    return result


def _integer(value: Any, path: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractViolation("wrong_type", path, "integer required")
    if not minimum <= value <= maximum:
        raise ContractViolation(
            "integer_range",
            path,
            f"must be between {minimum} and {maximum}",
        )
    return value
