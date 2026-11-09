"""Database access for webhook events and processing attempts.

All SQL for events lives here. Routes, services and later the MCP layer call
these methods and never see a connection, a cursor or a table name. That
boundary is what makes it possible to replace SQLite with another database
without rewriting request handling.
"""

import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from app.db import Attempt, StoredEvent, connect
from app.lifecycle import AttemptOutcome, EventStatus
from app.schemas.event import WebhookEvent


class EventRepository:
    """Stores and retrieves webhook events and their processing attempts."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def insert_if_new(
        self,
        source: str,
        event: WebhookEvent,
        received_at: datetime | None = None,
    ) -> tuple[int, bool]:
        """Insert an event unless it is already stored.

        Returns (record_id, created). Idempotency is enforced by the unique
        index on (source, event_id) plus ON CONFLICT DO NOTHING, so the
        database decides under its own lock. Checking with a SELECT first would
        leave a window in which two concurrent deliveries both believe they are
        first.

        Timestamps are normalised to UTC before storage. SQLite compares TEXT,
        so two ISO strings with different offsets would sort incorrectly.

        Why source and event_id together: two providers may legitimately use the
        same event id, and those are different events.
        """
        occurred_at = event.occurred_at.astimezone(timezone.utc)
        received = (received_at or datetime.now(timezone.utc)).astimezone(timezone.utc)

        with closing(connect(self.database_path)) as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO events
                    (source, event_id, event_type, occurred_at, data, received_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source, event_id) DO NOTHING
                """,
                (
                    source,
                    event.event_id,
                    event.event_type,
                    occurred_at.isoformat(),
                    json.dumps(event.data),
                    received.isoformat(),
                    EventStatus.RECEIVED.value,
                ),
            )

            if cursor.rowcount == 1:
                return int(cursor.lastrowid), True

            # No row was inserted, so lastrowid still refers to some earlier
            # insert in this connection. Read the existing row instead.
            row = connection.execute(
                "SELECT id FROM events WHERE source = ? AND event_id = ?",
                (source, event.event_id),
            ).fetchone()
            return int(row["id"]), False

    def get(self, record_id: int) -> StoredEvent | None:
        """Return one stored event, or None when no row has that id."""
        with closing(connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?",
                (record_id,),
            ).fetchone()

        if row is None:
            return None
        return StoredEvent.from_row(row)

    def find(self, source: str, event_id: str) -> StoredEvent | None:
        """Return the stored event for one source and event id, if present."""
        with closing(connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE source = ? AND event_id = ?",
                (source, event_id),
            ).fetchone()

        if row is None:
            return None
        return StoredEvent.from_row(row)

    def list(self, limit: int = 50) -> list[StoredEvent]:
        """Return stored events, newest first.

        Ordering uses the insertion id rather than received_at, because two
        events received in the same microsecond would otherwise have an
        unstable order.
        """
        with closing(connect(self.database_path)) as connection:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

        return [StoredEvent.from_row(row) for row in rows]

    def begin_attempt(
        self,
        record_id: int,
        started_at: datetime | None = None,
    ) -> int:
        """Open a new attempt and mark the event as processing.

        Both writes happen in one transaction, so the event cannot end up
        processing with no attempt recorded. Transition rules are checked by the
        service before calling this; the repository only writes what it is told.
        """
        started = (started_at or datetime.now(timezone.utc)).astimezone(timezone.utc)

        with closing(connect(self.database_path)) as connection, connection:
            connection.execute(
                "UPDATE events SET status = ? WHERE id = ?",
                (EventStatus.PROCESSING.value, record_id),
            )
            next_number = connection.execute(
                """
                SELECT COALESCE(MAX(attempt_number), 0) + 1
                FROM attempts
                WHERE event_row_id = ?
                """,
                (record_id,),
            ).fetchone()[0]
            cursor = connection.execute(
                """
                INSERT INTO attempts (event_row_id, attempt_number, started_at)
                VALUES (?, ?, ?)
                """,
                (record_id, next_number, started.isoformat()),
            )

        return int(cursor.lastrowid)

    def complete_attempt(
        self,
        attempt_id: int,
        event_status: EventStatus,
        outcome: AttemptOutcome,
        error: str | None = None,
        finished_at: datetime | None = None,
    ) -> bool:
        """Close an open attempt and move its event to a final status.

        Returns False when the attempt is missing or already finished, which is
        what makes finishing twice a detectable mistake instead of a silent
        overwrite.
        """
        finished = (finished_at or datetime.now(timezone.utc)).astimezone(timezone.utc)

        with closing(connect(self.database_path)) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE attempts
                SET finished_at = ?, outcome = ?, error = ?
                WHERE id = ? AND finished_at IS NULL
                """,
                (finished.isoformat(), outcome.value, error, attempt_id),
            )
            if cursor.rowcount == 0:
                return False

            connection.execute(
                """
                UPDATE events
                SET status = ?
                WHERE id = (SELECT event_row_id FROM attempts WHERE id = ?)
                """,
                (event_status.value, attempt_id),
            )
            return True

    def get_attempt(self, attempt_id: int) -> Attempt | None:
        with closing(connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()

        if row is None:
            return None
        return Attempt.from_row(row)

    def list_attempts(self, record_id: int) -> list[Attempt]:
        """Return the attempts for one event, oldest first."""
        with closing(connect(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT * FROM attempts
                WHERE event_row_id = ?
                ORDER BY attempt_number
                """,
                (record_id,),
            ).fetchall()

        return [Attempt.from_row(row) for row in rows]
