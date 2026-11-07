"""Optional guidance receipts. Store digests, counts, and closed codes only.

These additive tables use their own version suffix. Schema 8 clients can
ignore them. They are created only when the optional guidance feature is used.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

from agent_efficiency.store import Store

COOLDOWN = 86_400
ZERO = "0" * 16
TABLES = (
    """CREATE TABLE IF NOT EXISTS guidance_offers_v1 (
        token TEXT PRIMARY KEY,
        project TEXT NOT NULL,
        session_id TEXT NOT NULL REFERENCES sessions(session_id),
        request TEXT NOT NULL,
        action TEXT NOT NULL,
        version TEXT NOT NULL,
        offered_at REAL NOT NULL,
        choice TEXT CHECK(choice IN ('once', 'similar', 'later', 'never')),
        UNIQUE(session_id, request)
    )""",
    """CREATE TABLE IF NOT EXISTS guidance_state_v1 (
        project TEXT NOT NULL,
        action TEXT NOT NULL,
        version TEXT NOT NULL,
        choice TEXT NOT NULL CHECK(choice IN ('similar', 'later', 'never')),
        decided_at REAL NOT NULL,
        PRIMARY KEY(project, action)
    )""",
    """CREATE TABLE IF NOT EXISTS guidance_events_v1 (
        id INTEGER PRIMARY KEY,
        project TEXT NOT NULL,
        session_id TEXT NOT NULL REFERENCES sessions(session_id),
        request TEXT NOT NULL,
        action TEXT NOT NULL,
        version TEXT NOT NULL,
        code TEXT NOT NULL CHECK(code IN (
            'notice_prepared', 'once', 'similar', 'later', 'never', 'reset',
            'context_prepared', 'budget_exceeded')),
        chars INTEGER NOT NULL CHECK(chars >= 0),
        recorded_at REAL NOT NULL
    )""",
)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@contextmanager
def transaction(store: Store) -> Iterator[sqlite3.Connection]:
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for statement in TABLES:
            conn.execute(statement)
        yield conn


def record(conn, project, session, request, action, version, code, chars=0):
    conn.execute(
        "INSERT INTO guidance_events_v1 "
        "(project, session_id, request, action, version, code, chars, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project, session, request, action, version, code, chars, time.time()),
    )


def state(conn, project, action):
    row = conn.execute(
        "SELECT * FROM guidance_state_v1 WHERE project = ? AND action = ?",
        (project, action),
    ).fetchone()
    return dict(row) if row else {}


def request_handled(conn, session, request):
    return (
        conn.execute(
            "SELECT 1 FROM guidance_events_v1 WHERE session_id = ? AND request = ? "
            "AND code IN ('notice_prepared', 'context_prepared', 'budget_exceeded') LIMIT 1",
            (session, request),
        ).fetchone()
        is not None
    )


def current_request(conn, session):
    row = conn.execute(
        "SELECT turn_key FROM turns WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        (session,),
    ).fetchone()
    return digest(str(row[0]) if row else "")


def request_is_open(conn, session):
    row = conn.execute(
        "SELECT ended_at FROM turns WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        (session,),
    ).fetchone()
    return row is not None and row[0] is None


def can_offer(conn, project, action):
    current = state(conn, project, action)
    if current.get("choice") == "never":
        return False
    if (
        current.get("choice") == "later"
        and time.time() - current["decided_at"] < COOLDOWN
    ):
        return False
    latest = conn.execute(
        "SELECT MAX(offered_at) FROM guidance_offers_v1 WHERE project = ? AND action = ?",
        (project, action),
    ).fetchone()[0]
    return latest is None or time.time() - latest >= COOLDOWN


def offer(conn, project, session, request, action, version):
    token = secrets.token_hex(8)
    conn.execute(
        "INSERT INTO guidance_offers_v1 "
        "(token, project, session_id, request, action, version, offered_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (token, project, session, request, action, version, time.time()),
    )
    return token


def pending(conn, token, project, session, request):
    row = conn.execute(
        "SELECT * FROM guidance_offers_v1 WHERE token = ? AND project = ? "
        "AND session_id = ? AND request = ? AND choice IS NULL AND offered_at > ?",
        (token, project, session, request, time.time() - COOLDOWN),
    ).fetchone()
    return dict(row) if row else None


def choose(conn, offered, choice):
    conn.execute(
        "UPDATE guidance_offers_v1 SET choice = ? WHERE token = ? AND choice IS NULL",
        (choice, offered["token"]),
    )
    if choice != "once":
        conn.execute(
            "INSERT INTO guidance_state_v1 VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(project, action) DO UPDATE SET version = excluded.version, "
            "choice = excluded.choice, decided_at = excluded.decided_at",
            (
                offered["project"],
                offered["action"],
                offered["version"],
                choice,
                time.time(),
            ),
        )
    record(
        conn,
        offered["project"],
        offered["session_id"],
        offered["request"],
        offered["action"],
        offered["version"],
        choice,
    )


def reset(conn, project, session, request):
    conn.execute("DELETE FROM guidance_state_v1 WHERE project = ?", (project,))
    # Pending tokens cannot revive an approval after a reset. Keep the offers
    # and receipts as history, and let the cooldown expire normally.
    conn.execute(
        "UPDATE guidance_offers_v1 SET choice = 'later' "
        "WHERE project = ? AND choice IS NULL",
        (project,),
    )
    record(conn, project, session, request, ZERO, ZERO, "reset")


def context_fits(conn, session, chars):
    row = conn.execute(
        "SELECT chars_emitted FROM vault_receipts WHERE session_id = ? "
        "AND disposition IN ('delivered', 'truncated', 'deferred') ORDER BY id DESC LIMIT 1",
        (session,),
    ).fetchone()
    return chars <= 500 and (int(row[0]) if row else 0) + chars <= 9000


def in_experiment(store: Store, session: str) -> bool:
    with store.connect() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM experiment_enrollments WHERE session_id = ?", (session,)
            ).fetchone()
            is not None
        )


def context_reserve(store: Store, session: str) -> int:
    """Reserve room on later vault reloads, even if the proposal file is removed."""
    with store.connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'guidance_events_v1'"
        ).fetchone()
        if not exists:
            return 0
        row = conn.execute(
            "SELECT MAX(chars) FROM guidance_events_v1 "
            "WHERE session_id = ? AND code = 'context_prepared'",
            (session,),
        ).fetchone()
        return int(row[0] or 0)
