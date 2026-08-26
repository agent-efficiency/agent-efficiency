"""Small JSON and hashing helpers for bundled guidance data."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ContractViolation(ValueError):
    """A stable failure code plus a human-readable contract error."""

    code: str
    path: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code} at {self.path}: {self.detail}"


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes for hashing."""

    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_json_value", "$", str(exc)) from exc
    return rendered.encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Return a prefixed SHA-256 digest of canonical JSON."""

    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def read_json_object(path: str | Path) -> dict[str, Any]:
    """Read one UTF-8 JSON object and reject duplicate keys."""

    source = Path(path)

    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise ContractViolation(
                    "duplicate_property",
                    str(source),
                    f"property {key!r} appears more than once",
                )
            document[key] = value
        return document

    try:
        with source.open("r", encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=no_duplicate_keys)
    except UnicodeDecodeError as exc:
        raise ContractViolation(
            "invalid_encoding",
            str(source),
            "JSON must be UTF-8",
        ) from exc
    except json.JSONDecodeError as exc:
        raise ContractViolation("invalid_json", str(source), str(exc)) from exc
    if not isinstance(value, dict):
        raise ContractViolation("wrong_type", str(source), "root must be an object")
    return value
