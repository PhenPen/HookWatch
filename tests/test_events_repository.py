from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.db import Attempt, StoredEvent, migrate
from app.lifecycle import AttemptOutcome, EventStatus, FailureKind
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


def fail_and_dead_letter(repository: EventRepository, record_id: int) -> None:
    """Leave an event failed with the dead-letter marker set."""
    attempt_id = repository.claim(record_id)
    assert attempt_id is not None
    repository.complete_attempt(
        attempt_id,
        event_status=EventStatus.FAILED,
        outcome=AttemptOutcome.FAILED,
        failure_kind=FailureKind.PERMANENT,
        dead_letter=True,
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
    assert stored.dead_lettered_at is None


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


def test_list_pending_returns_received_and_retryable_failed(
    repository: EventRepository,
) -> None:
    received_id, _ = repository.insert_if_new("github", make_event("evt_received"))
    failed_id, _ = repository.insert_if_new("github", make_event("evt_failed"))
    dead_id, _ = repository.insert_if_new("github", make_event("evt_dead"))

    attempt_id = repository.claim(failed_id)
    assert attempt_id is not None
    repository.complete_attempt(
        attempt_id,
        event_status=EventStatus.FAILED,
        outcome=AttemptOutcome.FAILED,
        error="timeout",
        failure_kind=FailureKind.RETRYABLE,
    )
    repository.mark_dead_letter(dead_id)

    pending = repository.list_pending()

    assert [event.id for event in pending] == [received_id, failed_id]


def test_claim_marks_the_event_processing(repository: EventRepository) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())

    attempt_id = repository.claim(record_id)

    attempt = repository.get_attempt(attempt_id)
    stored = repository.get(record_id)
    assert attempt is not None
    assert attempt.attempt_number == 1
    assert attempt.finished_at is None
    assert attempt.outcome is None
    assert stored is not None
    assert stored.status is EventStatus.PROCESSING


def test_claim_is_refused_when_the_event_is_already_processing(
    repository: EventRepository,
) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())
    first_attempt = repository.claim(record_id)

    second_attempt = repository.claim(record_id)

    assert first_attempt is not None
    assert second_attempt is None


def test_claim_is_refused_for_an_unknown_event(
    repository: EventRepository,
) -> None:
    assert repository.claim(999) is None


def test_claim_is_refused_for_a_dead_lettered_event(
    repository: EventRepository,
) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())
    repository.mark_dead_letter(record_id)

    assert repository.claim(record_id) is None


def test_attempt_numbers_increase(repository: EventRepository) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())

    first_attempt = repository.claim(record_id)
    assert first_attempt is not None
    repository.complete_attempt(
        first_attempt,
        event_status=EventStatus.FAILED,
        outcome=AttemptOutcome.FAILED,
        failure_kind=FailureKind.RETRYABLE,
    )
    second_attempt = repository.claim(record_id)

    attempts = repository.list_attempts(record_id)
    assert second_attempt is not None
    assert [attempt.attempt_number for attempt in attempts] == [1, 2]


def test_complete_attempt_success(repository: EventRepository) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())
    attempt_id = repository.claim(record_id)
    assert attempt_id is not None

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
    assert attempt.failure_kind is None
    assert attempt.error is None
    assert stored is not None
    assert stored.status is EventStatus.PROCESSED


def test_complete_attempt_failure_stores_the_error(
    repository: EventRepository,
) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())
    attempt_id = repository.claim(record_id)
    assert attempt_id is not None

    repository.complete_attempt(
        attempt_id,
        event_status=EventStatus.FAILED,
        outcome=AttemptOutcome.FAILED,
        error="connection refused",
        failure_kind=FailureKind.RETRYABLE,
    )

    attempt = repository.get_attempt(attempt_id)
    assert isinstance(attempt, Attempt)
    assert attempt.outcome is AttemptOutcome.FAILED
    assert attempt.failure_kind is FailureKind.RETRYABLE
    assert attempt.error == "connection refused"


def test_complete_attempt_can_dead_letter(
    repository: EventRepository,
) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())
    attempt_id = repository.claim(record_id)
    assert attempt_id is not None
    finished_at = datetime(2026, 11, 7, 12, 5, tzinfo=timezone.utc)

    repository.complete_attempt(
        attempt_id,
        event_status=EventStatus.FAILED,
        outcome=AttemptOutcome.FAILED,
        error="bad payload",
        failure_kind=FailureKind.PERMANENT,
        dead_letter=True,
        finished_at=finished_at,
    )

    stored = repository.get(record_id)
    assert stored is not None
    assert stored.status is EventStatus.FAILED
    assert stored.dead_lettered_at == finished_at


def test_completing_an_attempt_twice_is_refused(
    repository: EventRepository,
) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())
    attempt_id = repository.claim(record_id)
    assert attempt_id is not None
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


def test_mark_dead_letter_is_idempotent(repository: EventRepository) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())
    at = datetime(2026, 11, 7, 12, 5, tzinfo=timezone.utc)

    first = repository.mark_dead_letter(record_id, at=at)
    second = repository.mark_dead_letter(record_id, at=at)

    assert first is True
    assert second is False


def test_recover_abandoned_closes_an_open_attempt(
    repository: EventRepository,
) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())
    started_at = datetime(2026, 11, 7, 11, 0, tzinfo=timezone.utc)
    attempt_id = repository.claim(record_id, started_at=started_at)
    assert attempt_id is not None
    recovered_at = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)

    recovered = repository.recover_abandoned(
        cutoff=recovered_at - timedelta(minutes=5),
        recovered_at=recovered_at,
    )

    attempt = repository.get_attempt(attempt_id)
    stored = repository.get(record_id)
    assert recovered == 1
    assert attempt is not None
    assert attempt.finished_at == recovered_at
    assert attempt.outcome is AttemptOutcome.FAILED
    assert attempt.failure_kind is FailureKind.RETRYABLE
    assert attempt.error is not None
    assert "abandoned" in attempt.error
    assert stored is not None
    assert stored.status is EventStatus.FAILED


def test_recover_abandoned_ignores_young_attempts(
    repository: EventRepository,
) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())
    now = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    attempt_id = repository.claim(record_id, started_at=now)
    assert attempt_id is not None

    recovered = repository.recover_abandoned(
        cutoff=now - timedelta(minutes=5),
        recovered_at=now,
    )

    attempt = repository.get_attempt(attempt_id)
    stored = repository.get(record_id)
    assert recovered == 0
    assert attempt is not None
    assert attempt.finished_at is None
    assert stored is not None
    assert stored.status is EventStatus.PROCESSING


def test_get_attempt_returns_none_for_an_unknown_id(
    repository: EventRepository,
) -> None:
    assert repository.get_attempt(999) is None


def test_list_attempts_is_empty_for_a_new_event(
    repository: EventRepository,
) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())

    assert repository.list_attempts(record_id) == []


def test_list_filtered_by_source(repository: EventRepository) -> None:
    repository.insert_if_new("github", make_event("evt_github"))
    repository.insert_if_new("stripe", make_event("evt_stripe"))

    events = repository.list_filtered(source="stripe")

    assert [event.event_id for event in events] == ["evt_stripe"]


def test_list_filtered_by_event_type(repository: EventRepository) -> None:
    repository.insert_if_new("github", make_event("evt_paid", "invoice.paid"))
    repository.insert_if_new("github", make_event("evt_failed", "invoice.failed"))

    events = repository.list_filtered(event_type="invoice.failed")

    assert [event.event_id for event in events] == ["evt_failed"]


def test_list_filtered_by_status(repository: EventRepository) -> None:
    received_id, _ = repository.insert_if_new("github", make_event("evt_received"))
    processed_id, _ = repository.insert_if_new("github", make_event("evt_processed"))
    attempt_id = repository.claim(processed_id)
    assert attempt_id is not None
    repository.complete_attempt(
        attempt_id,
        event_status=EventStatus.PROCESSED,
        outcome=AttemptOutcome.SUCCEEDED,
    )

    events = repository.list_filtered(status=EventStatus.PROCESSED)

    assert [event.id for event in events] == [processed_id]
    assert received_id not in [event.id for event in events]


def test_list_filtered_by_dead_letter_state(repository: EventRepository) -> None:
    live_id, _ = repository.insert_if_new("github", make_event("evt_live"))
    dead_id, _ = repository.insert_if_new("github", make_event("evt_dead"))
    repository.mark_dead_letter(dead_id)

    dead = repository.list_filtered(dead_lettered=True)
    alive = repository.list_filtered(dead_lettered=False)

    assert [event.id for event in dead] == [dead_id]
    assert [event.id for event in alive] == [live_id]


def test_list_filtered_combines_filters(repository: EventRepository) -> None:
    repository.insert_if_new("github", make_event("evt_1", "invoice.paid"))
    repository.insert_if_new("stripe", make_event("evt_2", "invoice.paid"))
    repository.insert_if_new("github", make_event("evt_3", "invoice.failed"))

    events = repository.list_filtered(source="github", event_type="invoice.paid")

    assert [event.event_id for event in events] == ["evt_1"]


def test_list_filtered_supports_limit_and_offset(
    repository: EventRepository,
) -> None:
    for index in range(4):
        repository.insert_if_new("github", make_event(f"evt_{index}"))

    events = repository.list_filtered(limit=2, offset=1)

    assert [event.event_id for event in events] == ["evt_2", "evt_1"]


def test_claim_carries_the_current_replay_number(
    repository: EventRepository,
) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())
    first_attempt = repository.claim(record_id)
    assert first_attempt is not None
    repository.complete_attempt(
        first_attempt,
        event_status=EventStatus.FAILED,
        outcome=AttemptOutcome.FAILED,
        failure_kind=FailureKind.RETRYABLE,
    )
    repository.replay(
        record_id,
        replay_number=1,
        requested_at=datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc),
        requested_by="alice",
        reason="fixed the downstream service",
    )

    second_attempt = repository.claim(record_id)

    assert second_attempt is not None
    attempt = repository.get_attempt(second_attempt)
    assert attempt is not None
    assert attempt.attempt_number == 2
    assert attempt.replay_number == 1
    assert repository.count_attempts(record_id, 0) == 1
    assert repository.count_attempts(record_id, 1) == 1


def test_replay_queues_an_event_and_records_the_request(
    repository: EventRepository,
) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())
    attempt_id = repository.claim(record_id)
    assert attempt_id is not None
    repository.complete_attempt(
        attempt_id,
        event_status=EventStatus.PROCESSED,
        outcome=AttemptOutcome.SUCCEEDED,
    )
    requested_at = datetime(2026, 11, 7, 12, 30, tzinfo=timezone.utc)

    replay = repository.replay(
        record_id,
        replay_number=1,
        requested_at=requested_at,
        requested_by="alice",
        reason="re-run after configuration change",
    )

    stored = repository.get(record_id)
    replays = repository.list_replays(record_id)
    assert replay is not None
    assert replay.replay_number == 1
    assert stored is not None
    assert stored.status is EventStatus.RECEIVED
    assert stored.dead_lettered_at is None
    assert stored.replay_count == 1
    assert len(replays) == 1
    assert replays[0].requested_at == requested_at
    assert replays[0].requested_by == "alice"
    assert replays[0].reason == "re-run after configuration change"


def test_replay_clears_the_dead_letter_marker(
    repository: EventRepository,
) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())
    fail_and_dead_letter(repository, record_id)

    replay = repository.replay(
        record_id,
        replay_number=1,
        requested_at=datetime(2026, 11, 7, 12, 30, tzinfo=timezone.utc),
        requested_by="alice",
        reason="fixed the payload",
    )

    stored = repository.get(record_id)
    assert replay is not None
    assert stored is not None
    assert stored.dead_lettered_at is None
    assert stored.status is EventStatus.RECEIVED


def test_replay_is_refused_for_a_received_event(
    repository: EventRepository,
) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())

    replay = repository.replay(
        record_id,
        replay_number=1,
        requested_at=datetime(2026, 11, 7, 12, 30, tzinfo=timezone.utc),
        requested_by="alice",
        reason="too early",
    )

    stored = repository.get(record_id)
    assert replay is None
    assert stored is not None
    assert stored.replay_count == 0
    assert repository.list_replays(record_id) == []


def test_replay_is_refused_while_processing(
    repository: EventRepository,
) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())
    assert repository.claim(record_id) is not None

    replay = repository.replay(
        record_id,
        replay_number=1,
        requested_at=datetime(2026, 11, 7, 12, 30, tzinfo=timezone.utc),
        requested_by="alice",
        reason="too early",
    )

    assert replay is None


def test_replay_is_refused_when_the_number_is_stale(
    repository: EventRepository,
) -> None:
    record_id, _ = repository.insert_if_new("github", make_event())
    fail_and_dead_letter(repository, record_id)
    first = repository.replay(
        record_id,
        replay_number=1,
        requested_at=datetime(2026, 11, 7, 12, 30, tzinfo=timezone.utc),
        requested_by="alice",
        reason="first",
    )
    assert first is not None

    second = repository.replay(
        record_id,
        replay_number=1,
        requested_at=datetime(2026, 11, 7, 12, 31, tzinfo=timezone.utc),
        requested_by="bob",
        reason="second",
    )

    assert second is None
    assert len(repository.list_replays(record_id)) == 1
