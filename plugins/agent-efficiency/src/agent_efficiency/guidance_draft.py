"""Draft proposals from explicit note entries without model calls or writes."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

from agent_efficiency.guidance import MAX_FILE_BYTES, validate_candidates


def draft(note: Path, cwd: Path, expires: str) -> dict:
    if date.fromisoformat(expires) <= date.today():
        raise ValueError("expiry must be a future date")
    if note.is_symlink() or not note.is_file():
        raise ValueError("note must be a regular file")
    with note.open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("note exceeds 32768 bytes")
    version = hashlib.sha256(raw).hexdigest()[:16]
    active = False
    found = False
    suggestions = []
    for line in raw.decode("utf-8").splitlines():
        if line == "## Search locations":
            if found:
                raise ValueError("duplicate Search locations section")
            active = found = True
            continue
        if line.startswith("#"):
            active = False
        if not active or not line.strip():
            continue
        match = re.fullmatch(
            r"- ([a-z][a-z0-9]{2,31}): ([A-Za-z0-9_][A-Za-z0-9_./-]*)", line
        )
        if not match:
            raise ValueError(
                "expected '- word: relative/directory' in Search locations"
            )
        term, target = match.groups()
        suggestions.append(
            {
                "kind": "search-start",
                "target": target,
                "terms": [term],
                "title": f"Start {term} searches in its component",
                "reason": f"The note explicitly maps {term} to this search location.",
                "source": f"{note.name} (note digest {version})",
                "expires": expires,
            }
        )
    if not suggestions:
        raise ValueError("note has no explicit Search locations entries")
    data = {"schema": 1, "suggestions": suggestions}
    validate_candidates(data, cwd)
    return data
