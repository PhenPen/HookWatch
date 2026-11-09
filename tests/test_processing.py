from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.db import migrate
from app.lifecycle import (
    AttemptOutcome,
    EventStatus,
    FailureKind,
    PermanentProcessingError,
    RetryableProcessingError,
)
from app.repositories.events import EventRepository
from app.schemas.event import WebhookEvent
from app.services.events import EventService, ProcessingSettings

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def make_event(event_id: str = "evt_1") -> WebhookEvent:
    return WebhookEvent(
        event_id=event_id,
        event_type="invoice.paid",
        occurred_at=NOW,
        data={"amount": 500},
    )


class RecordingProcessor:
    """Processor that records calls and raises queued errors in order."""

    def __init__(self, errors: list[Exception] | None = None) -> None:
        self.errors = list(errors or [])
        self.events: list = []

    def __call__(self, event: object) -> None:
        self.events.append(event)
        if self.errors:
            raise self.errors.pop(0)


@pytest.fixture
def repository(database_path: Path) -> EventRepository:
    migrate(database_path)
    return EventRepository(database_path)


def test_process_due_processes_a_received_event(
    repository: EventRepository,
) -> None:
    processor = RecordingProcessor()
    service = EventService(repository, processor=processor)
    result = service.record_delivery("github", make_event())

    handled = service.process_due(now=NOW)

    stored = repository.get(result.record_id)
    attempts = repository.list_attempts(result.record_id)
    assert handled == 1
    assert stored is not None
    assert stored.status is EventStatus.PROCESSED
    assert stored.dead_lettered_at is None
    assert [attempt.outcome for attempt in attempts] == [AttemptOutcome.SUCCEEDED]
    assert len(processor.events) == 1


def test_processor_receives_the_stored_event(
    repository: EventRepository,
) -> None:
    processor = RecordingProcessor()
    service = EventService(repository, processor=processor)
    result = service.record_delivery("github", make_event())

    service.process_due(now=NOW)

    event = processor.events[0]
    assert event.id == result.record_id
    assert event.source == "github"
    assert event.event_id == "evt_1"
    assert event.data == {"amount": 500}


def test_retryable_failure_waits_for_backoff_then_retries(
    repository: EventRepository,
) -> None:
    settings = ProcessingSettings(
        max_attempts=5,
        backoff_base_seconds=10,
        backoff_cap_seconds=60,
    )
    processor = RecordingProcessor([RetryableProcessingError("timeout")])
    service = EventService(repository, processor=processor, settings=settings)
    result = service.record_delivery("github", make_event())

    assert service.process_due(now=NOW) == 1

    stored = repository.get(result.record_id)
    attempts = repository.list_attempts(result.record_id)
    assert stored is not None
    assert stored.status is EventStatus.FAILED
    assert stored.dead_lettered_at is None
    assert attempts[0].outcome is AttemptOutcome.FAILED
    assert attempts[0].failure_kind is FailureKind.RETRYABLE
    assert attempts[0].error == "timeout"
    assert len(processor.events) == 1

    # Five seconds is less than the ten second first-failure delay.
    assert service.process_due(now=NOW + timedelta(seconds=5)) == 0
    assert len(processor.events) == 1

    assert service.process_due(now=NOW + timedelta(seconds=10)) == 1

    stored = repository.get(result.record_id)
    attempts = repository.list_attempts(result.record_id)
    assert stored is not None
    assert stored.status is EventStatus.PROCESSED
    assert stored.dead_lettered_at is None
    assert [attempt.outcome for attempt in attempts] == [
        AttemptOutcome.FAILED,
        AttemptOutcome.SUCCEEDED,
    ]
    assert len(processor.events) == 2


def test_permanent_failure_is_dead_lettered_immediately(
    repository: EventRepository,
) -> None:
    processor = RecordingProcessor([PermanentProcessingError("bad payload")])
    service = EventService(repository, processor=processor)
    result = service.record_delivery("github", make_event())

    assert service.process_due(now=NOW) == 1

    stored = repository.get(result.record_id)
    attempts = repository.list_attempts(result.record_id)
    assert stored is not None
    assert stored.status is EventStatus.FAILED
    assert stored.dead_lettered_at == NOW
    assert attempts[0].failure_kind is FailureKind.PERMANENT
    assert attempts[0].error == "bad payload"

    # Dead-lettered events leave the pending set.
    assert service.process_due(now=NOW + timedelta(minutes=5)) == 0
    assert len(processor.events) == 1


def test_exhausted_retries_are_dead_lettered(
    repository: EventRepository,
) -> None:
    settings = ProcessingSettings(
        max_attempts=2,
        backoff_base_seconds=1,
        backoff_cap_seconds=1,
    )
    processor = RecordingProcessor(
        [
            RetryableProcessingError("timeout"),
            RetryableProcessingError("timeout"),
        ]
    )
    service = EventService(repository, processor=processor, settings=settings)
    result = service.record_delivery("github", make_event())

    assert service.process_due(now=NOW) == 1
    stored = repository.get(result.record_id)
    assert stored is not None
    assert stored.status is EventStatus.FAILED
    assert stored.dead_lettered_at is None

    second_time = NOW + timedelta(seconds=1)
    assert service.process_due(now=second_time) == 1

    stored = repository.get(result.record_id)
    attempts = repository.list_attempts(result.record_id)
    assert stored is not None
    assert stored.status is EventStatus.FAILED
    assert stored.dead_lettered_at == second_time
    assert len(attempts) == 2
    assert [attempt.failure_kind for attempt in attempts] == [
        FailureKind.RETRYABLE,
        FailureKind.RETRYABLE,
    ]

    assert service.process_due(now=second_time + timedelta(minutes=1)) == 0
    assert len(processor.events) == 2


def test_unknown_exception_is_treated_as_retryable(
    repository: EventRepository,
) -> None:
    processor = RecordingProcessor([RuntimeError("boom")])
    service = EventService(repository, processor=processor)
    result = service.record_delivery("github", make_event())

    assert service.process_due(now=NOW) == 1

    stored = repository.get(result.record_id)
    attempts = repository.list_attempts(result.record_id)
    assert stored is not None
    assert stored.status is EventStatus.FAILED
    assert stored.dead_lettered_at is None
    assert attempts[0].failure_kind is FailureKind.RETRYABLE
    assert attempts[0].error is not None
    assert "RuntimeError" in attempts[0].error


def test_crashed_attempt_is_recovered_and_retried(
    repository: EventRepository,
) -> None:
    settings = ProcessingSettings(
        max_attempts=5,
        backoff_base_seconds=10,
        backoff_cap_seconds=60,
        stale_after_seconds=300,
    )
    processor = RecordingProcessor()
    service = EventService(repository, processor=processor, settings=settings)
    result = service.record_delivery("github", make_event())

    started_at = NOW - timedelta(seconds=600)
    attempt_id = repository.claim(result.record_id, started_at=started_at)
    assert attempt_id is not None

    # Recovery closes the stale attempt, but the backoff has only just begun.
    assert service.process_due(now=NOW) == 0

    attempts = repository.list_attempts(result.record_id)
    stored = repository.get(result.record_id)
    assert len(attempts) == 1
    assert attempts[0].finished_at == NOW
    assert attempts[0].failure_kind is FailureKind.RETRYABLE
    assert attempts[0].error is not None
    assert "abandoned" in attempts[0].error
    assert stored is not None
    assert stored.status is EventStatus.FAILED
    assert stored.dead_lettered_at is None

    assert service.process_due(now=NOW + timedelta(seconds=10)) == 1

    attempts = repository.list_attempts(result.record_id)
    stored = repository.get(result.record_id)
    assert stored is not None
    assert stored.status is EventStatus.PROCESSED
    assert [attempt.outcome for attempt in attempts] == [
        AttemptOutcome.FAILED,
        AttemptOutcome.SUCCEEDED,
    ]


def test_recover_abandoned_ignores_a_young_attempt(
    repository: EventRepository,
) -> None:
    settings = ProcessingSettings(stale_after_seconds=300)
    service = EventService(repository, processor=RecordingProcessor(), settings=settings)
    result = service.record_delivery("github", make_event())
    attempt_id = repository.claim(result.record_id, started_at=NOW)
    assert attempt_id is not None

    assert service.recover_abandoned(now=NOW) == 0

    attempt = repository.get_attempt(attempt_id)
    assert attempt is not None
    assert attempt.finished_at is None


def test_dead_lettered_event_is_kept(
    repository: EventRepository,
) -> None:
    processor = RecordingProcessor([PermanentProcessingError("bad payload")])
    service = EventService(repository, processor=processor)
    result = service.record_delivery("github", make_event())

    service.process_due(now=NOW)

    stored = repository.get(result.record_id)
    assert stored is not None
    assert stored.data == {"amount": 500}
    assert stored.dead_lettered_at is not None


def test_process_due_returns_zero_when_idle(
    repository: EventRepository,
) -> None:
    service = EventService(repository, processor=RecordingProcessor())

    assert service.process_due(now=NOW) == 0


def test_process_due_respects_the_batch_size(
    repository: EventRepository,
) -> None:
    settings = ProcessingSettings(batch_size=2)
    service = EventService(repository, processor=RecordingProcessor(), settings=settings)
    for index in range(3):
        service.record_delivery("github", make_event(event_id=f"evt_{index}"))

    assert service.process_due(now=NOW) == 2
    assert service.process_due(now=NOW) == 1
    assert service.process_due(now=NOW) == 0
