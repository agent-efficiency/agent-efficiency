"""Optional environment-bound check identities; sensitive values never leave memory."""

from __future__ import annotations

import json
import os

from agent_efficiency.digests import VALID, keyed_digest, load_key, read_key
from agent_efficiency.store import Store
from agent_efficiency.verification.config import CheckDefinition


def check_identity(check: CheckDefinition, store: Store) -> str | None:
    if not check.env_fingerprint:
        return check.digest
    # Never use the public fallback key for low-entropy environment secrets.
    load_key(store.paths.root)
    key = read_key(store.paths.root)
    if key.state != VALID:
        return None
    values = {name: os.environ.get(name) for name in check.env_fingerprint}
    if any(value is not None and len(value) > 32768 for value in values.values()):
        return None
    value = json.dumps(
        [check.digest, values], sort_keys=True, separators=(",", ":")
    ).encode()
    return "hmac-sha256:" + keyed_digest(
        key.key, b"agent-efficiency/v1/verification-environment", value
    )
