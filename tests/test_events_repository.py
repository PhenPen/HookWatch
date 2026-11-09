import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.db import Attempt, StoredEvent, migrate
from app.lifecycle import AttemptOutcome, EventStatus
from app.repositories.events import EventRepository
from app.schemas.event import WebhookEvent

OCCURRED_AT = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def make_event(
    event_id: str = "evt_1",
    event_type: str = "invoice.paid",
    occurred_at: datetime | None = None,
    data: dict | None = None,
) -> WebhookEvent:
    return WebhookEvent(
        event_id=event_id,
        event_type=event_type,
        occurred_at=occurred_at or OCCURRED_AT,
        data=data if data is not None else {"amount": 500},
    )


@pytest.fixture
def repository(database_path: Path) -> EventRepository:
    migrate(database_path)
    return EventRepository(database_path)


def test_insert_if_new_reports_the_first_insert(
    repository: EventRepository,
) -> None:
    record_id, created = repository.insert_if_new("github", make_event())

    assert created is True
    assert record_id > 0


def test_redelivery_is_reported_as_duplicate(repository: EventRepository) -> None:
    first_id, first_created = repository.insert_if_new("github", make_event())
    second_id, second_created = repository.insert_if_new("github", make_event())

    assert first_created is True
    assert second_created is False
    assert second_id == first_id
    assert len(repository.list()) == 1


def test_same_event_id_from_another_source_is_a_new_event(
    repository: EventRepository,
) -> None:
    github_id, github_created = repository.insert_if_new("github", make_event())
    stripe_id, stripe_created = repository.insert_if_new("stripe", make_event())

    assert github_created and stripe_created
    assert github_id != stripe_id
    assert len(repository.list()) == 2


def test_get_returns_the_stored_event(repository: EventRepository) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())

    stored = repository.get(record_id)

    assert isinstance(stored, StoredEvent)
    assert stored.id == record_id
    assert stored.source == "github"
    assert stored.event_id == "evt_1"
    assert stored.event_type == "invoice.paid"
    assert stored.occurred_at == OCCURRED_AT
    assert stored.data == {"amount": 500}
    assert stored.status is EventStatus.RECEIVED


def test_get_returns_none_for_an_unknown_id(repository: EventRepository) -> None:
    assert repository.get(999) is None


def test_find_returns_the_matching_event(repository: EventRepository) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())

    found = repository.find("github", "evt_1")

    assert found is not None
    assert found.id == record_id


def test_find_returns_none_for_an_unknown_pair(
    repository: EventRepository,
) -> None:
    repository.insert_if_new("github", make_event())

    assert repository.find("stripe", "evt_1") is None


def test_list_returns_newest_first(repository: EventRepository) -> None:
    for index in range(3):
        repository.insert_if_new("github", make_event(event_id=f"evt_{index}"))

    events = repository.list()

    assert [event.event_id for event in events] == ["evt_2", "evt_1", "evt_0"]


def test_list_respects_the_limit(repository: EventRepository) -> None:
    for index in range(5):
        repository.insert_if_new("github", make_event(event_id=f"evt_{index}"))

    events = repository.list(limit=2)

    assert [event.event_id for event in events] == ["evt_4", "evt_3"]


def test_occurred_at_is_normalised_to_utc(repository: EventRepository) -> None:
    event = make_event(
        occurred_at=datetime(2026, 11, 7, 13, 0, tzinfo=timezone(timedelta(hours=1)))
    )

    record_id, _ = repository.insert_if_new("github", event)
    stored = repository.get(record_id)

    assert stored is not None
    assert stored.occurred_at.utcoffset() == timedelta(0)
    assert stored.occurred_at == OCCURRED_AT


def test_received_at_defaults_to_now(repository: EventRepository) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())

    stored = repository.get(record_id)

    assert stored is not None
    assert abs((datetime.now(timezone.utc) - stored.received_at).total_seconds()) < 5


def test_received_at_can_be_supplied(repository: EventRepository) -> None:
    received_at = datetime(2026, 11, 8, 9, 30, tzinfo=timezone.utc)

    record_id, _ = repository.insert_if_new(
        "github", make_event(), received_at=received_at
    )
    stored = repository.get(record_id)

    assert stored is not None
    assert stored.received_at == received_at


def test_nested_data_survives_the_round_trip(
    repository: EventRepository,
) -> None:
    data = {"items": [{"sku": "A-1", "quantity": 2}], "note": None, "paid": True}

    record_id, _ = repository.insert_if_new("github", make_event(data=data))
    stored = repository.get(record_id)

    assert stored is not None
    assert stored.data == data


def test_events_survive_a_new_repository_instance(database_path: Path) -> None:
    migrate(database_path)
    first = EventRepository(database_path)
    record_id, _ = first.insert_if_new("github", make_event())

    second = EventRepository(database_path)

    assert second.get(record_id) is not None


def test_begin_attempt_marks_the_event_processing(
    repository: EventRepository,
) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())

    attempt_id = repository.begin_attempt(record_id)

    attempt = repository.get_attempt(attempt_id)
    stored = repository.get(record_id)
    assert attempt is not None
    assert attempt.attempt_number == 1
    assert attempt.finished_at is None
    assert attempt.outcome is None
    assert stored is not None
    assert stored.status is EventStatus.PROCESSING


def test_attempt_numbers_increase(repository: EventRepository) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())

    repository.begin_attempt(record_id)
    repository.begin_attempt(record_id)

    attempts = repository.list_attempts(record_id)
    assert [attempt.attempt_number for attempt in attempts] == [1, 2]


def test_attempts_are_rejected_for_an_unknown_event(
    repository: EventRepository,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        repository.begin_attempt(999)


def test_complete_attempt_success(repository: EventRepository) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())
    attempt_id = repository.begin_attempt(record_id)

    completed = repository.complete_attempt(
        attempt_id,
        event_status=EventStatus.PROCESSED,
        outcome=AttemptOutcome.SUCCEEDED,
    )

    attempt = repository.get_attempt(attempt_id)
    stored = repository.get(record_id)
    assert completed is True
    assert attempt is not None
    assert attempt.outcome is AttemptOutcome.SUCCEEDED
    assert attempt.finished_at is not None
    assert attempt.error is None
    assert stored is not None
    assert stored.status is EventStatus.PROCESSED


def test_complete_attempt_failure_stores_the_error(
    repository: EventRepository,
) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())
    attempt_id = repository.begin_attempt(record_id)

    repository.complete_attempt(
        attempt_id,
        event_status=EventStatus.FAILED,
        outcome=AttemptOutcome.FAILED,
        error="connection refused",
    )

    attempt = repository.get_attempt(attempt_id)
    assert isinstance(attempt, Attempt)
    assert attempt.outcome is AttemptOutcome.FAILED
    assert attempt.error == "connection refused"


def test_completing_an_attempt_twice_is_refused(
    repository: EventRepository,
) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())
    attempt_id = repository.begin_attempt(record_id)
    repository.complete_attempt(
        attempt_id,
        event_status=EventStatus.PROCESSED,
        outcome=AttemptOutcome.SUCCEEDED,
    )

    completed_again = repository.complete_attempt(
        attempt_id,
        event_status=EventStatus.PROCESSED,
        outcome=AttemptOutcome.SUCCEEDED,
    )

    assert completed_again is False


def test_get_attempt_returns_none_for_an_unknown_id(
    repository: EventRepository,
) -> None:
    assert repository.get_attempt(999) is None


def test_list_attempts_is_empty_for_a_new_event(
    repository: EventRepository,
) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())

    assert repository.list_attempts(record_id) == []
