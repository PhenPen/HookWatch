"""SQLite connections, schema migrations and row models."""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.lifecycle import AttemptOutcome, EventStatus, FailureKind

# Each entry is one schema version. Statements are executed in order inside a
# transaction, then PRAGMA user_version is bumped. SQLite stores that integer in
# the file itself, and PRAGMA user_version is how the next startup knows which
# migrations are already applied.
MIGRATIONS: list[list[str]] = [
    # Version 1: the original single table, as created in Stage 4.
    [
        """
        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,
            event_id    TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            data        TEXT NOT NULL,
            received_at TEXT NOT NULL
        )
        """,
    ],
    # Version 2: lifecycle status and database enforced idempotency.
    [
        # Rows stored before idempotency existed may contain duplicates. The
        # unique index below cannot be created while they exist, so keep the
        # oldest row of each (source, event_id) pair and discard the rest.
        """
        DELETE FROM events
        WHERE id NOT IN (SELECT MIN(id) FROM events GROUP BY source, event_id)
        """,
        """
        ALTER TABLE events ADD COLUMN status TEXT NOT NULL DEFAULT 'received'
            CHECK (status IN ('received', 'processing', 'processed', 'failed'))
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS events_source_event_id
            ON events (source, event_id)
        """,
    ],
    # Version 3: one row per processing attempt, giving a history rather than a
    # single counter.
    [
        """
        CREATE TABLE IF NOT EXISTS attempts (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            event_row_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            attempt_number INTEGER NOT NULL,
            started_at     TEXT NOT NULL,
            finished_at    TEXT,
            outcome        TEXT CHECK (outcome IN ('succeeded', 'failed')),
            error          TEXT,
            UNIQUE (event_row_id, attempt_number)
        )
        """,
    ],
    # Version 4: information the processing engine needs. Failure kind says
    # whether an attempt was retryable or permanent. The dead-letter timestamp
    # marks an event that will not be retried again.
    [
        """
        ALTER TABLE attempts ADD COLUMN failure_kind TEXT
            CHECK (failure_kind IN ('retryable', 'permanent'))
        """,
        "ALTER TABLE events ADD COLUMN dead_lettered_at TEXT",
        "CREATE INDEX IF NOT EXISTS events_status_idx ON events (status)",
    ],
    # Version 5: replay support. replay_count identifies the current retry
    # cycle for an event, so a replay starts with a fresh attempt budget while
    # keeping every earlier attempt. The replays table is the audit trail: who
    # asked for the replay, when, and why.
    [
        "ALTER TABLE events ADD COLUMN replay_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE attempts ADD COLUMN replay_number INTEGER NOT NULL DEFAULT 0",
        """
        CREATE TABLE IF NOT EXISTS replays (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            event_row_id  INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            replay_number INTEGER NOT NULL,
            requested_at  TEXT NOT NULL,
            requested_by  TEXT NOT NULL,
            reason        TEXT NOT NULL,
            UNIQUE (event_row_id, replay_number)
        )
        """,
    ],
]


@dataclass(frozen=True)
class StoredEvent:
    """One event as it exists in the database.

    This is a read model, not the request contract: it carries fields that the
    sender never provided, such as the database id, the current status and the
    moment we received the event.
    """

    id: int
    source: str
    event_id: str
    event_type: str
    occurred_at: datetime
    data: dict[str, Any]
    received_at: datetime
    status: EventStatus
    dead_lettered_at: datetime | None
    replay_count: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "StoredEvent":
        return cls(
            id=row["id"],
            source=row["source"],
            event_id=row["event_id"],
            event_type=row["event_type"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            data=json.loads(row["data"]),
            received_at=datetime.fromisoformat(row["received_at"]),
            status=EventStatus(row["status"]),
            dead_lettered_at=(
                datetime.fromisoformat(row["dead_lettered_at"])
                if row["dead_lettered_at"] is not None
                else None
            ),
            replay_count=row["replay_count"],
        )


@dataclass(frozen=True)
class Attempt:
    """One recorded attempt to process an event."""

    id: int
    event_row_id: int
    attempt_number: int
    replay_number: int
    started_at: datetime
    finished_at: datetime | None
    outcome: AttemptOutcome | None
    failure_kind: FailureKind | None
    error: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Attempt":
        return cls(
            id=row["id"],
            event_row_id=row["event_row_id"],
            attempt_number=row["attempt_number"],
            replay_number=row["replay_number"],
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=(
                datetime.fromisoformat(row["finished_at"])
                if row["finished_at"] is not None
                else None
            ),
            outcome=(
                AttemptOutcome(row["outcome"]) if row["outcome"] is not None else None
            ),
            failure_kind=(
                FailureKind(row["failure_kind"])
                if row["failure_kind"] is not None
                else None
            ),
            error=row["error"],
        )


@dataclass(frozen=True)
class Replay:
    """One recorded operator request to process an event again."""

    id: int
    event_row_id: int
    replay_number: int
    requested_at: datetime
    requested_by: str
    reason: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Replay":
        return cls(
            id=row["id"],
            event_row_id=row["event_row_id"],
            replay_number=row["replay_number"],
            requested_at=datetime.fromisoformat(row["requested_at"]),
            requested_by=row["requested_by"],
            reason=row["reason"],
        )


def schema_version() -> int:
    """The version a fully migrated database should report."""
    return len(MIGRATIONS)


def connect(database_path: Path) -> sqlite3.Connection:
    """Open a connection with row access by column name and foreign keys on.

    SQLite disables foreign key enforcement per connection by default, so the
    pragma here is what makes the references in the schema mean anything.

    Connections are short-lived and opened by whichever method needs one. That
    keeps this module free of shared mutable state, so no locking or thread
    affinity is needed.
    """
    connection = sqlite3.connect(database_path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def migrate(database_path: Path) -> int:
    """Apply every migration newer than the database version.

    Returns how many migrations ran. A fresh database runs all of them; an
    existing one runs only the new ones. Each migration commits before the next
    starts, so a failure leaves the database at the last fully applied version
    rather than half way through a version.
    """
    with closing(connect(database_path)) as connection:
        current = connection.execute("PRAGMA user_version").fetchone()[0]
        applied = 0

        for version in range(current, len(MIGRATIONS)):
            with connection:
                for statement in MIGRATIONS[version]:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {version + 1}")
            applied += 1

        return applied
