from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.db import StoredEvent, initialize_database
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
    initialize_database(database_path)
    return EventRepository(database_path)


def test_initialize_is_safe_to_run_twice(database_path: Path) -> None:
    initialize_database(database_path)
    initialize_database(database_path)

    assert database_path.exists()


def test_add_returns_increasing_ids(repository: EventRepository) -> None:
    first = repository.add("github", make_event(event_id="evt_1"))
    second = repository.add("github", make_event(event_id="evt_2"))

    assert second == first + 1


def test_get_returns_the_stored_event(repository: EventRepository) -> None:
    record_id = repository.add("github", make_event())

    stored = repository.get(record_id)

    assert isinstance(stored, StoredEvent)
    assert stored.id == record_id
    assert stored.source == "github"
    assert stored.event_id == "evt_1"
    assert stored.event_type == "invoice.paid"
    assert stored.occurred_at == OCCURRED_AT
    assert stored.data == {"amount": 500}


def test_get_returns_none_for_an_unknown_id(repository: EventRepository) -> None:
    assert repository.get(999) is None


def test_list_returns_newest_first(repository: EventRepository) -> None:
    for index in range(3):
        repository.add("github", make_event(event_id=f"evt_{index}"))

    events = repository.list()

    assert [event.event_id for event in events] == ["evt_2", "evt_1", "evt_0"]


def test_list_respects_the_limit(repository: EventRepository) -> None:
    for index in range(5):
        repository.add("github", make_event(event_id=f"evt_{index}"))

    events = repository.list(limit=2)

    assert len(events) == 2
    assert [event.event_id for event in events] == ["evt_4", "evt_3"]


def test_occurred_at_is_normalised_to_utc(repository: EventRepository) -> None:
    event = make_event(
        occurred_at=datetime(2026, 11, 7, 13, 0, tzinfo=timezone(timedelta(hours=1)))
    )

    record_id = repository.add("github", event)
    stored = repository.get(record_id)

    assert stored is not None
    assert stored.occurred_at.utcoffset() == timedelta(0)
    assert stored.occurred_at == OCCURRED_AT


def test_received_at_defaults_to_now(repository: EventRepository) -> None:
    record_id = repository.add("github", make_event())

    stored = repository.get(record_id)

    assert stored is not None
    assert abs((datetime.now(timezone.utc) - stored.received_at).total_seconds()) < 5


def test_received_at_can_be_supplied(repository: EventRepository) -> None:
    received_at = datetime(2026, 11, 8, 9, 30, tzinfo=timezone.utc)

    record_id = repository.add("github", make_event(), received_at=received_at)
    stored = repository.get(record_id)

    assert stored is not None
    assert stored.received_at == received_at


def test_nested_data_survives_the_round_trip(repository: EventRepository) -> None:
    data = {"items": [{"sku": "A-1", "quantity": 2}], "note": None, "paid": True}

    record_id = repository.add("github", make_event(data=data))
    stored = repository.get(record_id)

    assert stored is not None
    assert stored.data == data


def test_events_survive_a_new_repository_instance(database_path: Path) -> None:
    initialize_database(database_path)
    first = EventRepository(database_path)
    record_id = first.add("github", make_event())

    second = EventRepository(database_path)
    stored = second.get(record_id)

    assert stored is not None
    assert stored.event_id == "evt_1"


def test_same_event_id_can_be_stored_twice(repository: EventRepository) -> None:
    # Duplicate detection is Stage 5. Until then both deliveries are kept.
    repository.add("github", make_event(event_id="evt_same"))
    repository.add("github", make_event(event_id="evt_same"))

    assert len(repository.list()) == 2
