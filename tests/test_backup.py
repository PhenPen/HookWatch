import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.db import migrate, schema_version
from app.repositories.events import EventRepository
from app.schemas.event import WebhookEvent
from app.services.events import EventService
from tools import backup_db
from tools.backup_db import main


def add_event(database_path: Path, event_id: str) -> None:
    service = EventService(EventRepository(database_path))
    service.record_delivery(
        "github",
        WebhookEvent(
            event_id=event_id,
            event_type="invoice.paid",
            occurred_at="2026-11-07T12:00:00Z",
            data={"amount": 500},
        ),
    )


def seed(database_path: Path, *event_ids: str) -> None:
    migrate(database_path)
    for event_id in event_ids:
        add_event(database_path, event_id)


def count_events(database_path: Path) -> int:
    with closing(sqlite3.connect(database_path)) as connection:
        return int(
            connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        )


def test_create_writes_a_verified_backup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "hookwatch.db"
    output = tmp_path / "backup.db"
    seed(database_path, "evt_1")

    assert (
        main(
            [
                "create",
                "--db",
                str(database_path),
                "--output",
                str(output),
            ]
        )
        == 0
    )

    summary = json.loads(capsys.readouterr().out)
    assert summary["database"] == str(database_path)
    assert summary["backup"] == str(output)
    assert summary["bytes"] == output.stat().st_size
    assert summary["schema_version"] == schema_version()
    assert summary["events"] == 1
    assert summary["integrity"] == "ok"
    assert summary["foreign_keys"] == "ok"
    assert count_events(output) == 1


def test_default_output_sits_next_to_the_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "hookwatch.db"
    seed(database_path)

    assert main(["create", "--db", str(database_path)]) == 0

    summary = json.loads(capsys.readouterr().out)
    backup = Path(summary["backup"])
    assert backup.parent == database_path.parent
    assert backup.name.startswith("hookwatch-backup-")
    assert backup.is_file()


def test_backup_is_independent_of_later_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "hookwatch.db"
    output = tmp_path / "backup.db"
    seed(database_path, "evt_1")
    assert (
        main(
            [
                "create",
                "--db",
                str(database_path),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    capsys.readouterr()

    add_event(database_path, "evt_2")

    assert main(["verify", "--db", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["events"] == 1


def test_create_refuses_an_existing_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "hookwatch.db"
    output = tmp_path / "backup.db"
    seed(database_path)
    output.write_bytes(b"keep me")

    assert (
        main(
            [
                "create",
                "--db",
                str(database_path),
                "--output",
                str(output),
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert "already exists" in captured.err
    assert output.read_bytes() == b"keep me"


def test_force_replaces_an_existing_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "hookwatch.db"
    output = tmp_path / "backup.db"
    seed(database_path, "evt_1")
    output.write_bytes(b"old")

    assert (
        main(
            [
                "create",
                "--db",
                str(database_path),
                "--output",
                str(output),
                "--force",
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out)["events"] == 1
    assert count_events(output) == 1


def test_create_refuses_a_missing_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            [
                "create",
                "--db",
                str(tmp_path / "missing.db"),
                "--output",
                str(tmp_path / "backup.db"),
            ]
        )
        == 1
    )

    assert "no database" in capsys.readouterr().err


def test_create_refuses_a_database_without_events(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "other.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY)")
    output = tmp_path / "backup.db"

    assert (
        main(
            [
                "create",
                "--db",
                str(database_path),
                "--output",
                str(output),
            ]
        )
        == 1
    )

    assert "events table is missing" in capsys.readouterr().err
    assert not output.exists()


def test_create_refuses_the_source_as_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "hookwatch.db"
    seed(database_path)

    assert (
        main(
            [
                "create",
                "--db",
                str(database_path),
                "--output",
                str(database_path),
            ]
        )
        == 1
    )

    assert "must differ" in capsys.readouterr().err


def test_create_removes_the_partial_file_on_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "hookwatch.db"
    output = tmp_path / "backup.db"
    seed(database_path)

    def fail(path: Path) -> dict[str, object]:
        raise backup_db.BackupError("inspection failed")

    monkeypatch.setattr(backup_db, "_inspect", fail)

    assert (
        main(
            [
                "create",
                "--db",
                str(database_path),
                "--output",
                str(output),
            ]
        )
        == 1
    )

    assert "inspection failed" in capsys.readouterr().err
    assert not output.exists()
    assert not [path for path in tmp_path.iterdir() if ".partial" in path.name]


def test_create_refuses_a_missing_output_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "hookwatch.db"
    seed(database_path)

    assert (
        main(
            [
                "create",
                "--db",
                str(database_path),
                "--output",
                str(tmp_path / "missing" / "backup.db"),
            ]
        )
        == 1
    )

    assert "no directory" in capsys.readouterr().err


def test_verify_reports_a_good_backup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "hookwatch.db"
    output = tmp_path / "backup.db"
    seed(database_path, "evt_1", "evt_2")
    assert (
        main(
            [
                "create",
                "--db",
                str(database_path),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["verify", "--db", str(output)]) == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["database"] == str(output)
    assert summary["bytes"] == output.stat().st_size
    assert summary["schema_version"] == schema_version()
    assert summary["events"] == 2
    assert summary["integrity"] == "ok"
    assert summary["foreign_keys"] == "ok"


def test_verify_does_not_modify_the_backup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "hookwatch.db"
    output = tmp_path / "backup.db"
    seed(database_path)
    assert (
        main(
            [
                "create",
                "--db",
                str(database_path),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    capsys.readouterr()
    before = hashlib.sha256(output.read_bytes()).hexdigest()

    assert main(["verify", "--db", str(output)]) == 0

    after = hashlib.sha256(output.read_bytes()).hexdigest()
    assert before == after


def test_verify_rejects_a_corrupt_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a database")

    assert main(["verify", "--db", str(corrupt)]) == 1

    assert "cannot read" in capsys.readouterr().err


def test_verify_rejects_a_database_without_events(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "other.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY)")

    assert main(["verify", "--db", str(database_path)]) == 1

    assert "events table is missing" in capsys.readouterr().err


def test_restore_creates_a_missing_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "backup.db"
    target = tmp_path / "restored.db"
    seed(source, "evt_1")
    assert (
        main(
            [
                "create",
                "--db",
                str(source),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "restore",
                "--db",
                str(target),
                "--backup",
                str(output),
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert "stop the web process" in captured.err
    assert summary["database"] == str(target)
    assert summary["backup"] == str(output)
    assert summary["previous"] is None
    assert summary["events"] == 1
    assert count_events(target) == 1


def test_restore_refuses_an_existing_database_without_force(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    seed(source, "evt_1", "evt_2")
    seed(target, "evt_1")
    assert (
        main(
            [
                "create",
                "--db",
                str(source),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "restore",
                "--db",
                str(target),
                "--backup",
                str(output),
            ]
        )
        == 1
    )

    assert "already exists" in capsys.readouterr().err
    assert count_events(target) == 1


def test_restore_with_force_keeps_the_previous_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    seed(source, "evt_1", "evt_2")
    seed(target, "evt_1")
    assert (
        main(
            [
                "create",
                "--db",
                str(source),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "restore",
                "--db",
                str(target),
                "--backup",
                str(output),
                "--force",
            ]
        )
        == 0
    )

    summary = json.loads(capsys.readouterr().out)
    previous = Path(summary["previous"])
    assert previous.name.startswith("target.db.before-restore-")
    assert previous.is_file()
    assert count_events(previous) == 1
    assert count_events(target) == 2


def test_restore_refuses_the_target_as_backup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "hookwatch.db"
    seed(database_path, "evt_1")

    assert (
        main(
            [
                "restore",
                "--db",
                str(database_path),
                "--backup",
                str(database_path),
                "--force",
            ]
        )
        == 1
    )

    assert "must differ" in capsys.readouterr().err
    assert count_events(database_path) == 1


def test_restore_refuses_a_corrupt_backup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "target.db"
    corrupt = tmp_path / "corrupt.db"
    seed(target, "evt_1")
    corrupt.write_bytes(b"this is not a database")

    assert (
        main(
            [
                "restore",
                "--db",
                str(target),
                "--backup",
                str(corrupt),
                "--force",
            ]
        )
        == 1
    )

    assert "cannot read" in capsys.readouterr().err
    assert count_events(target) == 1


def test_restore_refuses_a_journal_sidecar(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    seed(source, "evt_1")
    seed(target, "evt_1")
    assert (
        main(
            [
                "create",
                "--db",
                str(source),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    capsys.readouterr()
    sidecar = target.with_name(target.name + "-journal")
    sidecar.write_bytes(b"")

    assert (
        main(
            [
                "restore",
                "--db",
                str(target),
                "--backup",
                str(output),
                "--force",
            ]
        )
        == 1
    )

    assert "stop HookWatch" in capsys.readouterr().err
    assert count_events(target) == 1


def test_a_newer_schema_is_reported(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "hookwatch.db"
    output = tmp_path / "backup.db"
    seed(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(f"PRAGMA user_version = {schema_version() + 1}")

    assert (
        main(
            [
                "create",
                "--db",
                str(database_path),
                "--output",
                str(output),
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert "newer schema" in captured.err
    assert json.loads(captured.out)["schema_version"] == schema_version() + 1
