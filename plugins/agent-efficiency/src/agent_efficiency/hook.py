"""Cross-host lifecycle hook dispatcher."""

from __future__ import annotations

import fnmatch
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from agent_efficiency.adapters import ADAPTERS, adapter_for
from agent_efficiency.capability_envelope import render_advisory_envelope
from agent_efficiency.capability_retrieval import (
    ensure_session_capability_pack,
    prepare_current_capability_pack,
    select_capability,
)
from agent_efficiency.contracts.effects import CanonicalEffect
from agent_efficiency.interventions import render_intervention
from agent_efficiency.models import Nudge
from agent_efficiency.verification.config import load_project_config
from agent_efficiency.verification.receipts import receipt_state
from agent_efficiency.verification.state import workspace_state
from agent_efficiency.policy import PolicyPack, classify_task, classify_tool
from agent_efficiency.store import Store, project_identity


CONTROL_RE = re.compile(
    r"^\s*(?:\$|/)?(?:agent-efficiency(?::agent-efficiency)?)"
    r"\s+(on|off|observe|advise|guard|status|vault|help)\s*[.!]?\s*$",
    re.IGNORECASE,
)


CURSOR_EVENT_NAMES = ADAPTERS["cursor"].native_events
CURSOR_SESSION_CONTEXT = (
    "Agent Efficiency is active for this Cursor session. Preserve the full task "
    "scope. Define acceptance checks before substantial edits. After code edits, "
    "run the smallest behavior check before moving on or stopping. Later Agent "
    "Efficiency messages are advisory checkpoints. Only local metadata is "
    "recorded; prompts, commands, code, outputs, and transcripts are not stored."
)
# Must equal agent_efficiency.vault.config.CONFIG_NAME. It is repeated here so
# a hook event can see that no vault is registered without importing the vault
# modules.
VAULT_CONFIG_NAME = "vault.json"
VAULT_REQUEST_MESSAGES = {
    "no_trees": (
        "No vault tree is registered. Run agent-efficiency vault register "
        "PATH for each tree."
    ),
    "observe_mode": "Observe mode records vault selection but does not add it.",
    "host_unsupported": (
        "This host cannot add context from a prompt. Vault context will be "
        "reloaded at the next successful tool result."
    ),
}


def run_hook(
    payload: dict[str, Any],
    *,
    store: Store | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Process one host hook and record privacy-safe full-path health."""

    hook_started_ns = time.perf_counter_ns()
    active_store = store or Store()
    outcome = "success"
    try:
        return _run_hook(payload, store=active_store, environ=environ)
    except Exception:
        outcome = "failed"
        raise
    finally:
        try:
            host = detect_host(payload, environ=environ)
            normalized = normalize_payload(payload, host=host)
            session_id = str(normalized.get("session_id") or "").strip()
            event_name = str(normalized.get("hook_event_name") or "").strip()
            session = active_store.get_session(session_id) if session_id else None
            if session and event_name:
                turn_key = str(
                    normalized.get("turn_id")
                    or normalized.get("prompt_id")
                    or normalized.get("generation_id")
                    or active_store.latest_turn_key(session_id)
                    or "session"
                )
                active_store.record_runtime_health(
                    session_id,
                    turn_key,
                    event_name=event_name,
                    operation=(
                        "session-start"
                        if event_name == "SessionStart"
                        else "normal-hook"
                    ),
                    duration_us=(time.perf_counter_ns() - hook_started_ns) // 1_000,
                    outcome=outcome,
                )
        except (OSError, ValueError, sqlite3.Error):
            # Health measurement cannot change hook behavior.
            pass


def _run_hook(
    payload: dict[str, Any],
    *,
    store: Store,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Dispatch one host hook after the health wrapper owns the store."""

    hook_started_ns = time.perf_counter_ns()
    host = detect_host(payload, environ=environ)
    adapter = adapter_for(host)
    canonical_event = adapter.to_event(payload)
    if canonical_event is None:
        return None
    payload = normalize_payload(payload, host=host)
    session_id = canonical_event.session_id
    event = str(payload.get("hook_event_name") or "").strip()
    if not event:
        return None
    active_store = store
    model = model_name(payload.get("model"))
    cwd = str(payload.get("cwd") or Path.cwd())
    session = active_store.get_session(session_id)
    session_capability: tuple[dict[str, Any], dict[str, Any]] | None = None
    if session is None or event == "SessionStart":
        try:
            current_pack, metadata = prepare_current_capability_pack(active_store)
            session, pin = active_store.ensure_session_with_capability_pack(
                session_id,
                host=host,
                cwd=cwd,
                model=model,
                pack_id=str(metadata["pack_id"]),
                pack_digest=str(metadata["pack_digest"]),
                sequence=int(metadata["sequence"]),
            )
            if pin.get("pack_digest") == metadata["pack_digest"]:
                session_capability = current_pack, pin
            else:
                session_capability = ensure_session_capability_pack(
                    active_store, session_id
                )
        except (OSError, ValueError):
            # Capability guidance is optional. Operational hooks remain live if
            # the local pack is absent, corrupt, or incompatible.
            session = active_store.ensure_session(
                session_id,
                host=host,
                cwd=cwd,
                model=model,
            )
            try:
                session_capability = ensure_session_capability_pack(
                    active_store, session_id
                )
            except (OSError, ValueError):
                session_capability = None
    mode = _effective_mode(str(session.get("mode") or active_store.default_mode()), cwd)
    if mode == "guard":
        try:
            guard_config = load_project_config(cwd)
            if session.get("guard_policy_digest") != guard_config.digest:
                active_store.set_guard_policy_digest(session_id, guard_config.digest)
                session["guard_policy_digest"] = guard_config.digest
        except ValueError:
            mode = "advise"

    if event == "UserPromptSubmit":
        if mode != "off":
            active_store.record_host_capabilities(
                session_id,
                host=host,
                host_version=canonical_event.host_version,
                event_name=canonical_event.name,
                capabilities=canonical_event.capabilities,
            )
        return _on_prompt(
            payload,
            active_store,
            session_id,
            host,
            mode,
            session_capability=session_capability,
        )

    # Off means no lifecycle telemetry. Session state remains available solely
    # so a later prompt control can turn the same session back on.
    if mode == "off":
        return None

    turn_key = _turn_key(payload, active_store, session_id)
    active_store.record_host_capabilities(
        session_id,
        host=host,
        host_version=canonical_event.host_version,
        event_name=canonical_event.name,
        capabilities=canonical_event.capabilities,
    )
    if event == "SessionStart":
        try:
            active_store.record_capability_runtime(
                session_id,
                turn_key,
                event_name=event,
                operation="session-start",
                duration_us=(time.perf_counter_ns() - hook_started_ns) // 1_000,
            )
        except (OSError, ValueError, sqlite3.Error):
            # Measurement is optional and cannot make startup fail.
            pass
        # The vault modules are only needed at session start, on an explicit
        # request, and for a Cursor deferral. Importing them on every hook
        # event costs about 25 ms, so they load where they are used.
        from agent_efficiency.vault_delivery import (
            deliver_vault_context,
            session_cause,
        )

        lead = (
            CURSOR_SESSION_CONTEXT
            if host == "cursor" and mode in {"advise", "guard"}
            else ""
        )
        delivery = deliver_vault_context(
            active_store,
            session_id,
            host,
            event,
            Path(cwd),
            cause=session_cause(payload),
            mode=mode,
            lead=lead,
        )
        if delivery.output:
            return delivery.output
        if lead:
            return {"additional_context": CURSOR_SESSION_CONTEXT}
        return None
    if event == "PreToolUse":
        return _on_pre_tool(payload, active_store, session_id, turn_key, host, mode)
    if event == "PostToolBatch":
        return _on_post_batch(payload, active_store, session_id, turn_key, host, mode)
    if event in {"PostToolUse", "PostToolUseFailure"}:
        output = _on_post_tool(payload, active_store, session_id, turn_key, host, mode)
        if host == "cursor" and event == "PostToolUse":
            output = _merge_cursor_context(
                output, _deferred_vault(active_store, session_id, cwd, mode)
            )
        return output
    if event in {"PreCompact", "PostCompact"}:
        active_store.record_compaction(session_id, turn_key, event)
        if event == "PostCompact":
            return _compact_vault(active_store, session_id, host, cwd, mode)
        if event == "PreCompact" and mode in {"advise", "guard"}:
            turn = active_store.get_turn(session_id, turn_key) or {}
            if int(turn.get("failure_count") or 0) >= 2:
                nudge = PolicyPack.load(active_store.paths.root).get(
                    "core.compact-failure-loop"
                )
                if nudge and active_store.record_nudge(
                    session_id, turn_key, nudge.policy_id
                ):
                    return _guidance_output(
                        active_store, session_id, turn_key, host, event, nudge
                    )
        return None
    if event == "Stop":
        return _on_stop(payload, active_store, session_id, turn_key, host, mode)
    if event == "SessionEnd":
        active_store.end_session(session_id)
        return None
    return None


def detect_host(
    payload: dict[str, Any], *, environ: Mapping[str, str] | None = None
) -> str:
    env = environ if environ is not None else os.environ
    event = str(payload.get("hook_event_name") or "")
    if (
        payload.get("cursor_version")
        or payload.get("conversation_id")
        or event in CURSOR_EVENT_NAMES
    ):
        return "cursor"
    if env.get("PLUGIN_ROOT") or env.get("PLUGIN_DATA"):
        return "codex"
    if env.get("CLAUDE_PLUGIN_ROOT") or env.get("CLAUDE_PLUGIN_DATA"):
        return "claude"
    model = payload.get("model")
    if isinstance(model, str):
        return "codex"
    if payload.get("prompt_id") or payload.get("last_assistant_message") is not None:
        return "claude"
    return "unknown"


def normalize_payload(payload: dict[str, Any], *, host: str) -> dict[str, Any]:
    """Map a host payload into the shared lifecycle contract."""
    return adapter_for(host).normalize_payload(payload)


def _effective_mode(session_mode: str, cwd: str) -> str:
    if session_mode not in {"advise", "guard"}:
        return session_mode
    try:
        project_guard = load_project_config(cwd).mode == "guard"
    except ValueError:
        project_guard = False
    return "guard" if project_guard else "advise"


def model_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("id", "display_name"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return None


def _on_prompt(
    payload: dict[str, Any],
    store: Store,
    session_id: str,
    host: str,
    mode: str,
    *,
    session_capability: tuple[dict[str, Any], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    prompt = str(payload.get("prompt") or "")
    control = parse_control(prompt)
    if control:
        if control in {"on", "advise", "guard", "observe", "off"}:
            selected = "advise" if control == "on" else control
            guard_policy_digest = None
            if selected == "guard":
                try:
                    guard_config = load_project_config(
                        str(payload.get("cwd") or Path.cwd())
                    )
                except ValueError:
                    guard_config = None
                if guard_config is None or guard_config.mode != "guard":
                    return _control_output(
                        host,
                        'Guard mode requires mode = "guard" in '
                        "agent-efficiency.toml for this repository.",
                    )
                guard_policy_digest = guard_config.digest
            if selected in {"off", "observe"}:
                active_turn = store.latest_turn_key(session_id)
                if active_turn:
                    store.record_intervention_outcome(
                        session_id, active_turn, "user_disabled_advice"
                    )
            invalidated = store.set_mode(
                session_id,
                selected,
                guard_policy_digest=guard_policy_digest,
            )
            suffix = (
                " The enrolled experiment session is now invalidated."
                if invalidated
                else ""
            )
            return _control_output(
                host, f"Agent Efficiency is {selected} for this session.{suffix}"
            )
        if control == "status":
            return _control_output(host, format_session_status(store, session_id))
        if control == "vault":
            if mode == "off":
                return _control_output(
                    host,
                    "Agent Efficiency is off for this session. Turn it on to "
                    "load vault context.",
                )
            from agent_efficiency.vault_delivery import deliver_vault_context

            delivery = deliver_vault_context(
                store,
                session_id,
                host,
                "UserPromptSubmit",
                Path(str(payload.get("cwd") or Path.cwd())),
                cause="request",
                mode=mode,
            )
            if delivery.output:
                return delivery.output
            return _control_output(
                host,
                VAULT_REQUEST_MESSAGES.get(
                    delivery.reason or "",
                    f"Vault context was not added ({delivery.disposition}).",
                ),
            )
        return _control_output(
            host,
            "Agent Efficiency controls: on, observe, advise, guard, off, status, "
            "vault. Modes apply to this session only. vault reloads vault "
            "context.",
        )

    if mode == "off":
        return None
    facts = classify_task(prompt)
    turn_key = _turn_key(payload, store, session_id, new_prompt=True)
    turn_recorded = False
    try:
        selection_started_ns = time.perf_counter_ns()
        capability_pack, pin = session_capability or ensure_session_capability_pack(
            store, session_id
        )
        decision = select_capability(
            capability_pack,
            prompt,
            facts,
            host=host,
            event_name="UserPromptSubmit",
            emitted_card_ids=(
                store.knowledge_selected_ids(session_id)
                if mode == "observe"
                else store.knowledge_emitted_ids(session_id)
            ),
        )
        context_chars = 0
        if decision:
            if decision.suppression_reason:
                store.record_knowledge_suppression(
                    session_id,
                    turn_key,
                    event_name="UserPromptSubmit",
                    pack_id=str(pin["pack_id"]),
                    pack_digest=str(pin["pack_digest"]),
                    card_id=decision.card_id,
                    score=decision.score,
                    match_reason=decision.match_reason,
                    suppression_reason=decision.suppression_reason,
                    prompt_chars=facts.prompt_chars,
                    task_type=facts.task_type,
                    risk=facts.risk,
                )
                turn_recorded = True
            elif decision.envelope:
                if mode == "observe":
                    store.record_knowledge_observation(
                        session_id,
                        turn_key,
                        event_name="UserPromptSubmit",
                        pack_id=str(pin["pack_id"]),
                        pack_digest=str(pin["pack_digest"]),
                        card_id=decision.card_id,
                        score=decision.score,
                        match_reason=decision.match_reason,
                        prompt_chars=facts.prompt_chars,
                        task_type=facts.task_type,
                        risk=facts.risk,
                    )
                    turn_recorded = True
                else:
                    if host == "cursor":
                        store.record_knowledge_pending(
                            session_id,
                            turn_key,
                            event_name="UserPromptSubmit",
                            pack_id=str(pin["pack_id"]),
                            pack_digest=str(pin["pack_digest"]),
                            card_id=decision.card_id,
                            score=decision.score,
                            match_reason=decision.match_reason,
                            prompt_chars=facts.prompt_chars,
                            task_type=facts.task_type,
                            risk=facts.risk,
                        )
                        emitted = False
                    else:
                        emitted, _ = store.record_knowledge_advisory(
                            session_id,
                            turn_key,
                            event_name="UserPromptSubmit",
                            pack_id=str(pin["pack_id"]),
                            pack_digest=str(pin["pack_digest"]),
                            card_id=decision.card_id,
                            score=decision.score,
                            match_reason=decision.match_reason,
                            emitted_chars=len(decision.envelope),
                            prompt_chars=facts.prompt_chars,
                            task_type=facts.task_type,
                            risk=facts.risk,
                        )
                    turn_recorded = True
                    if emitted:
                        context_chars = len(decision.envelope)
        store.record_capability_runtime(
            session_id,
            turn_key,
            event_name="UserPromptSubmit",
            operation="selection",
            duration_us=(time.perf_counter_ns() - selection_started_ns) // 1_000,
            context_chars=context_chars,
        )
        if decision and decision.envelope and context_chars:
            return _knowledge_guidance_output(
                store, session_id, turn_key, host, decision.card_id, decision.envelope
            )
    except (OSError, ValueError, sqlite3.Error):
        # Retrieval failures must never block the agent or disable the
        # operational governor.
        pass

    if not turn_recorded:
        store.start_turn(
            session_id,
            turn_key,
            prompt_chars=facts.prompt_chars,
            task_type=facts.task_type,
            risk=facts.risk,
        )
    if mode not in {"advise", "guard"}:
        return None
    pack = PolicyPack.load(store.paths.root, include_first_party=False)
    nudge = pack.task_guidance(facts, prompt)
    if not nudge or store.has_session_nudge(session_id, nudge.policy_id):
        return None
    if not store.record_nudge(session_id, turn_key, nudge.policy_id):
        return None
    return _guidance_output(
        store, session_id, turn_key, host, "UserPromptSubmit", nudge
    )


def _on_pre_tool(
    payload: dict[str, Any],
    store: Store,
    session_id: str,
    turn_key: str,
    host: str,
    mode: str,
) -> dict[str, Any] | None:
    store.start_turn(
        session_id,
        turn_key,
        prompt_chars=0,
        task_type="unknown",
        risk="normal",
    )
    tool_name = str(payload.get("tool_name") or "unknown")
    facts = classify_tool(tool_name, payload.get("tool_input"))
    store.record_event(
        session_id,
        turn_key,
        event_name="PreToolUse",
        tool_name=facts.tool_name,
        command_class=facts.command_class,
        signature=facts.signature,
        outcome="pending",
        safe_details=facts.safe_details,
        count_tool=True,
        count_subagent=facts.is_subagent,
        count_broad_scan=facts.is_broad_scan,
    )
    if facts.is_validation:
        store.record_intervention_outcome(session_id, turn_key, "check_started")
        store.record_intervention_outcome(
            session_id, turn_key, "suggested_action_observed"
        )
    if mode not in {"advise", "guard"}:
        return None

    policy_id: str | None = None
    failed_repeats = store.event_count(
        session_id,
        turn_key,
        signature=facts.signature,
        outcome="failure",
    )
    identical_count = store.event_count(
        session_id,
        turn_key,
        event_name="PreToolUse",
        signature=facts.signature,
    )
    class_count = store.event_count(
        session_id,
        turn_key,
        event_name="PreToolUse",
        command_class=facts.command_class,
    )
    subagent_count = store.event_count(
        session_id,
        turn_key,
        event_name="PreToolUse",
        command_class="subagent",
    )
    if (
        facts.command_class == "branch-create"
        and store.session_event_count(session_id, command_class="fetch") == 0
    ):
        policy_id = "core.fetch-before-branch"
    elif failed_repeats >= 2:
        policy_id = "core.break-failure-loop"
    elif facts.command_class == "install" and class_count >= 2:
        policy_id = "core.repeat-install"
    elif facts.is_broad_scan:
        policy_id = "core.narrow-search"
    elif facts.is_subagent and subagent_count >= 4:
        policy_id = "core.subagent-sprawl"
    elif identical_count >= 3:
        policy_id = "core.repeat-action"

    nudge: Nudge | None = (
        PolicyPack.load(store.paths.root).get(policy_id) if policy_id else None
    )
    if not nudge or not store.record_nudge(session_id, turn_key, nudge.policy_id):
        return None
    if nudge.policy_id in {
        "core.break-failure-loop",
        "core.repeat-install",
        "core.repeat-action",
    }:
        store.mark_repeated_action(session_id, turn_key)
    return _guidance_output(store, session_id, turn_key, host, "PreToolUse", nudge)


def _on_post_tool(
    payload: dict[str, Any],
    store: Store,
    session_id: str,
    turn_key: str,
    host: str,
    mode: str,
) -> dict[str, Any] | None:
    event = str(payload.get("hook_event_name"))
    tool_name = str(payload.get("tool_name") or "unknown")
    facts = classify_tool(tool_name, payload.get("tool_input"))
    failed = event == "PostToolUseFailure" or _response_failed(payload)
    duration = payload.get("duration_ms")
    duration_ms = int(duration) if isinstance(duration, (int, float)) else None
    counts = store.record_tool_event(
        session_id,
        turn_key,
        event_name=event,
        tool_name=facts.tool_name,
        command_class=facts.command_class,
        signature=facts.signature,
        outcome="failure" if failed else "success",
        duration_ms=duration_ms,
        safe_details=facts.safe_details,
        count_failure=failed,
        count_edit=facts.is_edit and not failed,
        count_code_edit=facts.is_code_edit and not failed,
        count_validation=facts.is_validation and not failed,
        count_subagent=facts.is_subagent,
        count_broad_scan=facts.is_broad_scan,
    )
    if facts.is_validation:
        store.record_intervention_outcome(
            session_id, turn_key, "check_failed" if failed else "check_passed"
        )
    if counts["failures"] >= 2:
        store.record_intervention_outcome(session_id, turn_key, "failure_repeated")
    if mode not in {"advise", "guard"}:
        return None
    if host == "cursor" and event == "PostToolUse":
        pending = store.pending_knowledge_receipt(session_id, turn_key)
        if pending:
            guidance = _deliver_pending_cursor_guidance(
                store, session_id, turn_key, pending
            )
            if guidance:
                return guidance
    if (
        facts.is_code_edit
        and int(
            (store.get_turn(session_id, turn_key) or {}).get("validation_count") or 0
        )
        == 0
    ):
        nudge = PolicyPack.load(store.paths.root).get("core.verify-change")
        if nudge and store.record_nudge(session_id, turn_key, nudge.policy_id):
            return _guidance_output(store, session_id, turn_key, host, event, nudge)
    policy_id = _runtime_policy_id(store, session_id, turn_key, facts, counts=counts)
    if not policy_id:
        return None
    nudge = PolicyPack.load(store.paths.root).get(policy_id)
    if not nudge or not store.record_nudge(session_id, turn_key, nudge.policy_id):
        return None
    if policy_id in {
        "core.break-failure-loop",
        "core.repeat-install",
        "core.repeat-action",
    }:
        store.mark_repeated_action(session_id, turn_key)
    return _guidance_output(store, session_id, turn_key, host, event, nudge)


def _on_post_batch(
    payload: dict[str, Any],
    store: Store,
    session_id: str,
    turn_key: str,
    host: str,
    mode: str,
) -> dict[str, Any] | None:
    calls = payload.get("tool_calls")
    if not isinstance(calls, list):
        return None
    policy_ids: set[str] = set()
    for call in calls:
        if not isinstance(call, dict):
            continue
        tool_name = str(call.get("tool_name") or "unknown")
        facts = classify_tool(tool_name, call.get("tool_input"))
        failed = _batch_response_failed(call.get("tool_response"))
        counts = store.record_tool_event(
            session_id,
            turn_key,
            event_name="PostToolBatch",
            tool_name=facts.tool_name,
            command_class=facts.command_class,
            signature=facts.signature,
            outcome="failure" if failed else "success",
            duration_ms=None,
            safe_details=facts.safe_details,
            count_failure=failed,
            count_edit=facts.is_edit and not failed,
            count_code_edit=facts.is_code_edit and not failed,
            count_validation=facts.is_validation and not failed,
            count_subagent=facts.is_subagent,
            count_broad_scan=facts.is_broad_scan,
        )
        if facts.is_validation:
            store.record_intervention_outcome(
                session_id, turn_key, "check_failed" if failed else "check_passed"
            )
        if counts["failures"] >= 2:
            store.record_intervention_outcome(session_id, turn_key, "failure_repeated")
        if mode in {"advise", "guard"}:
            policy_id = _runtime_policy_id(
                store, session_id, turn_key, facts, counts=counts
            )
            if policy_id:
                policy_ids.add(policy_id)
    if mode not in {"advise", "guard"} or not policy_ids:
        return None
    priority = (
        "core.break-failure-loop",
        "core.repeat-install",
        "core.narrow-search",
        "core.subagent-sprawl",
        "core.repeat-action",
    )
    policy_id = next((item for item in priority if item in policy_ids), None)
    if not policy_id:
        return None
    nudge = PolicyPack.load(store.paths.root).get(policy_id)
    if not nudge or not store.record_nudge(session_id, turn_key, nudge.policy_id):
        return None
    if policy_id in {
        "core.break-failure-loop",
        "core.repeat-install",
        "core.repeat-action",
    }:
        store.mark_repeated_action(session_id, turn_key)
    return _guidance_output(store, session_id, turn_key, host, "PostToolBatch", nudge)


def _deliver_pending_cursor_guidance(
    store: Store,
    session_id: str,
    turn_key: str,
    receipt: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        pack, pin = ensure_session_capability_pack(store, session_id)
        if str(pin["pack_id"]) != str(receipt["pack_id"]) or str(
            pin["pack_digest"]
        ) != str(receipt["pack_digest"]):
            return None
        card = next(
            (
                candidate
                for candidate in pack["cards"]
                if str(candidate.get("id")) == str(receipt["card_id"])
            ),
            None,
        )
        if not isinstance(card, dict):
            return None
        envelope = render_advisory_envelope(card, str(receipt["match_reason"]))
        emitted, _ = store.record_knowledge_advisory(
            session_id,
            turn_key,
            event_name="PostToolUse",
            pack_id=str(receipt["pack_id"]),
            pack_digest=str(receipt["pack_digest"]),
            card_id=str(receipt["card_id"]),
            score=int(receipt["score"]),
            match_reason=str(receipt["match_reason"]),
            emitted_chars=len(envelope),
        )
        if not emitted:
            return None
        store.mark_knowledge_pending_delivered(
            session_id, turn_key, str(receipt["card_id"])
        )
        return _knowledge_guidance_output(
            store,
            session_id,
            turn_key,
            "cursor",
            str(receipt["card_id"]),
            envelope,
            event="PostToolUse",
        )
    except (OSError, ValueError, sqlite3.Error):
        return None


def _deferred_vault(
    store: Store, session_id: str, cwd: str, mode: str
) -> dict[str, Any] | None:
    """Deliver vault context that Cursor could not receive at an earlier event.

    Cursor cloud agents fire no session start. The first tool result is the
    first event that can carry context, so delivery happens there and the
    receipt records it as deferred.

    Cursor also sends no event after a compaction that can carry context. A
    compaction is recorded at ``preCompact``, and the next tool result delivers
    for it, once, as a compact delivery.

    A Cursor prompt cannot carry context either, so an explicit vault request
    is recorded as pending and the next tool result reloads it, once.
    """

    if not _vault_registered(store):
        return None
    try:
        if not store.has_vault_receipt(session_id, exclude_cause="request"):
            cause = "deferred"
        elif store.vault_compaction_pending(session_id):
            cause = "compact"
        elif store.vault_request_pending(session_id):
            cause = "request"
        else:
            return None
    except (OSError, ValueError, sqlite3.Error):
        return None
    from agent_efficiency.vault_delivery import deliver_vault_context

    return deliver_vault_context(
        store,
        session_id,
        "cursor",
        "PostToolUse",
        Path(cwd),
        cause=cause,
        mode=mode,
    ).output


def _compact_vault(
    store: Store, session_id: str, host: str, cwd: str, mode: str
) -> dict[str, Any] | None:
    """Deliver vault context after a compaction if no event has done it yet.

    Some hosts send a session start after a compaction and some send only
    ``PostCompact``. Whichever arrives first delivers.
    """

    if not _vault_registered(store):
        return None
    try:
        due = store.vault_compaction_due(session_id)
    except (OSError, ValueError, sqlite3.Error):
        # A second copy is better than no context after a compaction.
        due = True
    if not due:
        return None
    from agent_efficiency.vault_delivery import deliver_vault_context

    return deliver_vault_context(
        store,
        session_id,
        host,
        "PostCompact",
        Path(cwd),
        cause="compact",
        mode=mode,
    ).output


def _vault_registered(store: Store) -> bool:
    """Return whether a vault tree list may exist, without loading the vault."""

    try:
        return (store.paths.root / VAULT_CONFIG_NAME).is_file()
    except OSError:
        # A tree list that cannot be checked may still be registered. Delivery
        # reports it as degraded, so the failure is visible.
        return True


def _merge_cursor_context(
    first: dict[str, Any] | None, second: dict[str, Any] | None
) -> dict[str, Any] | None:
    if not second:
        return first
    if not first:
        return second
    parts = [
        value
        for value in (first.get("additional_context"), second.get("additional_context"))
        if isinstance(value, str) and value
    ]
    return {**first, "additional_context": "\n\n".join(parts)}


def _runtime_policy_id(
    store: Store,
    session_id: str,
    turn_key: str,
    facts: Any,
    *,
    counts: dict[str, int] | None = None,
) -> str | None:
    if counts is None:
        counts = store.runtime_counts(
            session_id,
            turn_key,
            signature=facts.signature,
            command_class=facts.command_class,
        )
    if counts["failures"] >= 2:
        return "core.break-failure-loop"
    if facts.command_class == "install" and counts["class_count"] >= 2:
        return "core.repeat-install"
    if facts.is_broad_scan:
        return "core.narrow-search"
    if facts.is_subagent and counts["subagents"] >= 4:
        return "core.subagent-sprawl"
    if counts["identical"] >= 3:
        return "core.repeat-action"
    return None


def _on_stop(
    payload: dict[str, Any],
    store: Store,
    session_id: str,
    turn_key: str,
    host: str,
    mode: str,
) -> dict[str, Any] | None:
    turn = store.get_turn(session_id, turn_key)
    store.record_intervention_outcome(session_id, turn_key, "turn_completed")
    store.close_intervention_window(session_id, turn_key)
    store.finish_turn(session_id, turn_key)
    if not turn:
        return None
    if bool(payload.get("stop_hook_active")) or int(payload.get("loop_count") or 0) > 0:
        return None
    if int(turn.get("code_edit_count") or 0) <= 0:
        return None

    if mode == "guard":
        guard = _guard_stop_effect(payload, store, session_id, turn_key)
        if guard:
            return render_intervention(store, session_id, turn_key, host, "Stop", guard)
        return None

    if mode != "advise":
        return None
    if int(turn.get("validation_count") or 0) > 0:
        return None
    nudge = PolicyPack.load(store.paths.root).get("core.verify-change")
    if not nudge or not store.record_nudge(session_id, turn_key, nudge.policy_id):
        return None
    return _guidance_output(store, session_id, turn_key, host, "Stop", nudge)


def _guard_stop_effect(
    payload: dict[str, Any],
    store: Store,
    session_id: str,
    turn_key: str,
) -> CanonicalEffect | None:
    cwd = str(payload.get("cwd") or Path.cwd())
    try:
        config = load_project_config(cwd)
    except ValueError:
        return None
    if config.mode != "guard":
        return None
    session = store.get_session(session_id) or {}
    if session.get("guard_policy_digest") != config.digest:
        return None
    state = workspace_state(config.root, config.digest)
    project_key, _ = project_identity(str(config.root))
    required = [
        check
        for check in config.checks
        if "code" in check.required_for
        and _check_applies_to_paths(check, state.changed_paths)
    ]
    for check in required:
        receipts = store.verification_receipts(project_key, check_id=check.check_id)
        status = receipt_state(
            receipts[0] if receipts else None,
            check_digest=check.digest,
            workspace_digest=state.digest,
            max_age_seconds=check.max_age_seconds,
        )
        if status == "current":
            continue
        policy_id = f"guard.required-check-{status}"
        if not store.record_nudge(session_id, turn_key, policy_id):
            return None
        message = (
            f"Required check {check.check_id} is {status} for the current "
            f"workspace. Run agent-efficiency check {check.check_id}, then "
            "review the result before stopping."
        )
        return CanonicalEffect(
            effect="continue_turn",
            policy_id=policy_id,
            policy_revision="1",
            observed_fact=f"required check is {status}",
            agent_message=message,
            maximum_repeats=1,
            required_capability="continue_turn",
            fallback_effect="notify",
            measurement_label="guard-required-check",
        )
    return None


def _check_applies_to_paths(check: Any, paths: tuple[str, ...] | None) -> bool:
    if not check.applies_to or not paths:
        return True
    return any(
        fnmatch.fnmatchcase(path, pattern)
        or (pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]))
        for path in paths
        for pattern in check.applies_to
    )


def parse_control(prompt: str) -> str | None:
    match = CONTROL_RE.fullmatch(prompt)
    return match.group(1).casefold() if match else None


def format_session_status(store: Store, session_id: str) -> str:
    session = store.get_session(session_id)
    if not session:
        return "Agent Efficiency has no state for this session."
    cost = session.get("cost_usd")
    cost_text = f"${float(cost):.4f}" if cost is not None else "host metric unavailable"
    return (
        f"Agent Efficiency: {session['mode']} | turns {session['turn_count']} | "
        f"tools {session['tool_count']} | failures {session['failure_count']} | "
        f"nudges {session['nudge_count']} | cost {cost_text}."
    )


def _turn_key(
    payload: dict[str, Any],
    store: Store,
    session_id: str,
    *,
    new_prompt: bool = False,
) -> str:
    for key in ("turn_id", "prompt_id"):
        value = payload.get(key)
        if value:
            return str(value)
    if not new_prompt:
        latest = store.latest_turn_key(session_id)
        if latest:
            return latest
    session = store.get_session(session_id) or {}
    return f"turn-{int(session.get('turn_count') or 0) + 1}"


def _response_failed(payload: dict[str, Any]) -> bool:
    response = payload.get("tool_response")
    if not isinstance(response, dict):
        response = payload.get("tool_result")
    if not isinstance(response, dict):
        return False
    if response.get("success") is False or response.get("is_error") is True:
        return True
    exit_code = response.get("exit_code", response.get("exitCode"))
    if isinstance(exit_code, int) and exit_code != 0:
        return True
    status = str(response.get("status") or "").casefold()
    return status in {"failed", "failure", "error"}


def _batch_response_failed(response: Any) -> bool:
    if isinstance(response, dict):
        return _response_failed({"tool_response": response})
    if isinstance(response, list):
        for block in response:
            if not isinstance(block, dict):
                continue
            if block.get("is_error") is True or block.get("type") in {
                "error",
                "tool_error",
            }:
                return True
            text = block.get("text")
            if isinstance(text, str) and _failure_text(text):
                return True
        return False
    return isinstance(response, str) and _failure_text(response)


def _failure_text(value: str) -> bool:
    sample = value.lstrip()[:500].casefold()
    return bool(
        re.search(
            r"^(error|tool error|command failed|failed:)\b|"
            r"\bexited with (?:code|status(?: code)?) [1-9]\d*\b",
            sample,
        )
    )


def _control_output(host: str, message: str) -> dict[str, Any]:
    effect = CanonicalEffect(
        effect="notify",
        policy_id="control.session-mode",
        policy_revision="1",
        observed_fact="explicit control prompt",
        agent_message=message,
        user_message=message,
        measurement_label="session-control",
    )
    rendered = adapter_for(host).render(effect, native_event="UserPromptSubmit")
    return rendered or {}


def _guidance_output(
    store: Store,
    session_id: str,
    turn_key: str,
    host: str,
    event: str,
    nudge: Nudge,
) -> dict[str, Any] | None:
    effect = CanonicalEffect(
        effect="add_context",
        policy_id=nudge.policy_id,
        policy_revision="1",
        observed_fact=_policy_fact_class(nudge.policy_id),
        agent_message=nudge.message,
        required_capability="add_context",
        measurement_label="runtime-guidance",
    )
    return render_intervention(store, session_id, turn_key, host, event, effect)


def _policy_fact_class(policy_id: str) -> str:
    if policy_id.startswith("guard.required-check-"):
        return "required_check_state"
    return {
        "core.fetch-before-branch": "branch_without_observed_fetch",
        "core.compact-failure-loop": "compaction_during_failure_loop",
        "core.break-failure-loop": "repeated_failure",
        "core.repeat-install": "repeated_install",
        "core.narrow-search": "broad_scan",
        "core.subagent-sprawl": "subagent_sprawl",
        "core.repeat-action": "repeated_action",
        "core.verify-change": "unverified_change",
    }.get(policy_id, "policy_match")


def _knowledge_guidance_output(
    store: Store,
    session_id: str,
    turn_key: str,
    host: str,
    policy_id: str,
    envelope: str,
    *,
    event: str = "UserPromptSubmit",
) -> dict[str, Any] | None:
    effect = CanonicalEffect(
        effect="add_context",
        policy_id=policy_id,
        policy_revision="1",
        observed_fact="capability_match",
        agent_message=envelope,
        required_capability="add_context",
        measurement_label="capability-guidance",
    )
    rendered = render_intervention(store, session_id, turn_key, host, event, effect)
    if not rendered:
        return None
    prefix = "Agent Efficiency: "
    context = rendered.get("hookSpecificOutput")
    if isinstance(context, dict):
        value = context.get("additionalContext")
        if isinstance(value, str) and value.startswith(prefix):
            context["additionalContext"] = value[len(prefix) :]
    return rendered
