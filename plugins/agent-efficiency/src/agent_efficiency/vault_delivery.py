"""Deliver vault context through a host adapter and record a closed receipt.

Vault context is its own channel. It does not pass through the guidance path,
so it never counts against the advice budget and never appears in intervention
measurement. Note text goes from the vault into the host output and nowhere
else: the receipt holds counts, two digests, and closed codes.

Lifecycle causes:

* ``new``: select, render, deliver.
* ``resume``: deliver only if the rendered text changed since the last delivery
  in this session.
* ``compact``: deliver once per compaction, because the earlier context is
  gone. A host can send more than one event after a compaction, and the first
  one delivers.
* ``request``: an explicit prompt; always reselect and deliver.
* ``deferred``: a host with no session start delivers at the first event that
  can carry context.

A ``request`` or ``compact`` delivery carries one line after the vault header
saying it replaces earlier vault context. The line is added here, not in the
renderer, so ``vault show`` and the digest cover only the rendered text.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_efficiency.adapters import adapter_for
from agent_efficiency.adapters.base import BaseAdapter
from agent_efficiency.contracts.effects import CanonicalEffect
from agent_efficiency.store import Store
from agent_efficiency.vault.prepare import Prepared, prepare, stopped
from agent_efficiency.vault.render import HEADER

AGENT_PREFIX = "Agent Efficiency: "
SESSION_CAUSES = {
    "startup": "new",
    "clear": "new",
    "resume": "resume",
    "compact": "compact",
}
EMITTED = frozenset({"delivered", "truncated", "deferred"})
# A reselection can load a different project than earlier in the session, so
# the model is told which context is current.
RESELECTED = frozenset({"request", "compact"})
REPLACES = (
    "This vault context replaces any vault context loaded earlier in this session."
)
DEGRADED_NOTICE = (
    "Vault context is unavailable this session ({reason}). "
    "Run agent-efficiency vault show to see why."
)


@dataclass(frozen=True)
class Delivery:
    output: dict[str, Any] | None
    disposition: str
    reason: str | None


def session_cause(payload: dict[str, Any]) -> str:
    """Map a host's session-start source to a lifecycle cause. Unknown is new."""

    source = str(payload.get("source") or "").strip().casefold()
    return SESSION_CAUSES.get(source, "new")


def strip_agent_prefix(rendered: dict[str, Any] | None) -> dict[str, Any] | None:
    """Remove the guidance label from context that is not guidance."""

    if not rendered:
        return rendered
    context = rendered.get("hookSpecificOutput")
    if isinstance(context, dict):
        _strip(context, "additionalContext")
    _strip(rendered, "additional_context")
    return rendered


def deliver_vault_context(
    store: Store,
    session_id: str,
    host: str,
    event: str,
    cwd: Path,
    *,
    cause: str,
    mode: str,
    lead: str = "",
) -> Delivery:
    """Select, render, and deliver. ``lead`` is host text placed before the vault."""

    try:
        prepared = prepare(cwd, store.paths.root)
    except Exception:  # noqa: BLE001
        # Vault context is optional and must never stop a session. The
        # specific error types are already classified inside prepare.
        prepared = stopped("degraded", "parse_error")
    adapter = adapter_for(host)
    can_add = "add_context" in adapter.capabilities.get(event, ())

    if prepared.status == "unavailable" and prepared.reason == "no_trees":
        # No registered tree means the feature is not in use, so there is
        # nothing to record.
        return Delivery(None, "unavailable", "no_trees")
    if cause == "compact" and not _compaction_due(store, session_id):
        return _finish(store, session_id, cause, prepared, "skipped", "unchanged")
    if prepared.status == "unavailable":
        return _finish(store, session_id, cause, prepared, "unavailable")
    if prepared.status == "degraded":
        output = None
        if mode != "observe" and can_add:
            notice = DEGRADED_NOTICE.format(reason=prepared.reason)
            output = _render(adapter, event, lead, notice)
        return _finish(store, session_id, cause, prepared, "degraded", output=output)
    if cause == "resume" and _unchanged(store, session_id, prepared.digest):
        return _finish(store, session_id, cause, prepared, "skipped", "unchanged")
    if mode == "observe":
        return _finish(store, session_id, cause, prepared, "withheld", "observe_mode")
    if not can_add:
        return _finish(
            store, session_id, cause, prepared, "unavailable", "host_unsupported"
        )
    rendered = prepared.rendered
    text = _replacing(rendered.text) if cause in RESELECTED else rendered.text
    output = _render(adapter, event, lead, text)
    if output is None:
        return _finish(
            store, session_id, cause, prepared, "unavailable", "host_unsupported"
        )
    if cause == "deferred":
        disposition = "deferred"
    elif rendered.truncated or rendered.omitted:
        disposition = "truncated"
    else:
        disposition = "delivered"
    return _finish(
        store,
        session_id,
        cause,
        prepared,
        disposition,
        output=output,
        emitted=len(text),
    )


def _render(
    adapter: BaseAdapter, event: str, lead: str, text: str
) -> dict[str, Any] | None:
    message = "\n\n".join(part for part in (lead, text) if part)
    effect = CanonicalEffect(
        effect="add_context",
        policy_id="vault.context",
        policy_revision="1",
        observed_fact="vault_context",
        agent_message=message,
        required_capability="add_context",
        measurement_label="vault-context",
    )
    try:
        return strip_agent_prefix(adapter.render(effect, native_event=event))
    except (KeyError, TypeError, ValueError):
        return None


def _replacing(text: str) -> str:
    """Put the replacement line directly after the vault header."""

    if text.startswith(HEADER):
        return f"{HEADER}\n{REPLACES}{text[len(HEADER) :]}"
    return f"{REPLACES}\n\n{text}"


def _compaction_due(store: Store, session_id: str) -> bool:
    try:
        return store.vault_compaction_due(session_id)
    except (OSError, ValueError, sqlite3.Error):
        # A second copy is better than no context after a compaction.
        return True


def _unchanged(store: Store, session_id: str, digest: str) -> bool:
    try:
        last = store.latest_vault_delivery(session_id)
    except (OSError, ValueError, sqlite3.Error):
        return False
    return last is not None and last["payload_digest"] == digest


def _finish(
    store: Store,
    session_id: str,
    cause: str,
    prepared: Prepared,
    disposition: str,
    reason: str | None = None,
    *,
    output: dict[str, Any] | None = None,
    emitted: int = 0,
) -> Delivery:
    """Record the receipt. ``emitted`` is the length of the vault text sent."""

    code = reason if reason is not None else prepared.reason
    rendered = prepared.rendered
    fields: dict[str, Any] = {
        "cause": cause,
        "vault_revision": prepared.revision,
        "payload_digest": prepared.digest,
        "notes_selected": rendered.notes_selected if rendered else 0,
        "head_chars": rendered.head_chars if rendered else 0,
        "chars_emitted": emitted if disposition in EMITTED else 0,
        "chars_omitted": rendered.chars_omitted if rendered else 0,
        "disposition": disposition,
        "reason_code": code,
    }
    if cause == "deferred":
        # Several tool results can race to make the deferred delivery. Only
        # the one that records the receipt sends its output. A claim that
        # cannot be recorded sends nothing, so the next tool result retries.
        try:
            claimed = store.record_vault_receipt_once(
                session_id, exclude_cause="request", **fields
            )
        except (OSError, ValueError, sqlite3.Error):
            return Delivery(None, disposition, code)
        if claimed is None:
            return Delivery(None, "skipped", "unchanged")
        return Delivery(output, disposition, code)
    try:
        store.record_vault_receipt(session_id, **fields)
    except (OSError, ValueError, sqlite3.Error):
        # Measurement cannot change what the session receives.
        pass
    return Delivery(output, disposition, code)


def _strip(container: dict[str, Any], key: str) -> None:
    value = container.get(key)
    if isinstance(value, str) and value.startswith(AGENT_PREFIX):
        container[key] = value[len(AGENT_PREFIX) :]
