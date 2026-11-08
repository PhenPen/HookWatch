"""Print stored webhook events as one JSON object per line.

This is a development tool. It reads the database directly so events can be
inspected before an HTTP inspection API exists, which arrives in a later stage.

Examples:

    uv run python -m tools.inspect_events
    uv run python -m tools.inspect_events --limit 5
    uv run python -m tools.inspect_events --db C:\\temp\\other.db

Exit codes:

    0  the database was read successfully
    1  the database file or the events table does not exist
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

from app.db import StoredEvent
from app.repositories.events import EventRepository

DEFAULT_DATABASE_PATH = "hookwatch.db"


def format_event(event: StoredEvent) -> str:
    return json.dumps(
        {
            "id": event.id,
            "source": event.source,
            "event_id": event.event_id,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at.isoformat(),
            "received_at": event.received_at.isoformat(),
            "data": event.data,
        },
        ensure_ascii=False,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="inspect_events",
        description="Print stored webhook events, newest first.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=f"database file (default: HOOKWATCH_DB_PATH or {DEFAULT_DATABASE_PATH})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="maximum number of events to print (default: 20)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    path = Path(args.db or os.environ.get("HOOKWATCH_DB_PATH", DEFAULT_DATABASE_PATH))

    if not path.exists():
        print(f"error: no database at {path}", file=sys.stderr)
        return 1

    try:
        events = EventRepository(path).list(limit=args.limit)
    except sqlite3.OperationalError as error:
        print(f"error: cannot read events from {path}: {error}", file=sys.stderr)
        return 1

    if not events:
        print("no events stored")
        return 0

    for event in events:
        print(format_event(event))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
