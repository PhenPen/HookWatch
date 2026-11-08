"""Database access for webhook events.

All SQL for events lives here. Routes, and later the MCP layer, call these
methods and never see a connection, a cursor or a table name. That boundary is
what makes it possible to replace SQLite with another database without
rewriting the request handling.
"""

import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from app.db import StoredEvent, connect
from app.schemas.event import WebhookEvent


class EventRepository:
    """Stores and retrieves webhook events."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def add(
        self,
        source: str,
        event: WebhookEvent,
        received_at: datetime | None = None,
    ) -> int:
        """Insert one event and return its database id.

        Timestamps are normalised to UTC before storage. SQLite compares TEXT,
        so two ISO strings with different offsets would sort incorrectly; one
        canonical format keeps ordering reliable.
        """
        occurred_at = event.occurred_at.astimezone(timezone.utc)
        received = (received_at or datetime.now(timezone.utc)).astimezone(timezone.utc)

        with closing(connect(self.database_path)) as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO events
                    (source, event_id, event_type, occurred_at, data, received_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    event.event_id,
                    event.event_type,
                    occurred_at.isoformat(),
                    json.dumps(event.data),
                    received.isoformat(),
                ),
            )
        return int(cursor.lastrowid)

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
