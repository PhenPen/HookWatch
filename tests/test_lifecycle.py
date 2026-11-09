from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db import migrate
from app.lifecycle import (
    AttemptNotOpenError,
    AttemptOutcome,
    EventStatus,
    InvalidTransitionError,
)
from app.repositories.events import EventRepository
from app.schemas.event import WebhookEvent
from app.services.events import DeliveryResult, EventService


def make_event(event_id: str = "evt_1") -> WebhookEvent:
    return WebhookEvent(
        event_id=event_id,
        event_type="invoice.paid",
        occurred_at=datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc),
        data={"amount": 500},
    )


@pytest.fixture
def service(database_path: Path) -> EventService:
    migrate(database_path)
    return EventService(EventRepository(database_path))


def test_record_delivery_reports_a_new_event(service: EventService) -> None:
    result = service.record_delivery("github", make_event())

    assert isinstance(result, DeliveryResult)
    assert result.duplicate is False
    assert result.record_id > 0


def test_record_delivery_reports_a_duplicate(service: EventService) -> None:
    first = service.record_delivery("github", make_event())
    second = service.record_delivery("github", make_event())

    assert second.duplicate is True
    assert second.record_id == first.record_id


def test_start_processing_opens_an_attempt(service: EventService) -> None:
    result = service.record_delivery("github", make_event())

    attempt_id = service.start_processing(result.record_id)

    stored = service.repository.get(result.record_id)
    attempt = service.repository.get_attempt(attempt_id)
    assert stored is not None
    assert stored.status is EventStatus.PROCESSING
    assert attempt is not None
    assert attempt.attempt_number == 1


def test_start_processing_rejects_an_unknown_event(
    service: EventService,
) -> None:
    with pytest.raises(LookupError):
        service.start_processing(999)


def test_start_processing_rejects_processed_events(
    service: EventService,
) -> None:
    result = service.record_delivery("github", make_event())
    attempt_id = service.start_processing(result.record_id)
    service.finish_processing(attempt_id, succeeded=True)

    with pytest.raises(InvalidTransitionError):
        service.start_processing(result.record_id)


def test_finish_processing_success_moves_to_processed(
    service: EventService,
) -> None:
    result = service.record_delivery("github", make_event())
    attempt_id = service.start_processing(result.record_id)

    service.finish_processing(attempt_id, succeeded=True)

    stored = service.repository.get(result.record_id)
    attempt = service.repository.get_attempt(attempt_id)
    assert stored is not None
    assert stored.status is EventStatus.PROCESSED
    assert attempt is not None
    assert attempt.outcome is AttemptOutcome.SUCCEEDED
    assert attempt.finished_at is not None
    assert attempt.error is None


def test_finish_processing_failure_moves_to_failed(
    service: EventService,
) -> None:
    result = service.record_delivery("github", make_event())
    attempt_id = service.start_processing(result.record_id)

    service.finish_processing(attempt_id, succeeded=False, error="timeout")

    stored = service.repository.get(result.record_id)
    attempt = service.repository.get_attempt(attempt_id)
    assert stored is not None
    assert stored.status is EventStatus.FAILED
    assert attempt is not None
    assert attempt.outcome is AttemptOutcome.FAILED
    assert attempt.error == "timeout"


def test_finishing_twice_is_refused(service: EventService) -> None:
    result = service.record_delivery("github", make_event())
    attempt_id = service.start_processing(result.record_id)
    service.finish_processing(attempt_id, succeeded=True)

    with pytest.raises(AttemptNotOpenError):
        service.finish_processing(attempt_id, succeeded=True)


def test_finishing_an_unknown_attempt_is_refused(
    service: EventService,
) -> None:
    with pytest.raises(AttemptNotOpenError):
        service.finish_processing(999, succeeded=True)


def test_a_retry_after_failure_opens_a_second_attempt(
    service: EventService,
) -> None:
    result = service.record_delivery("github", make_event())
    first_attempt = service.start_processing(result.record_id)
    service.finish_processing(first_attempt, succeeded=False, error="timeout")

    second_attempt = service.start_processing(result.record_id)

    attempts = service.repository.list_attempts(result.record_id)
    assert [attempt.attempt_number for attempt in attempts] == [1, 2]
    assert second_attempt != first_attempt
    stored = service.repository.get(result.record_id)
    assert stored is not None
    assert stored.status is EventStatus.PROCESSING


def test_a_retry_can_succeed(service: EventService) -> None:
    result = service.record_delivery("github", make_event())
    first_attempt = service.start_processing(result.record_id)
    service.finish_processing(first_attempt, succeeded=False, error="timeout")
    second_attempt = service.start_processing(result.record_id)

    service.finish_processing(second_attempt, succeeded=True)

    stored = service.repository.get(result.record_id)
    attempts = service.repository.list_attempts(result.record_id)
    assert stored is not None
    assert stored.status is EventStatus.PROCESSED
    assert [attempt.outcome for attempt in attempts] == [
        AttemptOutcome.FAILED,
        AttemptOutcome.SUCCEEDED,
    ]
