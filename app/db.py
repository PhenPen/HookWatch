"""SQLite connection helpers, schema and the stored event row model."""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    data        TEXT NOT NULL,
    received_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class StoredEvent:
    """One event as it exists in the database.

    This is a read model, not the request contract: it carries fields that the
    sender never provided, such as the database id and the moment we received
    the event.
    """

    id: int
    source: str
    event_id: str
    event_type: str
    occurred_at: datetime
    data: dict[str, Any]
    received_at: datetime

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
        )


def connect(database_path: Path) -> sqlite3.Connection:
    """Open a connection with row access by column name.

    Connections are short-lived and opened by whichever method needs one. That
    keeps this module free of shared mutable state, so no locking or thread
    affinity is needed.
    """
    connection = sqlite3.connect(database_path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database(database_path: Path) -> None:
    """Create the schema if it does not exist yet.

    CREATE TABLE IF NOT EXISTS is the simplest safe initialization for a single
    table. It cannot alter an existing table, so the first schema change will
    need a real migration decision instead of another call to this function.
    """
    with closing(connect(database_path)) as connection, connection:
        connection.executescript(SCHEMA)
