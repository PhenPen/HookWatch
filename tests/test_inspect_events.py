import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db import migrate
from app.repositories.events import EventRepository
from app.schemas.event import WebhookEvent
from tools.inspect_events import main


def seed(database_path: Path, count: int) -> None:
    migrate(database_path)
    repository = EventRepository(database_path)
    for index in range(count):
        repository.insert_if_new(
            "github",
            WebhookEvent(
                event_id=f"evt_{index}",
                event_type="invoice.paid",
                occurred_at=datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc),
                data={"amount": index},
            ),
        )


def test_prints_events_newest_first(
    database_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed(database_path, 2)

    assert main(["--db", str(database_path)]) == 0

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event_id"] == "evt_1"
    assert first["source"] == "github"
    assert first["data"] == {"amount": 1}
    assert first["status"] == "received"
    assert first["occurred_at"] == "2026-11-07T12:00:00+00:00"


def test_respects_the_limit(
    database_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed(database_path, 5)

    assert main(["--db", str(database_path), "--limit", "2"]) == 0

    assert len(capsys.readouterr().out.strip().splitlines()) == 2


def test_reports_an_empty_database(
    database_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(database_path)

    assert main(["--db", str(database_path)]) == 0
    assert "no events stored" in capsys.readouterr().out


def test_reports_a_missing_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--db", str(tmp_path / "missing.db")]) == 1
    assert "no database at" in capsys.readouterr().err


def test_reports_a_file_without_the_events_table(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    empty = tmp_path / "empty.db"
    empty.touch()

    assert main(["--db", str(empty)]) == 1
    assert "cannot read events" in capsys.readouterr().err
