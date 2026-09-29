"""Concurrent, privacy-preserving SQLite event ledger."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from agent_efficiency.models import VALID_MODES
from agent_efficiency.paths import RuntimePaths, create_private_file


SCHEMA_VERSION = "7"
INTERVENTION_EVENT_WINDOW = 8
MAX_NUDGES_PER_TURN = 3
MAX_NUDGES_PER_SESSION = 8
MAX_KNOWLEDGE_ENVELOPES_PER_SESSION = 4
MAX_KNOWLEDGE_CHARS_PER_ENVELOPE = 500
MAX_KNOWLEDGE_CHARS_PER_SESSION = 2000

# The vault receipt is a closed schema. Every text column is a fixed code or a
# digest, so a vault path, note id, title, hook, remote, or branch cannot be
# stored even by mistake.
VAULT_CAUSES = ("new", "resume", "compact", "request", "deferred")
VAULT_DISPOSITIONS = (
    "delivered",
    "truncated",
    "deferred",
    "unavailable",
    "degraded",
    "skipped",
    "withheld",
)
VAULT_REASONS = (
    "no_match",
    "unmapped",
    "ambiguous",
    "over_budget",
    "host_limit",
    "host_unsupported",
    "parse_error",
    "timeout",
    "no_trees",
    "unchanged",
    "observe_mode",
    "cap_exceeded",
    "cap_refused",
    "cap_allowed_indeterminate",
)
VAULT_EMITTED = ("delivered", "truncated", "deferred")
# A compact receipt with one of these dispositions handled its compaction.
VAULT_COMPACTION_HANDLED = (*VAULT_EMITTED, "degraded", "withheld")
_VAULT_DIGEST = re.compile(r"^[0-9a-f]{16}$")
# Why a verification result is not a pass. Fixed codes only; the plain sentence
# for each lives in verification.receipts.REASONS.
VERIFICATION_REASONS = (
    "exit_nonzero",
    "timeout",
    "not_started",
    "workspace_unknown",
    "workspace_changed",
    "workspace_unreadable",
)


def _sql_choices(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


VAULT_RECEIPTS_SQL = f"""
CREATE TABLE IF NOT EXISTS vault_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    cause TEXT NOT NULL CHECK(cause IN ({_sql_choices(VAULT_CAUSES)})),
    vault_revision TEXT NOT NULL CHECK(length(vault_revision) = 16),
    payload_digest TEXT NOT NULL CHECK(length(payload_digest) = 16),
    notes_selected INTEGER NOT NULL CHECK(notes_selected >= 0),
    head_chars INTEGER NOT NULL CHECK(head_chars >= 0),
    chars_emitted INTEGER NOT NULL CHECK(chars_emitted >= 0),
    chars_omitted INTEGER NOT NULL CHECK(chars_omitted >= 0),
    disposition TEXT NOT NULL
        CHECK(disposition IN ({_sql_choices(VAULT_DISPOSITIONS)})),
    reason_code TEXT
        CHECK(reason_code IS NULL OR reason_code IN ({_sql_choices(VAULT_REASONS)})),
    recorded_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS vault_receipts_session
    ON vault_receipts(session_id, id DESC);
CREATE INDEX IF NOT EXISTS vault_receipts_recorded
    ON vault_receipts(recorded_at);
"""


RECEIPT_REASON_COLUMN = (
    "reason_code TEXT CHECK(reason_code IS NULL OR reason_code IN "
    f"({_sql_choices(VERIFICATION_REASONS)}))"
)


def _add_receipt_reason_column(conn: sqlite3.Connection) -> None:
    """Add the reason column to a receipt table made by an earlier release.

    The column is added in place rather than through a schema version, so an
    earlier release can still open the same database.
    """

    columns = {
        str(column[1])
        for column in conn.execute("PRAGMA table_info(verification_receipts)")
    }
    if columns and "reason_code" not in columns:
        conn.execute(
            f"ALTER TABLE verification_receipts ADD COLUMN {RECEIPT_REASON_COLUMN}"
        )


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def project_identity(cwd: str | None) -> tuple[str, str]:
    value = str(Path(cwd or ".").expanduser().resolve())
    key = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return key, Path(value).name or "root"


class Store:
    def __init__(self, root: str | Path | None = None) -> None:
        self.paths = RuntimePaths.from_root(root)
        self.paths.ensure()
        if not self.paths.database.is_file() or self._database_is_empty():
            # SQLite gives its journal files the database's mode, so creating
            # the file 0600 first keeps all three private.
            create_private_file(self.paths.database)
            self._initialize()

    def _database_is_empty(self) -> bool:
        """An empty file is a store another process has only just created."""

        try:
            return self.paths.database.stat().st_size == 0
        except OSError:
            return False

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.paths.database, timeout=1.5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 1500")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    host TEXT NOT NULL,
                    project_key TEXT NOT NULL,
                    project_name TEXT NOT NULL,
                    model TEXT,
                    mode TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    ended_at TEXT,
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    tool_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    nudge_count INTEGER NOT NULL DEFAULT 0,
                    compaction_count INTEGER NOT NULL DEFAULT 0,
                    subagent_count INTEGER NOT NULL DEFAULT 0,
                    edit_count INTEGER NOT NULL DEFAULT 0,
                    validation_count INTEGER NOT NULL DEFAULT 0,
                    repeated_action_count INTEGER NOT NULL DEFAULT 0,
                    broad_scan_count INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cache_read_tokens INTEGER,
                    cache_creation_tokens INTEGER,
                    context_used_percent REAL,
                    lines_added INTEGER,
                    lines_removed INTEGER,
                    evidence_version TEXT NOT NULL DEFAULT 'receipt-v1',
                    guard_policy_digest TEXT
                );

                CREATE INDEX IF NOT EXISTS sessions_project_updated
                    ON sessions(project_key, updated_at DESC);

                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_key TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    prompt_chars INTEGER NOT NULL DEFAULT 0,
                    task_type TEXT NOT NULL DEFAULT 'unknown',
                    risk TEXT NOT NULL DEFAULT 'normal',
                    tool_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    nudge_count INTEGER NOT NULL DEFAULT 0,
                    edit_count INTEGER NOT NULL DEFAULT 0,
                    code_edit_count INTEGER NOT NULL DEFAULT 0,
                    validation_count INTEGER NOT NULL DEFAULT 0,
                    repeated_action_count INTEGER NOT NULL DEFAULT 0,
                    broad_scan_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(session_id, turn_key),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS turns_session_started
                    ON turns(session_id, started_at DESC);

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_key TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    tool_name TEXT,
                    command_class TEXT,
                    signature TEXT,
                    outcome TEXT,
                    duration_ms INTEGER,
                    safe_details TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS events_turn_signature
                    ON events(session_id, turn_key, signature, event_name);

                CREATE TABLE IF NOT EXISTS nudges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_key TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, turn_key, policy_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS host_capabilities (
                    session_id TEXT NOT NULL,
                    host TEXT NOT NULL,
                    host_version TEXT,
                    event_name TEXT NOT NULL,
                    capabilities TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, event_name),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS interventions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_key TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    policy_revision TEXT NOT NULL,
                    triggering_fact TEXT NOT NULL,
                    requested_effect TEXT NOT NULL,
                    rendered_effect TEXT NOT NULL,
                    host_capability TEXT NOT NULL,
                    disposition TEXT NOT NULL CHECK(
                        disposition IN (
                            'emitted', 'suppressed', 'unsupported', 'failed'
                        )
                    ),
                    message_chars INTEGER NOT NULL CHECK(message_chars >= 0),
                    event_id_at_emit INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS interventions_session
                    ON interventions(session_id, turn_key, id DESC);

                CREATE TABLE IF NOT EXISTS intervention_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intervention_id INTEGER NOT NULL,
                    outcome TEXT NOT NULL CHECK(
                        outcome IN (
                            'suggested_action_observed', 'failure_repeated',
                            'check_started', 'check_passed', 'check_failed',
                            'turn_completed', 'user_disabled_advice',
                            'no_observable_response'
                        )
                    ),
                    event_distance INTEGER NOT NULL CHECK(event_distance >= 0),
                    observed_at TEXT NOT NULL,
                    UNIQUE(intervention_id, outcome),
                    FOREIGN KEY(intervention_id) REFERENCES interventions(id)
                );

                CREATE INDEX IF NOT EXISTS intervention_outcomes_intervention
                    ON intervention_outcomes(intervention_id, id);

                CREATE TABLE IF NOT EXISTS intervention_feedback (
                    intervention_id INTEGER PRIMARY KEY,
                    judgment TEXT NOT NULL CHECK(
                        judgment IN (
                            'useful', 'neutral', 'false_or_unnecessary'
                        )
                    ),
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(intervention_id) REFERENCES interventions(id)
                );

                CREATE TABLE IF NOT EXISTS runtime_health (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_key TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    operation TEXT NOT NULL CHECK(
                        operation IN ('normal-hook', 'session-start')
                    ),
                    duration_us INTEGER NOT NULL CHECK(duration_us >= 0),
                    outcome TEXT NOT NULL CHECK(
                        outcome IN ('success', 'failed')
                    ),
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS runtime_health_operation
                    ON runtime_health(operation, recorded_at);

                CREATE TABLE IF NOT EXISTS session_capability_packs (
                    session_id TEXT PRIMARY KEY,
                    pack_id TEXT NOT NULL,
                    pack_digest TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    pinned_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS knowledge_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_key TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    pack_id TEXT NOT NULL,
                    pack_digest TEXT NOT NULL,
                    card_id TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    match_reason TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    suppression_reason TEXT,
                    emitted_chars INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, turn_key, card_id, disposition),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS knowledge_receipts_session
                    ON knowledge_receipts(session_id, id DESC);

                CREATE TABLE IF NOT EXISTS experiment_enrollments (
                    session_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    cohort TEXT NOT NULL
                        CHECK(cohort IN ('observe', 'advise')),
                    task_class TEXT NOT NULL,
                    task_set_digest TEXT NOT NULL CHECK(
                        length(task_set_digest) = 71
                        AND substr(task_set_digest, 1, 7) = 'sha256:'
                        AND substr(task_set_digest, 8)
                            NOT GLOB '*[^0-9a-f]*'
                    ),
                    agent_profile TEXT NOT NULL,
                    blinded INTEGER NOT NULL CHECK(blinded IN (0, 1)),
                    enrolled_at TEXT NOT NULL,
                    invalidated_at TEXT,
                    invalidation_reason TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS experiment_enrollments_experiment
                    ON experiment_enrollments(
                        experiment_id, task_class, task_set_digest,
                        agent_profile, cohort
                    );

                CREATE TABLE IF NOT EXISTS session_outcomes (
                    session_id TEXT PRIMARY KEY,
                    accepted INTEGER NOT NULL CHECK(accepted IN (0, 1)),
                    acceptance_evidence TEXT NOT NULL CHECK(
                        acceptance_evidence IN (
                            'test', 'ci', 'review', 'user-confirmed', 'none'
                        )
                    ),
                    acceptance_evidence_digest TEXT CHECK(
                        (
                            acceptance_evidence = 'none'
                            AND acceptance_evidence_digest IS NULL
                        )
                        OR (
                            acceptance_evidence != 'none'
                            AND length(acceptance_evidence_digest) = 71
                            AND substr(acceptance_evidence_digest, 1, 7) = 'sha256:'
                            AND substr(acceptance_evidence_digest, 8)
                                NOT GLOB '*[^0-9a-f]*'
                        )
                    ),
                    completion_evidence_state TEXT NOT NULL DEFAULT 'inconclusive'
                        CHECK(completion_evidence_state IN (
                            'current', 'stale', 'missing', 'failed', 'blocked',
                            'inconclusive'
                        )),
                    wall_time_minutes REAL NOT NULL CHECK(wall_time_minutes > 0),
                    correction_turns INTEGER NOT NULL DEFAULT 0
                        CHECK(correction_turns >= 0),
                    human_review_minutes REAL NOT NULL DEFAULT 0
                        CHECK(human_review_minutes >= 0),
                    escaped_defects INTEGER NOT NULL DEFAULT 0
                        CHECK(escaped_defects >= 0),
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS capability_feedback (
                    session_id TEXT NOT NULL,
                    card_id TEXT NOT NULL,
                    rating TEXT NOT NULL
                        CHECK(rating IN ('useful', 'neutral', 'distracting')),
                    changed_next_action INTEGER NOT NULL
                        CHECK(changed_next_action IN (0, 1)),
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, card_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS capability_feedback_card
                    ON capability_feedback(card_id, rating);

                CREATE TABLE IF NOT EXISTS capability_runtime_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_key TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    duration_us INTEGER NOT NULL,
                    context_chars INTEGER NOT NULL DEFAULT 0,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS capability_runtime_operation
                    ON capability_runtime_samples(operation, recorded_at);

                CREATE TABLE IF NOT EXISTS check_definitions (
                    project_key TEXT NOT NULL,
                    check_id TEXT NOT NULL,
                    check_digest TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_key, check_id)
                );

                CREATE TABLE IF NOT EXISTS workspace_states (
                    workspace_digest TEXT PRIMARY KEY,
                    project_key TEXT NOT NULL,
                    git_commit TEXT,
                    dirty INTEGER NOT NULL CHECK(dirty IN (0, 1)),
                    observed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS verification_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_key TEXT NOT NULL,
                    check_id TEXT NOT NULL,
                    check_digest TEXT NOT NULL,
                    workspace_digest TEXT,
                    git_commit TEXT,
                    dirty INTEGER NOT NULL CHECK(dirty IN (0, 1)),
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    exit_code INTEGER,
                    result TEXT NOT NULL CHECK(
                        result IN ('pass', 'fail', 'blocked', 'inconclusive')
                    ),
                    runner_version TEXT NOT NULL,
                    host TEXT,
                    session_id TEXT,
                    turn_id TEXT,
                    schema_version INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS verification_receipts_project
                    ON verification_receipts(project_key, check_id, id DESC);

                CREATE TABLE IF NOT EXISTS status_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    cost_usd REAL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cache_read_tokens INTEGER,
                    cache_creation_tokens INTEGER,
                    context_used_percent REAL,
                    captured_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );
                """
            )
            conn.executescript(VAULT_RECEIPTS_SQL)
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES('default_mode', 'advise')"
            )
            _add_receipt_reason_column(conn)

    def _migrate(self) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'schema_version'"
            ).fetchone()
            version = str(row["value"]) if row else "1"
            if version not in {"1", "2", "3", "4", "5", "6", SCHEMA_VERSION}:
                raise ValueError(f"unsupported data schema version: {version}")
            if version != SCHEMA_VERSION:
                backup_path = self.paths.database.with_name(
                    f"{self.paths.database.name}.schema-{version}.bak"
                )
                if not backup_path.exists():
                    create_private_file(backup_path)
                    with closing(sqlite3.connect(backup_path)) as backup:
                        conn.backup(backup)
            if version == "1":
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS session_capability_packs (
                        session_id TEXT PRIMARY KEY,
                        pack_id TEXT NOT NULL,
                        pack_digest TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        pinned_at TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                    );

                    CREATE TABLE IF NOT EXISTS knowledge_receipts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        turn_key TEXT NOT NULL,
                        event_name TEXT NOT NULL,
                        pack_id TEXT NOT NULL,
                        pack_digest TEXT NOT NULL,
                        card_id TEXT NOT NULL,
                        score INTEGER NOT NULL,
                        match_reason TEXT NOT NULL,
                        disposition TEXT NOT NULL,
                        suppression_reason TEXT,
                        emitted_chars INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        UNIQUE(session_id, turn_key, card_id, disposition),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                    );

                    CREATE INDEX IF NOT EXISTS knowledge_receipts_session
                        ON knowledge_receipts(session_id, id DESC);
                    """
                )
                version = "2"
            if version == "2":
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS experiment_enrollments (
                        session_id TEXT PRIMARY KEY,
                        experiment_id TEXT NOT NULL,
                        cohort TEXT NOT NULL
                            CHECK(cohort IN ('observe', 'advise')),
                        task_class TEXT NOT NULL,
                        task_set_digest TEXT NOT NULL CHECK(
                            length(task_set_digest) = 71
                            AND substr(task_set_digest, 1, 7) = 'sha256:'
                            AND substr(task_set_digest, 8)
                                NOT GLOB '*[^0-9a-f]*'
                        ),
                        agent_profile TEXT NOT NULL,
                        blinded INTEGER NOT NULL CHECK(blinded IN (0, 1)),
                        enrolled_at TEXT NOT NULL,
                        invalidated_at TEXT,
                        invalidation_reason TEXT,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                    );

                    CREATE INDEX IF NOT EXISTS experiment_enrollments_experiment
                        ON experiment_enrollments(
                            experiment_id, task_class, task_set_digest,
                            agent_profile, cohort
                        );

                    CREATE TABLE IF NOT EXISTS session_outcomes (
                        session_id TEXT PRIMARY KEY,
                        accepted INTEGER NOT NULL CHECK(accepted IN (0, 1)),
                        acceptance_evidence TEXT NOT NULL CHECK(
                            acceptance_evidence IN (
                                'test', 'ci', 'review', 'user-confirmed', 'none'
                            )
                        ),
                        acceptance_evidence_digest TEXT CHECK(
                            (
                                acceptance_evidence = 'none'
                                AND acceptance_evidence_digest IS NULL
                            )
                            OR (
                                acceptance_evidence != 'none'
                                AND length(acceptance_evidence_digest) = 71
                                AND substr(acceptance_evidence_digest, 1, 7)
                                    = 'sha256:'
                                AND substr(acceptance_evidence_digest, 8)
                                    NOT GLOB '*[^0-9a-f]*'
                            )
                        ),
                        completion_evidence_state TEXT NOT NULL
                            DEFAULT 'inconclusive' CHECK(
                                completion_evidence_state IN (
                                    'current', 'stale', 'missing', 'failed',
                                    'blocked', 'inconclusive'
                                )
                            ),
                        wall_time_minutes REAL NOT NULL
                            CHECK(wall_time_minutes > 0),
                        correction_turns INTEGER NOT NULL DEFAULT 0
                            CHECK(correction_turns >= 0),
                        human_review_minutes REAL NOT NULL DEFAULT 0
                            CHECK(human_review_minutes >= 0),
                        escaped_defects INTEGER NOT NULL DEFAULT 0
                            CHECK(escaped_defects >= 0),
                        recorded_at TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                    );

                    CREATE TABLE IF NOT EXISTS capability_feedback (
                        session_id TEXT NOT NULL,
                        card_id TEXT NOT NULL,
                        rating TEXT NOT NULL CHECK(
                            rating IN ('useful', 'neutral', 'distracting')
                        ),
                        changed_next_action INTEGER NOT NULL
                            CHECK(changed_next_action IN (0, 1)),
                        recorded_at TEXT NOT NULL,
                        PRIMARY KEY(session_id, card_id),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                    );

                    CREATE INDEX IF NOT EXISTS capability_feedback_card
                        ON capability_feedback(card_id, rating);

                    CREATE TABLE IF NOT EXISTS capability_runtime_samples (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        turn_key TEXT NOT NULL,
                        event_name TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        duration_us INTEGER NOT NULL,
                        context_chars INTEGER NOT NULL DEFAULT 0,
                        recorded_at TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                    );

                    CREATE INDEX IF NOT EXISTS capability_runtime_operation
                        ON capability_runtime_samples(operation, recorded_at);
                    """
                )
            if version in {"1", "2", "3"}:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS check_definitions (
                        project_key TEXT NOT NULL,
                        check_id TEXT NOT NULL,
                        check_digest TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(project_key, check_id)
                    );

                    CREATE TABLE IF NOT EXISTS workspace_states (
                        workspace_digest TEXT PRIMARY KEY,
                        project_key TEXT NOT NULL,
                        git_commit TEXT,
                        dirty INTEGER NOT NULL CHECK(dirty IN (0, 1)),
                        observed_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS verification_receipts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        project_key TEXT NOT NULL,
                        check_id TEXT NOT NULL,
                        check_digest TEXT NOT NULL,
                        workspace_digest TEXT,
                        git_commit TEXT,
                        dirty INTEGER NOT NULL CHECK(dirty IN (0, 1)),
                        started_at TEXT NOT NULL,
                        finished_at TEXT NOT NULL,
                        duration_ms INTEGER NOT NULL,
                        exit_code INTEGER,
                        result TEXT NOT NULL CHECK(
                            result IN (
                                'pass', 'fail', 'blocked', 'inconclusive'
                            )
                        ),
                        runner_version TEXT NOT NULL,
                        host TEXT,
                        session_id TEXT,
                        turn_id TEXT,
                        schema_version INTEGER NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS verification_receipts_project
                        ON verification_receipts(
                            project_key, check_id, id DESC
                        );
                    """
                )
            if version in {"1", "2", "3", "4"}:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS interventions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        turn_key TEXT NOT NULL,
                        policy_id TEXT NOT NULL,
                        policy_revision TEXT NOT NULL,
                        triggering_fact TEXT NOT NULL,
                        requested_effect TEXT NOT NULL,
                        rendered_effect TEXT NOT NULL,
                        host_capability TEXT NOT NULL,
                        disposition TEXT NOT NULL CHECK(
                            disposition IN (
                                'emitted', 'suppressed', 'unsupported', 'failed'
                            )
                        ),
                        message_chars INTEGER NOT NULL CHECK(message_chars >= 0),
                        event_id_at_emit INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                    );

                    CREATE INDEX IF NOT EXISTS interventions_session
                        ON interventions(session_id, turn_key, id DESC);

                    CREATE TABLE IF NOT EXISTS intervention_outcomes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        intervention_id INTEGER NOT NULL,
                        outcome TEXT NOT NULL CHECK(
                            outcome IN (
                                'suggested_action_observed', 'failure_repeated',
                                'check_started', 'check_passed', 'check_failed',
                                'turn_completed', 'user_disabled_advice',
                                'no_observable_response'
                            )
                        ),
                        event_distance INTEGER NOT NULL
                            CHECK(event_distance >= 0),
                        observed_at TEXT NOT NULL,
                        UNIQUE(intervention_id, outcome),
                        FOREIGN KEY(intervention_id) REFERENCES interventions(id)
                    );

                    CREATE INDEX IF NOT EXISTS intervention_outcomes_intervention
                        ON intervention_outcomes(intervention_id, id);

                    CREATE TABLE IF NOT EXISTS intervention_feedback (
                        intervention_id INTEGER PRIMARY KEY,
                        judgment TEXT NOT NULL CHECK(
                            judgment IN (
                                'useful', 'neutral', 'false_or_unnecessary'
                            )
                        ),
                        recorded_at TEXT NOT NULL,
                        FOREIGN KEY(intervention_id) REFERENCES interventions(id)
                    );

                    CREATE TABLE IF NOT EXISTS runtime_health (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        turn_key TEXT NOT NULL,
                        event_name TEXT NOT NULL,
                        operation TEXT NOT NULL CHECK(
                            operation IN ('normal-hook', 'session-start')
                        ),
                        duration_us INTEGER NOT NULL CHECK(duration_us >= 0),
                        outcome TEXT NOT NULL CHECK(
                            outcome IN ('success', 'failed')
                        ),
                        recorded_at TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                    );

                    CREATE INDEX IF NOT EXISTS runtime_health_operation
                        ON runtime_health(operation, recorded_at);

                    """
                )
            if version in {"1", "2", "3", "4", "5"}:
                session_columns = {
                    str(column["name"])
                    for column in conn.execute("PRAGMA table_info(sessions)")
                }
                if "evidence_version" not in session_columns:
                    conn.execute(
                        "ALTER TABLE sessions ADD COLUMN evidence_version "
                        "TEXT NOT NULL DEFAULT 'receipt-v1'"
                    )
                if "guard_policy_digest" not in session_columns:
                    conn.execute(
                        "ALTER TABLE sessions ADD COLUMN guard_policy_digest TEXT"
                    )
                conn.execute(
                    "UPDATE sessions SET evidence_version = 'legacy-observation'"
                )
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS host_capabilities (
                        session_id TEXT NOT NULL,
                        host TEXT NOT NULL,
                        host_version TEXT,
                        event_name TEXT NOT NULL,
                        capabilities TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        PRIMARY KEY(session_id, event_name),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                    );
                    """
                )
            if version in {"1", "2", "3", "4", "5", "6"}:
                conn.executescript(VAULT_RECEIPTS_SQL)
            outcome_columns = {
                str(column["name"])
                for column in conn.execute("PRAGMA table_info(session_outcomes)")
            }
            if "completion_evidence_state" not in outcome_columns:
                conn.execute(
                    "ALTER TABLE session_outcomes ADD COLUMN "
                    "completion_evidence_state TEXT NOT NULL "
                    "DEFAULT 'inconclusive' CHECK("
                    "completion_evidence_state IN ("
                    "'current', 'stale', 'missing', 'failed', 'blocked', "
                    "'inconclusive'))"
                )
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (SCHEMA_VERSION,),
            )

    def ensure_current_schema(self) -> None:
        if self.get_setting("schema_version", "1") != SCHEMA_VERSION:
            self._migrate()

    def migration_status(self) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'schema_version'"
            ).fetchone()
        version = str(row["value"]) if row else "1"
        supported = version in {"1", "2", "3", "4", "5", "6", SCHEMA_VERSION}
        backup = self.paths.database.with_name(
            f"{self.paths.database.name}.schema-{version}.bak"
        )
        return {
            "current_schema": version,
            "target_schema": SCHEMA_VERSION,
            "migration_required": version != SCHEMA_VERSION,
            "supported": supported,
            "backup_path": str(backup) if version != SCHEMA_VERSION else None,
        }

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def default_mode(self) -> str:
        value = self.get_setting("default_mode", "advise") or "advise"
        return value if value in VALID_MODES else "advise"

    def set_default_mode(self, mode: str) -> None:
        if mode not in VALID_MODES:
            raise ValueError(f"invalid mode: {mode}")
        self.set_setting("default_mode", mode)

    def ensure_session(
        self,
        session_id: str,
        *,
        host: str,
        cwd: str | None,
        model: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        project_key, project_name = project_identity(cwd)
        with self.connect() as conn:
            row = self._ensure_session_row(
                conn,
                session_id,
                host=host,
                project_key=project_key,
                project_name=project_name,
                model=model,
                now=now,
            )
        return dict(row) if row else {}

    def ensure_session_with_capability_pack(
        self,
        session_id: str,
        *,
        host: str,
        cwd: str | None,
        model: str | None,
        pack_id: str,
        pack_digest: str,
        sequence: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Create or refresh a session and establish its immutable pin atomically."""

        now = utc_now()
        project_key, project_name = project_identity(cwd)
        try:
            with self.connect() as conn:
                row = self._ensure_session_row(
                    conn,
                    session_id,
                    host=host,
                    project_key=project_key,
                    project_name=project_name,
                    model=model,
                    now=now,
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO session_capability_packs(
                        session_id, pack_id, pack_digest, sequence, pinned_at
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (session_id, pack_id, pack_digest, sequence, now),
                )
                pin = conn.execute(
                    "SELECT * FROM session_capability_packs WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            self._migrate()
            return self.ensure_session_with_capability_pack(
                session_id,
                host=host,
                cwd=cwd,
                model=model,
                pack_id=pack_id,
                pack_digest=pack_digest,
                sequence=sequence,
            )
        return (
            dict(row) if row else {},
            dict(pin) if pin else {},
        )

    @staticmethod
    def _ensure_session_row(
        conn: sqlite3.Connection,
        session_id: str,
        *,
        host: str,
        project_key: str,
        project_name: str,
        model: str | None,
        now: str,
    ) -> sqlite3.Row | None:
        setting = conn.execute(
            "SELECT value FROM settings WHERE key = 'default_mode'"
        ).fetchone()
        default_mode = (
            str(setting["value"])
            if setting and str(setting["value"]) in VALID_MODES
            else "advise"
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO sessions(
                session_id, host, project_key, project_name, model, mode,
                started_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                host,
                project_key,
                project_name,
                model,
                default_mode,
                now,
                now,
            ),
        )
        conn.execute(
            """
            UPDATE sessions
               SET host = ?, project_key = ?, project_name = ?,
                   model = COALESCE(?, model), updated_at = ?, ended_at = NULL
             WHERE session_id = ?
            """,
            (host, project_key, project_name, model, now, session_id),
        )
        return conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def set_mode(
        self,
        session_id: str,
        mode: str,
        *,
        guard_policy_digest: str | None = None,
    ) -> bool:
        if mode not in VALID_MODES:
            raise ValueError(f"invalid mode: {mode}")
        self.ensure_current_schema()
        now = utc_now()
        with self.connect() as conn:
            invalidation = conn.execute(
                """
                UPDATE experiment_enrollments
                   SET invalidated_at = ?,
                       invalidation_reason = 'session-mode-changed'
                 WHERE session_id = ? AND cohort != ?
                   AND invalidated_at IS NULL
                """,
                (now, session_id, mode),
            )
            conn.execute(
                "UPDATE sessions SET mode = ?, guard_policy_digest = ?, "
                "updated_at = ? WHERE session_id = ?",
                (
                    mode,
                    guard_policy_digest if mode == "guard" else None,
                    now,
                    session_id,
                ),
            )
        return invalidation.rowcount > 0

    def set_guard_policy_digest(self, session_id: str, digest: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE sessions SET guard_policy_digest = ?, updated_at = ? "
                "WHERE session_id = ?",
                (digest, utc_now(), session_id),
            )

    def latest_session(
        self, cwd: str | None = None, *, host: str | None = None
    ) -> dict[str, Any] | None:
        clauses: list[str] = []
        parameters: list[str] = []
        if cwd:
            project_key, _ = project_identity(cwd)
            clauses.append("project_key = ?")
            parameters.append(project_key)
        if host:
            clauses.append("host = ?")
            parameters.append(host)
        where_clause = " AND ".join(clauses)
        where = f" WHERE {where_clause}" if where_clause else ""
        query = f"SELECT * FROM sessions{where} ORDER BY updated_at DESC LIMIT 1"
        with self.connect() as conn:
            row = conn.execute(query, parameters).fetchone()
        return dict(row) if row else None

    def start_turn(
        self,
        session_id: str,
        turn_key: str,
        *,
        prompt_chars: int,
        task_type: str,
        risk: str,
    ) -> None:
        now = utc_now()
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO turns(
                    session_id, turn_key, started_at, prompt_chars, task_type, risk
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (session_id, turn_key, now, prompt_chars, task_type, risk),
            )
            inserted = conn.total_changes > before
            if inserted:
                conn.execute(
                    "UPDATE sessions SET turn_count = turn_count + 1, updated_at = ? "
                    "WHERE session_id = ?",
                    (now, session_id),
                )

    def record_host_capabilities(
        self,
        session_id: str,
        *,
        host: str,
        host_version: str | None,
        event_name: str,
        capabilities: tuple[str, ...],
    ) -> None:
        self.ensure_current_schema()
        encoded = json.dumps(sorted(set(capabilities)), separators=(",", ":"))
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO host_capabilities(
                    session_id, host, host_version, event_name, capabilities,
                    observed_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, event_name) DO UPDATE SET
                    host = excluded.host,
                    host_version = excluded.host_version,
                    capabilities = excluded.capabilities,
                    observed_at = excluded.observed_at
                """,
                (
                    session_id,
                    host,
                    host_version,
                    event_name,
                    encoded,
                    utc_now(),
                ),
            )

    def record_event(
        self,
        session_id: str,
        turn_key: str,
        *,
        event_name: str,
        tool_name: str | None = None,
        command_class: str | None = None,
        signature: str | None = None,
        outcome: str | None = None,
        duration_ms: int | None = None,
        safe_details: dict[str, Any] | None = None,
        count_tool: bool = False,
        count_failure: bool = False,
        count_edit: bool = False,
        count_code_edit: bool = False,
        count_validation: bool = False,
        count_subagent: bool = False,
        count_broad_scan: bool = False,
    ) -> None:
        now = utc_now()
        details = json.dumps(safe_details or {}, sort_keys=True, separators=(",", ":"))
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO events(
                    session_id, turn_key, event_name, tool_name, command_class,
                    signature, outcome, duration_ms, safe_details, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    turn_key,
                    event_name,
                    tool_name,
                    command_class,
                    signature,
                    outcome,
                    duration_ms,
                    details,
                    now,
                ),
            )
            session_updates = {
                "tool_count": int(count_tool),
                "failure_count": int(count_failure),
                "edit_count": int(count_edit),
                "validation_count": int(count_validation),
                "subagent_count": int(count_subagent),
                "broad_scan_count": int(count_broad_scan),
            }
            turn_updates = {
                "tool_count": int(count_tool),
                "failure_count": int(count_failure),
                "edit_count": int(count_edit),
                "code_edit_count": int(count_code_edit),
                "validation_count": int(count_validation),
                "broad_scan_count": int(count_broad_scan),
            }
            self._increment(conn, "sessions", "session_id", session_id, session_updates)
            self._increment_turn(conn, session_id, turn_key, turn_updates)
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (now, session_id),
            )

    def record_tool_event(
        self,
        session_id: str,
        turn_key: str,
        *,
        event_name: str,
        tool_name: str,
        command_class: str,
        signature: str,
        outcome: str,
        duration_ms: int | None,
        safe_details: dict[str, Any],
        count_failure: bool,
        count_edit: bool,
        count_code_edit: bool,
        count_validation: bool,
        count_subagent: bool,
        count_broad_scan: bool,
    ) -> dict[str, int]:
        """Record one resolved tool and return trigger counters atomically."""

        now = utc_now()
        details = json.dumps(safe_details, sort_keys=True, separators=(",", ":"))
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO turns(
                    session_id, turn_key, started_at, prompt_chars, task_type, risk
                ) VALUES(?, ?, ?, 0, 'unknown', 'normal')
                """,
                (session_id, turn_key, now),
            )
            if conn.total_changes > before:
                conn.execute(
                    "UPDATE sessions SET turn_count = turn_count + 1 "
                    "WHERE session_id = ?",
                    (session_id,),
                )
            conn.execute(
                """
                INSERT INTO events(
                    session_id, turn_key, event_name, tool_name, command_class,
                    signature, outcome, duration_ms, safe_details, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    turn_key,
                    event_name,
                    tool_name,
                    command_class,
                    signature,
                    outcome,
                    duration_ms,
                    details,
                    now,
                ),
            )
            self._increment(
                conn,
                "sessions",
                "session_id",
                session_id,
                {
                    "tool_count": 1,
                    "failure_count": int(count_failure),
                    "edit_count": int(count_edit),
                    "validation_count": int(count_validation),
                    "subagent_count": int(count_subagent),
                    "broad_scan_count": int(count_broad_scan),
                },
            )
            self._increment_turn(
                conn,
                session_id,
                turn_key,
                {
                    "tool_count": 1,
                    "failure_count": int(count_failure),
                    "edit_count": int(count_edit),
                    "code_edit_count": int(count_code_edit),
                    "validation_count": int(count_validation),
                    "broad_scan_count": int(count_broad_scan),
                },
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN signature = ? THEN 1 ELSE 0 END) AS identical,
                    SUM(CASE WHEN signature = ? AND outcome = 'failure'
                             THEN 1 ELSE 0 END) AS failures,
                    SUM(CASE WHEN command_class = ? THEN 1 ELSE 0 END) AS class_count,
                    SUM(CASE WHEN command_class = 'subagent'
                             THEN 1 ELSE 0 END) AS subagents
                  FROM events
                 WHERE session_id = ? AND turn_key = ?
                """,
                (
                    signature,
                    signature,
                    command_class,
                    session_id,
                    turn_key,
                ),
            ).fetchone()
        return {
            "identical": int(row["identical"] or 0) if row else 0,
            "failures": int(row["failures"] or 0) if row else 0,
            "class_count": int(row["class_count"] or 0) if row else 0,
            "subagents": int(row["subagents"] or 0) if row else 0,
        }

    @staticmethod
    def _increment(
        conn: sqlite3.Connection,
        table: str,
        key_column: str,
        key_value: str,
        values: dict[str, int],
    ) -> None:
        active = {key: value for key, value in values.items() if value}
        if not active:
            return
        assignments = ", ".join(f"{key} = {key} + ?" for key in active)
        conn.execute(
            f"UPDATE {table} SET {assignments} WHERE {key_column} = ?",
            (*active.values(), key_value),
        )

    @staticmethod
    def _increment_turn(
        conn: sqlite3.Connection,
        session_id: str,
        turn_key: str,
        values: dict[str, int],
    ) -> None:
        active = {key: value for key, value in values.items() if value}
        if not active:
            return
        assignments = ", ".join(f"{key} = {key} + ?" for key in active)
        conn.execute(
            f"UPDATE turns SET {assignments} WHERE session_id = ? AND turn_key = ?",
            (*active.values(), session_id, turn_key),
        )

    def event_count(
        self,
        session_id: str,
        turn_key: str,
        *,
        event_name: str | None = None,
        signature: str | None = None,
        command_class: str | None = None,
        outcome: str | None = None,
    ) -> int:
        clauses = ["session_id = ?", "turn_key = ?"]
        params: list[Any] = [session_id, turn_key]
        for column, value in (
            ("event_name", event_name),
            ("signature", signature),
            ("command_class", command_class),
            ("outcome", outcome),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        query = f"SELECT COUNT(*) AS n FROM events WHERE {' AND '.join(clauses)}"
        with self.connect() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row["n"]) if row else 0

    def session_event_count(
        self,
        session_id: str,
        *,
        command_class: str | None = None,
        outcome: str | None = None,
    ) -> int:
        clauses = ["session_id = ?"]
        params: list[Any] = [session_id]
        for column, value in (
            ("command_class", command_class),
            ("outcome", outcome),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        query = f"SELECT COUNT(*) AS n FROM events WHERE {' AND '.join(clauses)}"
        with self.connect() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row["n"]) if row else 0

    def runtime_counts(
        self,
        session_id: str,
        turn_key: str,
        *,
        signature: str,
        command_class: str,
    ) -> dict[str, int]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN signature = ? THEN 1 ELSE 0 END) AS identical,
                    SUM(CASE WHEN signature = ? AND outcome = 'failure'
                             THEN 1 ELSE 0 END) AS failures,
                    SUM(CASE WHEN command_class = ? THEN 1 ELSE 0 END) AS class_count,
                    SUM(CASE WHEN command_class = 'subagent'
                             THEN 1 ELSE 0 END) AS subagents
                  FROM events
                 WHERE session_id = ? AND turn_key = ?
                """,
                (
                    signature,
                    signature,
                    command_class,
                    session_id,
                    turn_key,
                ),
            ).fetchone()
        return {
            "identical": int(row["identical"] or 0) if row else 0,
            "failures": int(row["failures"] or 0) if row else 0,
            "class_count": int(row["class_count"] or 0) if row else 0,
            "subagents": int(row["subagents"] or 0) if row else 0,
        }

    def mark_repeated_action(self, session_id: str, turn_key: str) -> None:
        with self.connect() as conn:
            self._increment(
                conn,
                "sessions",
                "session_id",
                session_id,
                {"repeated_action_count": 1},
            )
            self._increment_turn(
                conn,
                session_id,
                turn_key,
                {"repeated_action_count": 1},
            )

    def record_runtime_health(
        self,
        session_id: str,
        turn_key: str,
        *,
        event_name: str,
        operation: str,
        duration_us: int,
        outcome: str,
    ) -> None:
        """Record full hook latency and success without event content."""

        if operation not in {"normal-hook", "session-start"}:
            raise ValueError(f"unknown runtime operation: {operation}")
        if outcome not in {"success", "failed"}:
            raise ValueError(f"unknown runtime outcome: {outcome}")
        self.ensure_current_schema()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_health(
                    session_id, turn_key, event_name, operation, duration_us,
                    outcome, recorded_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    turn_key,
                    event_name,
                    operation,
                    max(0, int(duration_us)),
                    outcome,
                    utc_now(),
                ),
            )

    def record_intervention(
        self,
        session_id: str,
        turn_key: str,
        *,
        policy_id: str,
        policy_revision: str,
        triggering_fact: str,
        requested_effect: str,
        rendered_effect: str,
        host_capability: str,
        disposition: str,
        message_chars: int,
    ) -> int:
        """Record effect metadata without storing guidance content."""

        self.ensure_current_schema()
        if disposition not in {"emitted", "suppressed", "unsupported", "failed"}:
            raise ValueError(f"unknown intervention disposition: {disposition}")
        if message_chars < 0:
            raise ValueError("message_chars cannot be negative")
        with self.connect() as conn:
            event = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS id FROM events "
                "WHERE session_id = ? AND turn_key = ?",
                (session_id, turn_key),
            ).fetchone()
            cursor = conn.execute(
                """
                INSERT INTO interventions(
                    session_id, turn_key, policy_id, policy_revision,
                    triggering_fact, requested_effect, rendered_effect,
                    host_capability, disposition, message_chars,
                    event_id_at_emit, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    turn_key,
                    policy_id,
                    policy_revision,
                    triggering_fact,
                    requested_effect,
                    rendered_effect,
                    host_capability,
                    disposition,
                    message_chars,
                    int(event["id"] or 0) if event else 0,
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def record_vault_receipt(
        self,
        session_id: str,
        *,
        cause: str,
        vault_revision: str,
        payload_digest: str,
        notes_selected: int,
        head_chars: int,
        chars_emitted: int,
        chars_omitted: int,
        disposition: str,
        reason_code: str | None = None,
    ) -> int:
        """Record one vault delivery decision as counts, digests, and codes."""

        row = self._vault_receipt_row(
            session_id,
            cause=cause,
            vault_revision=vault_revision,
            payload_digest=payload_digest,
            notes_selected=notes_selected,
            head_chars=head_chars,
            chars_emitted=chars_emitted,
            chars_omitted=chars_omitted,
            disposition=disposition,
            reason_code=reason_code,
        )
        with self.connect() as conn:
            return self._insert_vault_receipt(conn, row)

    def record_vault_receipt_once(
        self, session_id: str, *, exclude_cause: str, **fields: Any
    ) -> int | None:
        """Record a receipt only if the session has none of another cause.

        The check and the insert share one immediate transaction, so when
        several events race to make the first delivery, exactly one records it.
        Return the new row id, or ``None`` when another receipt was already
        there.
        """

        row = self._vault_receipt_row(session_id, **fields)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            found = conn.execute(
                "SELECT 1 FROM vault_receipts "
                "WHERE session_id = ? AND cause != ? LIMIT 1",
                (session_id, exclude_cause),
            ).fetchone()
            if found is not None:
                return None
            return self._insert_vault_receipt(conn, row)

    def _vault_receipt_row(
        self,
        session_id: str,
        *,
        cause: str,
        vault_revision: str,
        payload_digest: str,
        notes_selected: int,
        head_chars: int,
        chars_emitted: int,
        chars_omitted: int,
        disposition: str,
        reason_code: str | None = None,
    ) -> tuple[Any, ...]:
        """Validate one vault receipt and return its row values."""

        self.ensure_current_schema()
        if cause not in VAULT_CAUSES:
            raise ValueError("unknown vault cause")
        if disposition not in VAULT_DISPOSITIONS:
            raise ValueError("unknown vault disposition")
        if reason_code is not None and reason_code not in VAULT_REASONS:
            raise ValueError("unknown vault reason code")
        for name, value in (
            ("vault_revision", vault_revision),
            ("payload_digest", payload_digest),
        ):
            if not isinstance(value, str) or not _VAULT_DIGEST.match(value):
                raise ValueError(f"{name} must be a 16 character hex digest")
        for value in (notes_selected, head_chars, chars_emitted, chars_omitted):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("vault receipt counts must be non-negative integers")
        return (
            session_id,
            cause,
            vault_revision,
            payload_digest,
            notes_selected,
            head_chars,
            chars_emitted,
            chars_omitted,
            disposition,
            reason_code,
            utc_now(),
        )

    @staticmethod
    def _insert_vault_receipt(conn: sqlite3.Connection, row: tuple[Any, ...]) -> int:
        cursor = conn.execute(
            """
            INSERT INTO vault_receipts(
                session_id, cause, vault_revision, payload_digest,
                notes_selected, head_chars, chars_emitted, chars_omitted,
                disposition, reason_code, recorded_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )
        return int(cursor.lastrowid)

    def latest_vault_delivery(self, session_id: str) -> dict[str, Any] | None:
        """Return the newest receipt that put vault context into the session."""

        self.ensure_current_schema()
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT vault_revision, payload_digest, disposition, cause
                  FROM vault_receipts
                 WHERE session_id = ?
                   AND disposition IN ({_sql_choices(VAULT_EMITTED)})
                 ORDER BY id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def has_vault_receipt(
        self, session_id: str, *, exclude_cause: str | None = None
    ) -> bool:
        """Return whether the session has a receipt, ignoring one cause if given."""

        self.ensure_current_schema()
        query = "SELECT 1 FROM vault_receipts WHERE session_id = ?"
        params: list[Any] = [session_id]
        if exclude_cause is not None:
            query += " AND cause != ?"
            params.append(exclude_cause)
        with self.connect() as conn:
            row = conn.execute(f"{query} LIMIT 1", params).fetchone()
        return row is not None

    def vault_compaction_due(self, session_id: str) -> bool:
        """Return whether a compaction still needs its vault delivery.

        Hosts send different events after a compaction, so each compaction is
        counted by its ``PreCompact`` event. A compaction is handled once a
        compact receipt emitted context or reported why it could not. With no
        ``PreCompact`` event at all, every compaction is due.
        """

        self.ensure_current_schema()
        with self.connect() as conn:
            compactions, handled = self._vault_compaction_counts(conn, session_id)
        return compactions == 0 or compactions > handled

    def vault_compaction_pending(self, session_id: str) -> bool:
        """Return whether a recorded compaction has not had its vault delivery.

        Unlike ``vault_compaction_due``, a session with no ``PreCompact`` event
        has nothing pending.
        """

        self.ensure_current_schema()
        with self.connect() as conn:
            compactions, handled = self._vault_compaction_counts(conn, session_id)
        return compactions > handled

    def vault_request_pending(self, session_id: str) -> bool:
        """Return whether a prompt reload is still waiting for a tool result.

        A host that cannot add context from a prompt records the request as
        ``host_unsupported``. It stays pending until a later receipt handled a
        delivery, or a later request receipt recorded the reload.
        """

        self.ensure_current_schema()
        with self.connect() as conn:
            return self._vault_request_pending(conn, session_id)

    def record_vault_request_receipt(
        self, session_id: str, **fields: Any
    ) -> int | None:
        """Record the reload of a pending request, once.

        The pending check and the insert share one immediate transaction, so
        when several tool results race, exactly one records the reload. Return
        the new row id, or ``None`` when nothing was pending.
        """

        row = self._vault_receipt_row(session_id, **fields)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._vault_request_pending(conn, session_id):
                return None
            return self._insert_vault_receipt(conn, row)

    @staticmethod
    def _vault_request_pending(conn: sqlite3.Connection, session_id: str) -> bool:
        row = conn.execute(
            f"""
            SELECT
                (SELECT COALESCE(MAX(id), 0) FROM vault_receipts
                  WHERE session_id = ? AND cause = 'request'
                    AND reason_code = 'host_unsupported'),
                (SELECT COALESCE(MAX(id), 0) FROM vault_receipts
                  WHERE session_id = ?
                    AND (disposition IN ({_sql_choices(VAULT_COMPACTION_HANDLED)})
                         OR (cause = 'request'
                             AND COALESCE(reason_code, '') != 'host_unsupported')))
            """,
            (session_id, session_id),
        ).fetchone()
        return int(row[0]) > int(row[1])

    def record_vault_compaction_receipt(
        self, session_id: str, **fields: Any
    ) -> int | None:
        """Record a compact receipt only while a compaction is still due.

        The due check and the insert share one immediate transaction, so when
        several events race to handle one compaction, exactly one records it.
        Return the new row id, or ``None`` when the compaction was already
        handled.
        """

        row = self._vault_receipt_row(session_id, **fields)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            compactions, handled = self._vault_compaction_counts(conn, session_id)
            if compactions and compactions <= handled:
                return None
            return self._insert_vault_receipt(conn, row)

    @staticmethod
    def _vault_compaction_counts(
        conn: sqlite3.Connection, session_id: str
    ) -> tuple[int, int]:
        """Return the compactions seen and the compact receipts that handled one."""

        row = conn.execute(
            f"""
            SELECT
                (SELECT COUNT(*) FROM events
                  WHERE session_id = ? AND event_name = 'PreCompact'),
                (SELECT COUNT(*) FROM vault_receipts
                  WHERE session_id = ? AND cause = 'compact'
                    AND disposition IN ({_sql_choices(VAULT_COMPACTION_HANDLED)}))
            """,
            (session_id, session_id),
        ).fetchone()
        return int(row[0]), int(row[1])

    def vault_summary(
        self, days: int = 30, *, session_id: str | None = None
    ) -> dict[str, Any]:
        """Report selected, emitted, deferred, and unavailable separately."""

        self.ensure_current_schema()
        since = (
            (datetime.now(UTC) - timedelta(days=max(1, days)))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        clause = "recorded_at >= ?"
        params: list[Any] = [since]
        if session_id:
            clause += " AND session_id = ?"
            params.append(session_id)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT disposition, reason_code, notes_selected, head_chars,
                       chars_emitted, chars_omitted
                  FROM vault_receipts
                 WHERE {clause}
                """,
                params,
            ).fetchall()
        emitted = [row for row in rows if row["disposition"] in VAULT_EMITTED]
        reasons: dict[str, int] = {}
        for row in rows:
            if row["reason_code"]:
                reasons[row["reason_code"]] = reasons.get(row["reason_code"], 0) + 1

        def count(disposition: str) -> int:
            return sum(1 for row in rows if row["disposition"] == disposition)

        return {
            "receipts": len(rows),
            "selected": sum(1 for row in rows if int(row["notes_selected"]) > 0),
            "emitted": len(emitted),
            "chars_emitted": sum(int(row["chars_emitted"]) for row in emitted),
            "head_chars": sum(int(row["head_chars"]) for row in emitted),
            "chars_omitted": sum(int(row["chars_omitted"]) for row in rows),
            "truncated": count("truncated"),
            "deferred": count("deferred"),
            "unavailable": count("unavailable"),
            "degraded": count("degraded"),
            "skipped": count("skipped"),
            "withheld": count("withheld"),
            "reasons": dict(sorted(reasons.items())),
        }

    def record_intervention_outcome(
        self, session_id: str, turn_key: str, outcome: str
    ) -> int | None:
        """Associate an observed result with the latest bounded intervention."""

        valid = {
            "suggested_action_observed",
            "failure_repeated",
            "check_started",
            "check_passed",
            "check_failed",
            "turn_completed",
            "user_disabled_advice",
            "no_observable_response",
        }
        if outcome not in valid:
            raise ValueError(f"unknown intervention outcome: {outcome}")
        self.ensure_current_schema()
        with self.connect() as conn:
            intervention = conn.execute(
                """
                SELECT i.id, i.event_id_at_emit,
                       (
                           SELECT COUNT(*) FROM events e
                            WHERE e.session_id = i.session_id
                              AND e.turn_key = i.turn_key
                              AND e.id > i.event_id_at_emit
                       ) AS event_distance
                  FROM interventions i
                 WHERE i.session_id = ? AND i.turn_key = ?
                   AND i.disposition = 'emitted'
                 ORDER BY i.id DESC
                 LIMIT 1
                """,
                (session_id, turn_key),
            ).fetchone()
            if (
                not intervention
                or int(intervention["event_distance"] or 0) > INTERVENTION_EVENT_WINDOW
            ):
                return None
            distance = int(intervention["event_distance"] or 0)
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO intervention_outcomes(
                    intervention_id, outcome, event_distance, observed_at
                ) VALUES(?, ?, ?, ?)
                """,
                (int(intervention["id"]), outcome, distance, utc_now()),
            )
            return int(intervention["id"]) if cursor.rowcount else None

    def close_intervention_window(self, session_id: str, turn_key: str) -> None:
        """Mark emitted interventions that had no observed result in the turn."""

        self.ensure_current_schema()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO intervention_outcomes(
                    intervention_id, outcome, event_distance, observed_at
                )
                SELECT i.id, 'no_observable_response', 0, ?
                  FROM interventions i
                 WHERE i.session_id = ? AND i.turn_key = ?
                   AND i.disposition = 'emitted'
                   AND NOT EXISTS (
                       SELECT 1 FROM intervention_outcomes o
                        WHERE o.intervention_id = i.id
                   )
                """,
                (utc_now(), session_id, turn_key),
            )

    def record_intervention_feedback(
        self, intervention_id: int, judgment: str
    ) -> dict[str, Any]:
        """Record one immutable human judgment for an intervention."""

        if judgment not in {"useful", "neutral", "false_or_unnecessary"}:
            raise ValueError(f"unknown intervention judgment: {judgment}")
        self.ensure_current_schema()
        with self.connect() as conn:
            intervention = conn.execute(
                "SELECT id FROM interventions WHERE id = ?", (intervention_id,)
            ).fetchone()
            if not intervention:
                raise ValueError("intervention does not exist")
            existing = conn.execute(
                "SELECT * FROM intervention_feedback WHERE intervention_id = ?",
                (intervention_id,),
            ).fetchone()
            if existing:
                if str(existing["judgment"]) != judgment:
                    raise ValueError("intervention judgment is immutable")
                return dict(existing)
            conn.execute(
                "INSERT INTO intervention_feedback("
                "intervention_id, judgment, recorded_at) VALUES(?, ?, ?)",
                (intervention_id, judgment, utc_now()),
            )
            row = conn.execute(
                "SELECT * FROM intervention_feedback WHERE intervention_id = ?",
                (intervention_id,),
            ).fetchone()
        return dict(row)

    def latest_intervention(
        self, session_id: str | None = None
    ) -> dict[str, Any] | None:
        self.ensure_current_schema()
        where = "WHERE i.session_id = ?" if session_id else ""
        parameters: tuple[str, ...] = (session_id,) if session_id else ()
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT i.*
                  FROM interventions i
                  {where}
                 ORDER BY i.id DESC
                 LIMIT 1
                """,
                parameters,
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            feedback = conn.execute(
                "SELECT judgment, recorded_at FROM intervention_feedback "
                "WHERE intervention_id = ?",
                (int(row["id"]),),
            ).fetchone()
            outcomes = conn.execute(
                "SELECT outcome, event_distance, observed_at "
                "FROM intervention_outcomes WHERE intervention_id = ? "
                "ORDER BY id",
                (int(row["id"]),),
            ).fetchall()
        result["feedback"] = dict(feedback) if feedback else None
        result["outcomes"] = [dict(item) for item in outcomes]
        return result

    def intervention_summary(
        self, days: int = 30, *, session_id: str | None = None
    ) -> dict[str, Any]:
        """Return association-only intervention counts and policy views."""

        self.ensure_current_schema()
        since = (
            (datetime.now(UTC) - timedelta(days=max(1, days)))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        clause = "i.created_at >= ?"
        params: list[Any] = [since]
        if session_id:
            clause += " AND i.session_id = ?"
            params.append(session_id)
        with self.connect() as conn:
            dispositions = conn.execute(
                f"""
                SELECT disposition, COUNT(*) AS count
                  FROM interventions i
                 WHERE {clause}
                 GROUP BY disposition ORDER BY disposition
                """,
                params,
            ).fetchall()
            outcomes = conn.execute(
                f"""
                SELECT o.outcome, COUNT(*) AS count
                  FROM intervention_outcomes o
                  JOIN interventions i ON i.id = o.intervention_id
                 WHERE {clause}
                 GROUP BY o.outcome ORDER BY o.outcome
                """,
                params,
            ).fetchall()
            policies = conn.execute(
                f"""
                SELECT i.policy_id, COUNT(DISTINCT i.id) AS interventions,
                       SUM(CASE WHEN o.outcome = 'suggested_action_observed'
                                THEN 1 ELSE 0 END) AS followed,
                       SUM(CASE WHEN o.outcome = 'user_disabled_advice'
                                THEN 1 ELSE 0 END) AS disabled,
                       COUNT(DISTINCT CASE
                           WHEN f.judgment = 'false_or_unnecessary' THEN i.id
                       END) AS false_or_unnecessary,
                       COUNT(DISTINCT f.intervention_id) AS rated
                  FROM interventions i
                  LEFT JOIN intervention_outcomes o
                    ON o.intervention_id = i.id
                  LEFT JOIN intervention_feedback f
                    ON f.intervention_id = i.id
                 WHERE {clause}
                 GROUP BY i.policy_id ORDER BY i.policy_id
                """,
                params,
            ).fetchall()
        disposition_counts = {
            str(row["disposition"]): int(row["count"]) for row in dispositions
        }
        outcome_counts = {str(row["outcome"]): int(row["count"]) for row in outcomes}
        emitted = disposition_counts.get("emitted", 0)
        rated = sum(int(row["rated"] or 0) for row in policies)
        false_or_unnecessary = sum(
            int(row["false_or_unnecessary"] or 0) for row in policies
        )
        return {
            "scope": "session" if session_id else "cohort",
            "session_id": session_id,
            "dispositions": disposition_counts,
            "outcomes": outcome_counts,
            "policies": [dict(row) for row in policies],
            "emitted": emitted,
            "followed_rate": (
                outcome_counts.get("suggested_action_observed", 0) / emitted
                if emitted
                else None
            ),
            "disable_rate": (
                outcome_counts.get("user_disabled_advice", 0) / emitted
                if emitted
                else None
            ),
            "rated": rated,
            "false_or_unnecessary": false_or_unnecessary,
            "false_or_unnecessary_rate": (
                false_or_unnecessary / rated if rated else None
            ),
            "claim_boundary": (
                "Observed results are associated with interventions. "
                "They do not establish causation."
            ),
        }

    def record_nudge(self, session_id: str, turn_key: str, policy_id: str) -> bool:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            turn_row = conn.execute(
                "SELECT nudge_count FROM turns WHERE session_id = ? AND turn_key = ?",
                (session_id, turn_key),
            ).fetchone()
            session_row = conn.execute(
                "SELECT nudge_count FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not turn_row or not session_row:
                return False
            if int(turn_row["nudge_count"]) >= MAX_NUDGES_PER_TURN:
                return False
            if int(session_row["nudge_count"]) >= MAX_NUDGES_PER_SESSION:
                return False
            before = conn.total_changes
            conn.execute(
                "INSERT OR IGNORE INTO nudges(session_id, turn_key, policy_id, created_at) "
                "VALUES(?, ?, ?, ?)",
                (session_id, turn_key, policy_id, now),
            )
            if conn.total_changes == before:
                return False
            self._increment(
                conn,
                "sessions",
                "session_id",
                session_id,
                {"nudge_count": 1},
            )
            self._increment_turn(
                conn,
                session_id,
                turn_key,
                {"nudge_count": 1},
            )
        return True

    def has_session_nudge(self, session_id: str, policy_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM nudges WHERE session_id = ? AND policy_id = ? LIMIT 1",
                (session_id, policy_id),
            ).fetchone()
        return row is not None

    def pin_capability_pack(
        self,
        session_id: str,
        *,
        pack_id: str,
        pack_digest: str,
        sequence: int,
    ) -> dict[str, Any]:
        """Pin a verified pack once; later activation cannot move this session."""

        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO session_capability_packs(
                    session_id, pack_id, pack_digest, sequence, pinned_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (session_id, pack_id, pack_digest, sequence, now),
            )
            row = conn.execute(
                "SELECT * FROM session_capability_packs WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return dict(row) if row else {}

    def get_session_capability_pack(self, session_id: str) -> dict[str, Any] | None:
        try:
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT * FROM session_capability_packs WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            self._migrate()
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT * FROM session_capability_packs WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        return dict(row) if row else None

    def knowledge_emitted_ids(self, session_id: str) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT card_id
                  FROM knowledge_receipts
                 WHERE session_id = ? AND disposition = 'emitted'
                """,
                (session_id,),
            ).fetchall()
        return {str(row["card_id"]) for row in rows}

    def knowledge_selected_ids(self, session_id: str) -> set[str]:
        """Return cards that emitted or represented an observe counterfactual."""

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT card_id
                  FROM knowledge_receipts
                 WHERE session_id = ?
                   AND disposition IN ('emitted', 'observed')
                """,
                (session_id,),
            ).fetchall()
        return {str(row["card_id"]) for row in rows}

    def record_knowledge_suppression(
        self,
        session_id: str,
        turn_key: str,
        *,
        event_name: str,
        pack_id: str,
        pack_digest: str,
        card_id: str,
        score: int,
        match_reason: str,
        suppression_reason: str,
        prompt_chars: int | None = None,
        task_type: str = "unknown",
        risk: str = "normal",
    ) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if prompt_chars is not None:
                self._ensure_turn(
                    conn,
                    session_id,
                    turn_key,
                    prompt_chars=prompt_chars,
                    task_type=task_type,
                    risk=risk,
                )
            self._insert_knowledge_receipt(
                conn,
                session_id,
                turn_key,
                event_name=event_name,
                pack_id=pack_id,
                pack_digest=pack_digest,
                card_id=card_id,
                score=score,
                match_reason=match_reason,
                disposition="suppressed",
                suppression_reason=suppression_reason,
                emitted_chars=0,
            )

    def record_knowledge_observation(
        self,
        session_id: str,
        turn_key: str,
        *,
        event_name: str,
        pack_id: str,
        pack_digest: str,
        card_id: str,
        score: int,
        match_reason: str,
        prompt_chars: int,
        task_type: str,
        risk: str,
    ) -> None:
        """Record a would-have-emitted selection without injecting guidance."""

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_turn(
                conn,
                session_id,
                turn_key,
                prompt_chars=prompt_chars,
                task_type=task_type,
                risk=risk,
            )
            self._insert_knowledge_receipt(
                conn,
                session_id,
                turn_key,
                event_name=event_name,
                pack_id=pack_id,
                pack_digest=pack_digest,
                card_id=card_id,
                score=score,
                match_reason=match_reason,
                disposition="observed",
                suppression_reason=None,
                emitted_chars=0,
            )

    def record_knowledge_pending(
        self,
        session_id: str,
        turn_key: str,
        *,
        event_name: str,
        pack_id: str,
        pack_digest: str,
        card_id: str,
        score: int,
        match_reason: str,
        prompt_chars: int,
        task_type: str,
        risk: str,
    ) -> None:
        """Record guidance selected at prompt time for later host delivery."""

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_turn(
                conn,
                session_id,
                turn_key,
                prompt_chars=prompt_chars,
                task_type=task_type,
                risk=risk,
            )
            self._insert_knowledge_receipt(
                conn,
                session_id,
                turn_key,
                event_name=event_name,
                pack_id=pack_id,
                pack_digest=pack_digest,
                card_id=card_id,
                score=score,
                match_reason=match_reason,
                disposition="pending",
                suppression_reason=None,
                emitted_chars=0,
            )

    def pending_knowledge_receipt(
        self, session_id: str, turn_key: str
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM knowledge_receipts
                 WHERE session_id = ? AND turn_key = ?
                   AND disposition = 'pending'
                 ORDER BY id ASC
                 LIMIT 1
                """,
                (session_id, turn_key),
            ).fetchone()
        return dict(row) if row else None

    def mark_knowledge_pending_delivered(
        self, session_id: str, turn_key: str, card_id: str
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE knowledge_receipts
                   SET disposition = 'delivered'
                 WHERE session_id = ? AND turn_key = ?
                   AND card_id = ? AND disposition = 'pending'
                """,
                (session_id, turn_key, card_id),
            )

    def record_knowledge_advisory(
        self,
        session_id: str,
        turn_key: str,
        *,
        event_name: str,
        pack_id: str,
        pack_digest: str,
        card_id: str,
        score: int,
        match_reason: str,
        emitted_chars: int,
        prompt_chars: int | None = None,
        task_type: str = "unknown",
        risk: str = "normal",
    ) -> tuple[bool, str | None]:
        """Atomically enforce combined nudge and knowledge-context budgets."""

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if prompt_chars is not None:
                self._ensure_turn(
                    conn,
                    session_id,
                    turn_key,
                    prompt_chars=prompt_chars,
                    task_type=task_type,
                    risk=risk,
                )
            turn_row = conn.execute(
                "SELECT nudge_count FROM turns WHERE session_id = ? AND turn_key = ?",
                (session_id, turn_key),
            ).fetchone()
            session_row = conn.execute(
                "SELECT nudge_count FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            usage = conn.execute(
                """
                SELECT COUNT(*) AS envelopes,
                       COALESCE(SUM(emitted_chars), 0) AS characters
                  FROM knowledge_receipts
                 WHERE session_id = ? AND disposition = 'emitted'
                """,
                (session_id,),
            ).fetchone()
            duplicate = conn.execute(
                "SELECT 1 FROM nudges WHERE session_id = ? AND policy_id = ? LIMIT 1",
                (session_id, card_id),
            ).fetchone()

            suppression_reason: str | None = None
            if not turn_row or not session_row:
                suppression_reason = "missing-turn"
            elif duplicate:
                suppression_reason = "dedupe"
            elif emitted_chars <= 0 or emitted_chars > MAX_KNOWLEDGE_CHARS_PER_ENVELOPE:
                suppression_reason = "envelope-budget"
            elif int(turn_row["nudge_count"]) >= MAX_NUDGES_PER_TURN:
                suppression_reason = "turn-budget"
            elif int(session_row["nudge_count"]) >= MAX_NUDGES_PER_SESSION:
                suppression_reason = "session-budget"
            elif int(usage["envelopes"] or 0) >= MAX_KNOWLEDGE_ENVELOPES_PER_SESSION:
                suppression_reason = "knowledge-envelope-budget"
            elif (
                int(usage["characters"] or 0) + emitted_chars
                > MAX_KNOWLEDGE_CHARS_PER_SESSION
            ):
                suppression_reason = "knowledge-character-budget"

            if suppression_reason:
                self._insert_knowledge_receipt(
                    conn,
                    session_id,
                    turn_key,
                    event_name=event_name,
                    pack_id=pack_id,
                    pack_digest=pack_digest,
                    card_id=card_id,
                    score=score,
                    match_reason=match_reason,
                    disposition="suppressed",
                    suppression_reason=suppression_reason,
                    emitted_chars=0,
                )
                return False, suppression_reason

            before = conn.total_changes
            conn.execute(
                "INSERT OR IGNORE INTO nudges(session_id, turn_key, policy_id, created_at) "
                "VALUES(?, ?, ?, ?)",
                (session_id, turn_key, card_id, utc_now()),
            )
            if conn.total_changes == before:
                self._insert_knowledge_receipt(
                    conn,
                    session_id,
                    turn_key,
                    event_name=event_name,
                    pack_id=pack_id,
                    pack_digest=pack_digest,
                    card_id=card_id,
                    score=score,
                    match_reason=match_reason,
                    disposition="suppressed",
                    suppression_reason="dedupe",
                    emitted_chars=0,
                )
                return False, "dedupe"

            self._insert_knowledge_receipt(
                conn,
                session_id,
                turn_key,
                event_name=event_name,
                pack_id=pack_id,
                pack_digest=pack_digest,
                card_id=card_id,
                score=score,
                match_reason=match_reason,
                disposition="emitted",
                suppression_reason=None,
                emitted_chars=emitted_chars,
            )
            self._increment(
                conn,
                "sessions",
                "session_id",
                session_id,
                {"nudge_count": 1},
            )
            self._increment_turn(
                conn,
                session_id,
                turn_key,
                {"nudge_count": 1},
            )
        return True, None

    def knowledge_receipt_summary(self, session_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            totals = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN disposition = 'emitted' THEN 1 ELSE 0 END)
                        AS emitted,
                    SUM(CASE WHEN disposition = 'suppressed' THEN 1 ELSE 0 END)
                        AS suppressed,
                    SUM(CASE WHEN disposition = 'observed' THEN 1 ELSE 0 END)
                        AS observed,
                    COALESCE(SUM(
                        CASE WHEN disposition = 'emitted' THEN emitted_chars ELSE 0 END
                    ), 0) AS emitted_chars
                  FROM knowledge_receipts
                 WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            reasons = conn.execute(
                """
                SELECT suppression_reason, COUNT(*) AS count
                  FROM knowledge_receipts
                 WHERE session_id = ? AND disposition = 'suppressed'
                 GROUP BY suppression_reason
                 ORDER BY suppression_reason
                """,
                (session_id,),
            ).fetchall()
        return {
            "emitted": int(totals["emitted"] or 0) if totals else 0,
            "suppressed": int(totals["suppressed"] or 0) if totals else 0,
            "observed": int(totals["observed"] or 0) if totals else 0,
            "emitted_chars": int(totals["emitted_chars"] or 0) if totals else 0,
            "suppression_reasons": {
                str(row["suppression_reason"]): int(row["count"]) for row in reasons
            },
        }

    def latest_knowledge_receipt(
        self, session_id: str, card_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM knowledge_receipts
                 WHERE session_id = ? AND card_id = ?
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (session_id, card_id),
            ).fetchone()
        return dict(row) if row else None

    def record_capability_runtime(
        self,
        session_id: str,
        turn_key: str,
        *,
        event_name: str,
        operation: str,
        duration_us: int,
        context_chars: int = 0,
    ) -> None:
        try:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO capability_runtime_samples(
                        session_id, turn_key, event_name, operation,
                        duration_us, context_chars, recorded_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        turn_key[:128],
                        event_name[:64],
                        operation[:64],
                        max(0, int(duration_us)),
                        max(0, int(context_chars)),
                        utc_now(),
                    ),
                )
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            self._migrate()
            self.record_capability_runtime(
                session_id,
                turn_key,
                event_name=event_name,
                operation=operation,
                duration_us=duration_us,
                context_chars=context_chars,
            )

    @staticmethod
    def _ensure_turn(
        conn: sqlite3.Connection,
        session_id: str,
        turn_key: str,
        *,
        prompt_chars: int,
        task_type: str,
        risk: str,
    ) -> None:
        now = utc_now()
        before = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO turns(
                session_id, turn_key, started_at, prompt_chars, task_type, risk
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                turn_key,
                now,
                max(0, int(prompt_chars)),
                task_type,
                risk,
            ),
        )
        if conn.total_changes > before:
            conn.execute(
                "UPDATE sessions SET turn_count = turn_count + 1, updated_at = ? "
                "WHERE session_id = ?",
                (now, session_id),
            )

    @staticmethod
    def _insert_knowledge_receipt(
        conn: sqlite3.Connection,
        session_id: str,
        turn_key: str,
        *,
        event_name: str,
        pack_id: str,
        pack_digest: str,
        card_id: str,
        score: int,
        match_reason: str,
        disposition: str,
        suppression_reason: str | None,
        emitted_chars: int,
    ) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO knowledge_receipts(
                session_id, turn_key, event_name, pack_id, pack_digest,
                card_id, score, match_reason, disposition,
                suppression_reason, emitted_chars, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                turn_key,
                event_name[:64],
                pack_id[:128],
                pack_digest[:80],
                card_id[:128],
                int(score),
                match_reason[:240],
                disposition,
                suppression_reason,
                int(emitted_chars),
                utc_now(),
            ),
        )

    def latest_turn_key(self, session_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT turn_key FROM turns WHERE session_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return str(row["turn_key"]) if row else None

    def get_turn(self, session_id: str, turn_key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM turns WHERE session_id = ? AND turn_key = ?",
                (session_id, turn_key),
            ).fetchone()
        return dict(row) if row else None

    def finish_turn(self, session_id: str, turn_key: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE turns SET ended_at = ? WHERE session_id = ? AND turn_key = ?",
                (utc_now(), session_id, turn_key),
            )

    def record_compaction(
        self, session_id: str, turn_key: str, event_name: str
    ) -> None:
        self.record_event(
            session_id,
            turn_key,
            event_name=event_name,
            outcome="observed",
        )
        if event_name == "PostCompact":
            with self.connect() as conn:
                self._increment(
                    conn,
                    "sessions",
                    "session_id",
                    session_id,
                    {"compaction_count": 1},
                )

    def end_session(self, session_id: str) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ?, updated_at = ? WHERE session_id = ?",
                (now, now, session_id),
            )

    def record_status_sample(self, session_id: str, metrics: dict[str, Any]) -> None:
        now = utc_now()
        values = (
            metrics.get("cost_usd"),
            metrics.get("input_tokens"),
            metrics.get("output_tokens"),
            metrics.get("cache_read_tokens"),
            metrics.get("cache_creation_tokens"),
            metrics.get("context_used_percent"),
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO status_samples(
                    session_id, cost_usd, input_tokens, output_tokens,
                    cache_read_tokens, cache_creation_tokens,
                    context_used_percent, captured_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, *values, now),
            )
            conn.execute(
                """
                UPDATE sessions
                   SET cost_usd = ?, input_tokens = ?, output_tokens = ?,
                       cache_read_tokens = ?, cache_creation_tokens = ?,
                       context_used_percent = ?,
                       lines_added = COALESCE(?, lines_added),
                       lines_removed = COALESCE(?, lines_removed),
                       updated_at = ?
                 WHERE session_id = ?
                """,
                (
                    *values,
                    metrics.get("lines_added"),
                    metrics.get("lines_removed"),
                    now,
                    session_id,
                ),
            )

    def aggregate(self, days: int = 30) -> dict[str, Any]:
        since = (
            (datetime.now(UTC) - timedelta(days=max(1, days)))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        with self.connect() as conn:
            totals = conn.execute(
                """
                SELECT
                    COUNT(*) AS sessions,
                    COALESCE(SUM(turn_count), 0) AS turns,
                    COALESCE(SUM(tool_count), 0) AS tools,
                    COALESCE(SUM(failure_count), 0) AS failures,
                    COALESCE(SUM(nudge_count), 0) AS nudges,
                    COALESCE(SUM(compaction_count), 0) AS compactions,
                    COALESCE(SUM(subagent_count), 0) AS subagents,
                    COALESCE(SUM(edit_count), 0) AS edits,
                    COALESCE(SUM(validation_count), 0) AS validations,
                    COALESCE(SUM(repeated_action_count), 0) AS repeated_actions,
                    COALESCE(SUM(broad_scan_count), 0) AS broad_scans,
                    COALESCE(SUM(cost_usd), 0) AS cost_usd,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                    COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens
                FROM sessions
                WHERE started_at >= ?
                """,
                (since,),
            ).fetchone()
            modes = conn.execute(
                """
                SELECT mode, COUNT(*) AS sessions,
                       COALESCE(SUM(tool_count), 0) AS tools,
                       COALESCE(SUM(failure_count), 0) AS failures,
                       COALESCE(SUM(cost_usd), 0) AS cost_usd
                  FROM sessions
                 WHERE started_at >= ?
                 GROUP BY mode
                 ORDER BY mode
                """,
                (since,),
            ).fetchall()
            edited_turns = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN code_edit_count > 0 THEN 1 ELSE 0 END) AS edited,
                    SUM(CASE WHEN code_edit_count > 0 AND validation_count > 0
                             THEN 1 ELSE 0 END) AS verified
                  FROM turns
                 WHERE started_at >= ?
                """,
                (since,),
            ).fetchone()
        result = dict(totals) if totals else {}
        result["days"] = days
        result["by_mode"] = [dict(row) for row in modes]
        result["edited_turns"] = int(edited_turns["edited"] or 0)
        result["verified_edited_turns"] = int(edited_turns["verified"] or 0)
        return result

    def record_verification_receipt(
        self, receipt: dict[str, Any], *, project_key: str
    ) -> int:
        self.ensure_current_schema()
        now = utc_now()
        with self.connect() as conn:
            _add_receipt_reason_column(conn)
            conn.execute(
                """
                INSERT INTO check_definitions(
                    project_key, check_id, check_digest, updated_at
                ) VALUES(?, ?, ?, ?)
                ON CONFLICT(project_key, check_id) DO UPDATE SET
                    check_digest = excluded.check_digest,
                    updated_at = excluded.updated_at
                """,
                (
                    project_key,
                    receipt["check_id"],
                    receipt["check_digest"],
                    now,
                ),
            )
            if receipt.get("workspace_digest"):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO workspace_states(
                        workspace_digest, project_key, git_commit, dirty,
                        observed_at
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        receipt["workspace_digest"],
                        project_key,
                        receipt.get("git_commit"),
                        int(bool(receipt["dirty"])),
                        now,
                    ),
                )
            cursor = conn.execute(
                """
                INSERT INTO verification_receipts(
                    project_key, check_id, check_digest, workspace_digest,
                    git_commit, dirty, started_at, finished_at, duration_ms,
                    exit_code, result, runner_version, host, session_id,
                    turn_id, schema_version, reason_code
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_key,
                    receipt["check_id"],
                    receipt["check_digest"],
                    receipt.get("workspace_digest"),
                    receipt.get("git_commit"),
                    int(bool(receipt["dirty"])),
                    receipt["started_at"],
                    receipt["finished_at"],
                    int(receipt["duration_ms"]),
                    receipt.get("exit_code"),
                    receipt["result"],
                    receipt["runner_version"],
                    receipt.get("host"),
                    receipt.get("session_id"),
                    receipt.get("turn_id"),
                    int(receipt["schema_version"]),
                    receipt.get("reason_code"),
                ),
            )
            return int(cursor.lastrowid)

    def verification_receipt(self, receipt_id: int) -> dict[str, Any] | None:
        self.ensure_current_schema()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM verification_receipts WHERE id = ?",
                (receipt_id,),
            ).fetchone()
        return dict(row) if row else None

    def verification_receipts(
        self, project_key: str, *, check_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.ensure_current_schema()
        query = "SELECT * FROM verification_receipts WHERE project_key = ?"
        values: list[Any] = [project_key]
        if check_id:
            query += " AND check_id = ?"
            values.append(check_id)
        query += " ORDER BY id DESC"
        with self.connect() as conn:
            rows = conn.execute(query, values).fetchall()
        return [dict(row) for row in rows]
