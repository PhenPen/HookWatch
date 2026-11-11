import sqlite3
from pathlib import Path

import pytest

from app.db import MIGRATIONS, connect, migrate, schema_version


def read_version(database_path: Path) -> int:
    with sqlite3.connect(database_path) as connection:
        return connection.execute("PRAGMA user_version").fetchone()[0]


def read_columns(database_path: Path, table: str) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def read_indexes(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    return {row[0] for row in rows}


def read_tables(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    return {row[0] for row in rows}


def build_version_one(database_path: Path) -> None:
    """Create a database exactly as Stage 4 left it, before any migration."""
    with sqlite3.connect(database_path) as connection:
        connection.execute(MIGRATIONS[0][0])
        connection.execute("PRAGMA user_version = 1")


def build_version(database_path: Path, version: int) -> None:
    """Apply the first versions migrations by hand, then pin the version."""
    with sqlite3.connect(database_path) as connection:
        for migration in MIGRATIONS[:version]:
            for statement in migration:
                connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {version}")


def insert_event(
    database_path: Path,
    source: str,
    event_id: str,
    event_type: str = "invoice.paid",
) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO events
                (source, event_id, event_type, occurred_at, data, received_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source,
                event_id,
                event_type,
                "2026-11-07T12:00:00+00:00",
                "{}",
                "2026-11-07T12:00:01+00:00",
            ),
        )


def test_fresh_database_gets_every_migration(database_path: Path) -> None:
    applied = migrate(database_path)

    assert applied == len(MIGRATIONS)
    assert read_version(database_path) == schema_version()
    assert {"events", "attempts", "replays", "principals"} <= read_tables(database_path)
    assert {"status", "dead_lettered_at", "replay_count"} <= read_columns(
        database_path, "events"
    )
    assert {"failure_kind", "replay_number"} <= read_columns(
        database_path, "attempts"
    )
    assert {"name", "role", "key_prefix", "key_hash"} <= read_columns(
        database_path, "principals"
    )
    assert "events_source_event_id" in read_indexes(database_path)
    assert "events_status_idx" in read_indexes(database_path)
    assert "principals_active_name" in read_indexes(database_path)
    assert "principals_key_hash" in read_indexes(database_path)


def test_migrating_twice_changes_nothing(database_path: Path) -> None:
    migrate(database_path)
    version_after_first = read_version(database_path)

    applied = migrate(database_path)

    assert applied == 0
    assert read_version(database_path) == version_after_first


def test_version_one_database_is_upgraded_in_place(database_path: Path) -> None:
    build_version_one(database_path)
    insert_event(database_path, "github", "evt_1")

    applied = migrate(database_path)

    assert applied == len(MIGRATIONS) - 1
    assert read_version(database_path) == schema_version()
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT source, event_id, status FROM events"
        ).fetchone()
    assert row == ("github", "evt_1", "received")


def test_version_three_database_gets_the_processing_columns(
    database_path: Path,
) -> None:
    build_version(database_path, 3)
    insert_event(database_path, "github", "evt_1")

    applied = migrate(database_path)

    assert applied == len(MIGRATIONS) - 3
    assert read_version(database_path) == schema_version()
    assert "dead_lettered_at" in read_columns(database_path, "events")
    assert "failure_kind" in read_columns(database_path, "attempts")
    assert "events_status_idx" in read_indexes(database_path)
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT source, event_id, dead_lettered_at FROM events"
        ).fetchone()
    assert row == ("github", "evt_1", None)


def test_version_four_database_gets_replay_support(database_path: Path) -> None:
    build_version(database_path, 4)
    insert_event(database_path, "github", "evt_1")

    applied = migrate(database_path)

    assert applied == 2  # versions 5 and 6
    assert read_version(database_path) == schema_version()
    assert "replays" in read_tables(database_path)
    assert "replay_count" in read_columns(database_path, "events")
    assert "replay_number" in read_columns(database_path, "attempts")
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT source, event_id, replay_count FROM events"
        ).fetchone()
    assert row == ("github", "evt_1", 0)


def test_version_five_database_gets_operator_identities(database_path: Path) -> None:
    build_version(database_path, 5)
    insert_event(database_path, "github", "evt_1")

    applied = migrate(database_path)

    assert applied == 1
    assert read_version(database_path) == schema_version()
    assert "principals" in read_tables(database_path)
    assert {"name", "role", "key_prefix", "key_hash"} <= read_columns(
        database_path, "principals"
    )
    assert "principals_active_name" in read_indexes(database_path)
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT source, event_id FROM events"
        ).fetchone()
    assert row == ("github", "evt_1")


def test_upgrade_deduplicates_existing_rows(database_path: Path) -> None:
    build_version_one(database_path)
    insert_event(database_path, "github", "evt_1")   # id 1, kept
    insert_event(database_path, "github", "evt_1")   # id 2, duplicate
    insert_event(database_path, "github", "evt_1")   # id 3, duplicate
    insert_event(database_path, "stripe", "evt_1")   # id 4, different source
    insert_event(database_path, "github", "evt_2")   # id 5, different event

    migrate(database_path)

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT id, source, event_id FROM events ORDER BY id"
        ).fetchall()
    assert rows == [
        (1, "github", "evt_1"),
        (4, "stripe", "evt_1"),
        (5, "github", "evt_2"),
    ]


def test_unique_index_rejects_duplicates(database_path: Path) -> None:
    migrate(database_path)
    insert_event(database_path, "github", "evt_1")

    with pytest.raises(sqlite3.IntegrityError):
        insert_event(database_path, "github", "evt_1")


def test_failed_migration_keeps_the_last_applied_version(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = [MIGRATIONS[0], ["CREATE TABLE broken ("]]
    monkeypatch.setattr("app.db.MIGRATIONS", broken)

    with pytest.raises(sqlite3.OperationalError):
        migrate(database_path)

    assert read_version(database_path) == 1


def test_connections_enforce_foreign_keys(database_path: Path) -> None:
    migrate(database_path)

    with sqlite3.connect(database_path) as raw:
        assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 0

    connection = connect(database_path)
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()
