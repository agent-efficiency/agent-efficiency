"""Bounded stdin protocol for the experimental observe-only Pi extension."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from agent_efficiency.adapters.pi import PiAdapter
from agent_efficiency.hook import run_hook
from agent_efficiency.store import Store
from agent_efficiency.policy import PolicyPack


def dispatch(payload: dict, store: Store) -> dict:
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.startswith("pi:"):
        raise ValueError("invalid session")
    action = payload.get("action", "event")
    if action not in {"event", "status", "observe", "off"}:
        raise ValueError("unsupported action")
    if (
        action == "event"
        and payload.get("hook_event_name") not in PiAdapter.native_events
    ):
        raise ValueError("unsupported event")
    with store.connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS pi_advice_previews (
            id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
            policy_id TEXT NOT NULL, reason TEXT NOT NULL, event TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(session_id, turn_id, policy_id)
        )""")
    session = store.get_session(session_id)
    if session is None:
        store.ensure_session(session_id, host="pi", cwd=payload.get("cwd"))
        store.set_mode(session_id, "observe")
    elif session["mode"] not in {"observe", "off"}:
        store.set_mode(session_id, "observe")
    if action in {"observe", "off"}:
        store.set_mode(session_id, action)
    elif action == "event":
        # Deliberately do not accept prompts, output, or assistant messages.
        allowed = {
            "session_id",
            "cwd",
            "turn_id",
            "hook_event_name",
            "tool_name",
            "tool_use_id",
            "tool_input",
            "duration_ms",
        }
        event = {k: v for k, v in payload.items() if k in allowed}
        event["host"] = "pi"
        output = run_hook(event, store=store, environ={})
        if output and "preview" in output:
            preview = output["preview"]
            with store.connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO pi_advice_previews "
                    "(session_id, turn_id, policy_id, reason, event) VALUES (?, ?, ?, ?, ?)",
                    (
                        session_id,
                        event.get("turn_id") or "session",
                        preview["policy_id"],
                        preview["reason"],
                        preview["event"],
                    ),
                )
    session = store.get_session(session_id)
    with store.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS turns, COALESCE(SUM(tool_count), 0) AS tools, "
            "COALESCE(SUM(failure_count), 0) AS failures "
            "FROM turns WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        preview_count = conn.execute(
            "SELECT COUNT(*) FROM pi_advice_previews WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        previews = [
            dict(item)
            for item in conn.execute(
                "SELECT policy_id, reason, event, created_at FROM pi_advice_previews "
                "WHERE session_id = ? ORDER BY id DESC LIMIT 5",
                (session_id,),
            )
        ]
    pack = PolicyPack.load(store.paths.root)
    for preview in previews:
        nudge = pack.get(preview["policy_id"])
        preview["message"] = nudge.message if nudge else "Policy text unavailable."
        preview["effect"] = "would_advise"
    return {
        "ok": True,
        "mode": session["mode"],
        "host": "pi",
        **dict(row),
        "preview_count": preview_count,
        "previews": previews,
    }


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("oversize request")
        payload = json.loads(raw)
        root = Path(
            os.environ.get("AGENT_EFFICIENCY_PI_DATA_DIR")
            or Path.home() / ".local/share/agent-efficiency-pi"
        )
        result = dispatch(payload, Store(root))
    except Exception:
        # Never print exception text: it can contain work content or paths.
        result = {"ok": False, "error": "bridge-unavailable"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
