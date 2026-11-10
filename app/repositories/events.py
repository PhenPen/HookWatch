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

from app.db import Attempt, Replay, StoredEvent, connect
from app.lifecycle import AttemptOutcome, EventStatus, FailureKind
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
        return self.list_filtered(limit=limit)

    def list_filtered(
        self,
        *,
        source: str | None = None,
        event_type: str | None = None,
        status: EventStatus | None = None,
        dead_lettered: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[StoredEvent]:
        """Return stored events matching every supplied filter, newest first.

        Only fixed SQL fragments are assembled here. Filter values are always
        bound parameters, so user input never becomes SQL.
        """
        clauses: list[str] = []
        parameters: list[object] = []

        if source is not None:
            clauses.append("source = ?")
            parameters.append(source)
        if event_type is not None:
            clauses.append("event_type = ?")
            parameters.append(event_type)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status.value)
        if dead_lettered is True:
            clauses.append("dead_lettered_at IS NOT NULL")
        elif dead_lettered is False:
            clauses.append("dead_lettered_at IS NULL")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((limit, offset))

        with closing(connect(self.database_path)) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM events
                {where}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()

        return [StoredEvent.from_row(row) for row in rows]

    def list_pending(self, limit: int = 50) -> list[StoredEvent]:
        """Return events the engine may look at, oldest first.

        Received events are always candidates. Failed events are candidates
        only while they have not been dead-lettered; the service then applies
        the attempt limit and backoff before claiming one.
        """
        with closing(connect(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE (status = ? OR status = ?)
                  AND dead_lettered_at IS NULL
                ORDER BY id
                LIMIT ?
                """,
                (EventStatus.RECEIVED.value, EventStatus.FAILED.value, limit),
            ).fetchall()

        return [StoredEvent.from_row(row) for row in rows]

    def claim(self, record_id: int, started_at: datetime | None = None) -> int | None:
        """Take ownership of an event and open a new attempt.

        Returns the attempt id, or None when another worker won the race or the
        event is not claimable. The conditional UPDATE is the important part:
        it only changes a row that is still received or failed, so the database
        decides which worker owns the event.

        The status change and the attempt insert happen in one transaction, so
        an event cannot be processing with no attempt recorded.

        The attempt carries the event's current replay_count. That ties the try
        to one retry cycle, so a replay can start a fresh budget without
        discarding the attempts from earlier cycles.

        Transition rules are checked by the service before calling this; the
        repository only writes what it is told.
        """
        started = (started_at or datetime.now(timezone.utc)).astimezone(timezone.utc)

        with closing(connect(self.database_path)) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE events
                SET status = ?
                WHERE id = ?
                  AND dead_lettered_at IS NULL
                  AND status IN (?, ?)
                """,
                (
                    EventStatus.PROCESSING.value,
                    record_id,
                    EventStatus.RECEIVED.value,
                    EventStatus.FAILED.value,
                ),
            )
            if cursor.rowcount == 0:
                return None

            replay_number = connection.execute(
                "SELECT replay_count FROM events WHERE id = ?",
                (record_id,),
            ).fetchone()["replay_count"]
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
                INSERT INTO attempts
                    (event_row_id, attempt_number, replay_number, started_at)
                VALUES (?, ?, ?, ?)
                """,
                (record_id, next_number, replay_number, started.isoformat()),
            )

            return int(cursor.lastrowid)

    def complete_attempt(
        self,
        attempt_id: int,
        event_status: EventStatus,
        outcome: AttemptOutcome,
        error: str | None = None,
        failure_kind: FailureKind | None = None,
        dead_letter: bool = False,
        finished_at: datetime | None = None,
    ) -> bool:
        """Close an open attempt and move its event to a final status.

        Returns False when the attempt is missing or already finished, which is
        what makes finishing twice a detectable mistake instead of a silent
        overwrite.

        When dead_letter is true the event also gets a dead-letter timestamp in
        the same transaction, so the failure and the decision to stop retrying
        cannot disagree.
        """
        finished = (finished_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        kind = failure_kind.value if failure_kind is not None else None

        with closing(connect(self.database_path)) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE attempts
                SET finished_at = ?, outcome = ?, error = ?, failure_kind = ?
                WHERE id = ? AND finished_at IS NULL
                """,
                (finished.isoformat(), outcome.value, error, kind, attempt_id),
            )
            if cursor.rowcount == 0:
                return False

            if dead_letter:
                connection.execute(
                    """
                    UPDATE events
                    SET status = ?, dead_lettered_at = ?
                    WHERE id = (SELECT event_row_id FROM attempts WHERE id = ?)
                    """,
                    (event_status.value, finished.isoformat(), attempt_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE events
                    SET status = ?
                    WHERE id = (SELECT event_row_id FROM attempts WHERE id = ?)
                    """,
                    (event_status.value, attempt_id),
                )
            return True

    def recover_abandoned(
        self,
        cutoff: datetime,
        recovered_at: datetime | None = None,
    ) -> int:
        """Close attempts whose worker disappeared before finishing.

        An open attempt is a lease. If the process that claimed it died, the
        attempt keeps finished_at NULL and the event stays processing forever.
        Any attempt started before the cutoff is treated as abandoned, closed
        as a retryable failure, and its event is moved to failed so the normal
        retry path can pick it up.

        Returns how many attempts were closed.
        """
        cutoff_iso = cutoff.astimezone(timezone.utc).isoformat()
        finished = (recovered_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        recovered = 0

        with closing(connect(self.database_path)) as connection, connection:
            rows = connection.execute(
                """
                SELECT id, event_row_id FROM attempts
                WHERE finished_at IS NULL AND started_at < ?
                """,
                (cutoff_iso,),
            ).fetchall()

            for row in rows:
                cursor = connection.execute(
                    """
                    UPDATE attempts
                    SET finished_at = ?, outcome = ?, error = ?, failure_kind = ?
                    WHERE id = ? AND finished_at IS NULL
                    """,
                    (
                        finished.isoformat(),
                        AttemptOutcome.FAILED.value,
                        "abandoned: worker stopped before finishing",
                        FailureKind.RETRYABLE.value,
                        row["id"],
                    ),
                )
                if cursor.rowcount == 0:
                    continue

                connection.execute(
                    """
                    UPDATE events
                    SET status = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        EventStatus.FAILED.value,
                        row["event_row_id"],
                        EventStatus.PROCESSING.value,
                    ),
                )
                recovered += 1

        return recovered

    def mark_dead_letter(
        self,
        record_id: int,
        at: datetime | None = None,
    ) -> bool:
        """Stop retrying an event without deleting it.

        Returns False when the event is already dead-lettered, so two workers
        cannot both claim to have made the decision.
        """
        timestamp = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)

        with closing(connect(self.database_path)) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE events
                SET dead_lettered_at = ?
                WHERE id = ? AND dead_lettered_at IS NULL
                """,
                (timestamp.isoformat(), record_id),
            )
            return cursor.rowcount == 1

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

    def count_attempts(self, record_id: int, replay_number: int) -> int:
        """How many attempts this event has made in one replay cycle."""
        with closing(connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM attempts
                WHERE event_row_id = ? AND replay_number = ?
                """,
                (record_id, replay_number),
            ).fetchone()

        return int(row["total"])

    def replay(
        self,
        record_id: int,
        *,
        replay_number: int,
        requested_at: datetime,
        requested_by: str,
        reason: str,
    ) -> Replay | None:
        """Queue an existing event for another processing cycle.

        Returns None when the event is missing or not in a replayable state.
        The conditional UPDATE is the whole safeguard: only a failed or
        processed event can move back to received, and only one replay request
        can win that race. A second request then sees received and is refused.

        The event row is never copied or reinserted, so replay cannot bypass
        the unique index on (source, event_id). The queued attempt count is
        reset by incrementing replay_count, while every earlier attempt stays
        in place.

        `replay_number` is supplied by the service and is the value the
        increment will produce. The caller reads the event first, so the
        repository does not have to guess between a missing row and a row that
        changed after that read.
        """
        requested = requested_at.astimezone(timezone.utc).isoformat()

        with closing(connect(self.database_path)) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE events
                SET status = ?, dead_lettered_at = NULL,
                    replay_count = replay_count + 1
                WHERE id = ?
                  AND replay_count = ?
                  AND status IN (?, ?)
                """,
                (
                    EventStatus.RECEIVED.value,
                    record_id,
                    replay_number - 1,
                    EventStatus.FAILED.value,
                    EventStatus.PROCESSED.value,
                ),
            )
            if cursor.rowcount == 0:
                return None

            cursor = connection.execute(
                """
                INSERT INTO replays
                    (event_row_id, replay_number, requested_at, requested_by, reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                (record_id, replay_number, requested, requested_by, reason),
            )

            return Replay(
                id=int(cursor.lastrowid),
                event_row_id=record_id,
                replay_number=replay_number,
                requested_at=datetime.fromisoformat(requested),
                requested_by=requested_by,
                reason=reason,
            )

    def list_replays(self, record_id: int) -> list[Replay]:
        """Return the replay audit trail for one event, oldest first."""
        with closing(connect(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT * FROM replays
                WHERE event_row_id = ?
                ORDER BY replay_number
                """,
                (record_id,),
            ).fetchall()

        return [Replay.from_row(row) for row in rows]
