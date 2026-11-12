"""Create, verify and restore consistent HookWatch database backups.

The database holds the delivery queue, processing attempts, replay history and
the hashed operator keys. Copying the file while the server is running can
produce a torn backup, because SQLite may be part-way through a write. The
create command uses SQLite's online backup API instead: it reads a consistent
snapshot while writers continue, writes to a temporary file, checks the copy,
and only then moves it into place.

Examples:

    uv run python -m tools.backup_db create --db hookwatch.db
    uv run python -m tools.backup_db create --db hookwatch.db --output backups/hw.db
    uv run python -m tools.backup_db verify --db backups/hw.db
    uv run python -m tools.backup_db restore --db hookwatch.db --backup backups/hw.db --force

Restore replaces the database file, so stop the web process and the engine
first. It refuses when a journal sidecar exists, which usually means a process
still has the database open, and it keeps the replaced file next to the target
as ``<name>.before-restore-<timestamp>``.

Exit codes:

    0  the command succeeded
    1  the database or backup is missing, invalid or in use, or an argument is
       unusable
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from app.db import schema_version

DEFAULT_DATABASE_PATH = "hookwatch.db"

# Copy in chunks rather than one long read, so a busy writer gets a chance to
# run between pages.
BACKUP_PAGES = 256

# A sidecar means a process may still be using the database. Restoring under it
# would leave a journal or WAL file describing a file that no longer exists.
SIDECARS = ("-journal", "-wal", "-shm")


class BackupError(Exception):
    """A backup, verification or restore could not be completed."""


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _open_for_reading(path: Path) -> sqlite3.Connection:
    """Open a connection that cannot write, so inspection has no side effects."""
    connection = sqlite3.connect(path, timeout=5.0)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _require_events_table(connection: sqlite3.Connection) -> None:
    """Refuse a file that is not a migrated HookWatch database."""
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
    ).fetchone()
    if row is None:
        raise BackupError(
            "not a HookWatch database: the events table is missing"
        )


def _inspect(path: Path) -> dict[str, object]:
    """Check a database and return the facts a backup summary reports."""
    with closing(_open_for_reading(path)) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        integrity = [
            str(row[0]) for row in connection.execute("PRAGMA integrity_check")
        ]
        if integrity != ["ok"]:
            raise BackupError(
                "integrity check failed: " + "; ".join(integrity)
            )

        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise BackupError(
                f"foreign key check failed: {len(violations)} violation(s)"
            )

        _require_events_table(connection)
        events = int(
            connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        )

    return {
        "schema_version": version,
        "events": events,
        "integrity": "ok",
        "foreign_keys": "ok",
    }


def verify_database(path: Path) -> dict[str, object]:
    """Check an existing database or backup without modifying it."""
    if not path.is_file():
        raise BackupError(f"no database at {path}")

    try:
        summary = _inspect(path)
    except sqlite3.Error as error:
        raise BackupError(f"cannot read {path}: {error}") from error

    return {
        "database": str(path),
        "bytes": path.stat().st_size,
        **summary,
    }


def default_backup_path(database_path: Path) -> Path:
    """Name a backup after the source, with a UTC timestamp."""
    stamp = _timestamp()
    suffix = database_path.suffix or ".db"
    candidate = database_path.with_name(
        f"{database_path.stem}-backup-{stamp}{suffix}"
    )
    counter = 1
    while candidate.exists():
        candidate = database_path.with_name(
            f"{database_path.stem}-backup-{stamp}-{counter}{suffix}"
        )
        counter += 1
    return candidate


def create_backup(
    database_path: Path,
    output_path: Path,
    *,
    force: bool = False,
) -> dict[str, object]:
    """Copy a live database into a verified backup file."""
    if not database_path.is_file():
        raise BackupError(f"no database at {database_path}")
    if database_path.resolve() == output_path.resolve():
        raise BackupError(
            "the backup output must differ from the source database"
        )
    if output_path.exists() and not force:
        raise BackupError(
            f"{output_path} already exists; pass --force to replace it"
        )
    if not output_path.parent.is_dir():
        raise BackupError(f"no directory at {output_path.parent}")

    handle, temp_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".partial",
    )
    os.close(handle)
    temp_path = Path(temp_name)

    try:
        with closing(_open_for_reading(database_path)) as source:
            _require_events_table(source)
            with closing(
                sqlite3.connect(temp_path, timeout=5.0)
            ) as target:
                source.backup(target, pages=BACKUP_PAGES)

        summary = _inspect(temp_path)
        os.replace(temp_path, output_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    return {
        "database": str(database_path),
        "backup": str(output_path),
        "bytes": output_path.stat().st_size,
        **summary,
    }


def restore_backup(
    backup_path: Path,
    database_path: Path,
    *,
    force: bool = False,
) -> dict[str, object]:
    """Replace a database with a verified backup, keeping the old file."""
    if backup_path.resolve() == database_path.resolve():
        raise BackupError("the backup and database must differ")

    verified = verify_database(backup_path)

    if not database_path.parent.is_dir():
        raise BackupError(f"no directory at {database_path.parent}")

    for suffix in SIDECARS:
        sidecar = database_path.with_name(database_path.name + suffix)
        if sidecar.exists():
            raise BackupError(
                f"{sidecar} exists; stop HookWatch before restoring"
            )

    previous: Path | None = None

    handle, temp_name = tempfile.mkstemp(
        dir=database_path.parent,
        prefix=f".{database_path.name}.",
        suffix=".restore",
    )
    os.close(handle)
    temp_path = Path(temp_name)

    try:
        if database_path.exists():
            if not force:
                raise BackupError(
                    f"{database_path} already exists; pass --force to replace it"
                )
            previous = database_path.with_name(
                f"{database_path.name}.before-restore-{_timestamp()}"
            )
            counter = 1
            while previous.exists():
                previous = database_path.with_name(
                    f"{database_path.name}.before-restore-{_timestamp()}-{counter}"
                )
                counter += 1
            os.replace(database_path, previous)

        shutil.copyfile(backup_path, temp_path)
        os.replace(temp_path, database_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        # Put the original back rather than leaving no database at all.
        if previous is not None and not database_path.exists():
            os.replace(previous, database_path)
        raise

    return {
        "database": str(database_path),
        "backup": str(backup_path),
        "bytes": database_path.stat().st_size,
        "previous": str(previous) if previous is not None else None,
        "schema_version": verified["schema_version"],
        "events": verified["events"],
        "integrity": verified["integrity"],
        "foreign_keys": verified["foreign_keys"],
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="backup_db",
        description="Create, verify and restore HookWatch database backups.",
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db",
        default=None,
        help=(
            "database file (default: HOOKWATCH_DB_PATH or "
            f"{DEFAULT_DATABASE_PATH})"
        ),
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser(
        "create",
        parents=[common],
        help="copy a live database into a verified backup",
    )
    create.add_argument(
        "--output",
        default=None,
        help="backup path (default: next to the database, with a timestamp)",
    )
    create.add_argument(
        "--force",
        action="store_true",
        help="replace an existing output file",
    )

    subparsers.add_parser(
        "verify",
        parents=[common],
        help="check an existing database or backup without changing it",
    )

    restore = subparsers.add_parser(
        "restore",
        parents=[common],
        help="replace the database with a verified backup",
    )
    restore.add_argument(
        "--backup",
        required=True,
        help="backup file to restore",
    )
    restore.add_argument(
        "--force",
        action="store_true",
        help="replace an existing database, keeping it as a before-restore file",
    )

    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    path = Path(
        args.db or os.environ.get("HOOKWATCH_DB_PATH", DEFAULT_DATABASE_PATH)
    )

    try:
        if args.command == "create":
            output = (
                Path(args.output)
                if args.output
                else default_backup_path(path)
            )
            summary = create_backup(path, output, force=args.force)
        elif args.command == "restore":
            print(
                "warning: stop the web process and engine before restoring; "
                "the database file is replaced",
                file=sys.stderr,
            )
            summary = restore_backup(
                Path(args.backup),
                path,
                force=args.force,
            )
        else:
            summary = verify_database(path)
    except BackupError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if int(summary["schema_version"]) > schema_version():
        print(
            "warning: this database uses a newer schema than this release; "
            "restore it with a matching HookWatch version",
            file=sys.stderr,
        )

    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
