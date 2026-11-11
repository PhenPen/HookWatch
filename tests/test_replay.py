from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import migrate
from app.lifecycle import (
    AttemptOutcome,
    EventStatus,
    ReplayNotAllowedError,
    RetryableProcessingError,
)
from app.main import create_app
from app.repositories.events import EventRepository
from app.schemas.event import WebhookEvent
from app.services.events import EventService, ProcessingSettings
from app.settings import Settings

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
AUTH = {"Authorization": "Bearer replay-secret"}


def make_event(event_id: str = "evt_1") -> WebhookEvent:
    return WebhookEvent(
        event_id=event_id,
        event_type="invoice.paid",
        occurred_at=NOW,
        data={"amount": 500},
    )


class FailingProcessor:
    """Fail a fixed number of times, then succeed."""

    def __init__(self, failures: int = 1_000) -> None:
        self.remaining = failures
        self.calls = 0

    def __call__(self, event: object) -> None:
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise RetryableProcessingError("downstream unavailable")


@pytest.fixture
def repository(database_path: Path) -> EventRepository:
    migrate(database_path)
    return EventRepository(database_path)


def test_replay_starts_a_fresh_attempt_budget(
    repository: EventRepository,
) -> None:
    settings = ProcessingSettings(
        max_attempts=2,
        backoff_base_seconds=1,
        backoff_cap_seconds=1,
    )
    service = EventService(repository, processor=FailingProcessor(), settings=settings)
    record_id = service.record_delivery("github", make_event()).record_id

    assert service.process_due(now=NOW) == 1
    assert service.process_due(now=NOW + timedelta(seconds=1)) == 1
    stored = repository.get(record_id)
    assert stored is not None
    assert stored.dead_lettered_at == NOW + timedelta(seconds=1)

    replay = service.replay_event(
        record_id,
        reason="fixed the downstream service",
        requested_by="alice",
        now=NOW + timedelta(seconds=2),
    )

    stored = repository.get(record_id)
    assert replay.replay_number == 1
    assert stored is not None
    assert stored.status is EventStatus.RECEIVED
    assert stored.dead_lettered_at is None
    assert stored.replay_count == 1

    # The new cycle gets its own two attempts, so the first failure here is
    # not treated as the third failure of the old cycle.
    assert service.process_due(now=NOW + timedelta(seconds=2)) == 1
    stored = repository.get(record_id)
    assert stored is not None
    assert stored.status is EventStatus.FAILED
    assert stored.dead_lettered_at is None

    assert service.process_due(now=NOW + timedelta(seconds=3)) == 1
    stored = repository.get(record_id)
    attempts = repository.list_attempts(record_id)
    assert stored is not None
    assert stored.status is EventStatus.FAILED
    assert stored.dead_lettered_at == NOW + timedelta(seconds=3)
    assert [attempt.replay_number for attempt in attempts] == [0, 0, 1, 1]


def test_replay_can_turn_a_dead_letter_into_a_success(
    repository: EventRepository,
) -> None:
    settings = ProcessingSettings(
        max_attempts=2,
        backoff_base_seconds=1,
        backoff_cap_seconds=1,
    )
    service = EventService(
        repository,
        processor=FailingProcessor(failures=2),
        settings=settings,
    )
    record_id = service.record_delivery("github", make_event()).record_id
    service.process_due(now=NOW)
    service.process_due(now=NOW + timedelta(seconds=1))
    stored = repository.get(record_id)
    assert stored is not None
    assert stored.dead_lettered_at is not None

    service.replay_event(
        record_id,
        reason="retry after the fix",
        now=NOW + timedelta(seconds=2),
    )
    assert service.process_due(now=NOW + timedelta(seconds=2)) == 1

    stored = repository.get(record_id)
    attempts = repository.list_attempts(record_id)
    assert stored is not None
    assert stored.status is EventStatus.PROCESSED
    assert stored.dead_lettered_at is None
    assert [attempt.replay_number for attempt in attempts] == [0, 0, 1]
    assert attempts[-1].outcome is AttemptOutcome.SUCCEEDED


def test_processed_events_can_be_replayed(repository: EventRepository) -> None:
    service = EventService(repository, processor=lambda event: None)
    record_id = service.record_delivery("github", make_event()).record_id
    service.process_due(now=NOW)

    replay = service.replay_event(
        record_id,
        reason="re-run after a configuration change",
        now=NOW + timedelta(seconds=1),
    )
    assert service.process_due(now=NOW + timedelta(seconds=1)) == 1

    stored = repository.get(record_id)
    attempts = repository.list_attempts(record_id)
    assert replay.replay_number == 1
    assert stored is not None
    assert stored.status is EventStatus.PROCESSED
    assert stored.replay_count == 1
    assert [attempt.replay_number for attempt in attempts] == [0, 1]


def test_replay_is_refused_for_a_queued_event(
    repository: EventRepository,
) -> None:
    service = EventService(repository)
    record_id = service.record_delivery("github", make_event()).record_id

    with pytest.raises(ReplayNotAllowedError, match="already queued"):
        service.replay_event(record_id, reason="too early")


def test_replay_is_refused_while_processing(
    repository: EventRepository,
) -> None:
    service = EventService(repository)
    record_id = service.record_delivery("github", make_event()).record_id
    assert repository.claim(record_id) is not None

    with pytest.raises(ReplayNotAllowedError, match="currently being processed"):
        service.replay_event(record_id, reason="too early")


def test_replay_of_an_unknown_event_is_a_lookup_error(
    repository: EventRepository,
) -> None:
    service = EventService(repository)

    with pytest.raises(LookupError, match="does not exist"):
        service.replay_event(999, reason="nothing there")


def test_replay_is_recorded_in_the_history(
    repository: EventRepository,
) -> None:
    service = EventService(repository, processor=lambda event: None)
    record_id = service.record_delivery("github", make_event()).record_id
    service.process_due(now=NOW)
    at = NOW + timedelta(seconds=5)

    service.replay_event(
        record_id,
        reason="operator asked for another run",
        requested_by="alice",
        now=at,
    )

    history = service.get_event_history(record_id)
    assert history.event.replay_count == 1
    assert len(history.attempts) == 1
    assert len(history.replays) == 1
    assert history.replays[0].replay_number == 1
    assert history.replays[0].requested_at == at
    assert history.replays[0].requested_by == "alice"
    assert history.replays[0].reason == "operator asked for another run"


def test_replay_does_not_break_idempotency(repository: EventRepository) -> None:
    service = EventService(repository, processor=lambda event: None)
    first = service.record_delivery("github", make_event())
    service.process_due(now=NOW)
    service.replay_event(
        first.record_id,
        reason="re-run the stored event",
        now=NOW + timedelta(seconds=1),
    )

    again = service.record_delivery("github", make_event())

    assert again.duplicate is True
    assert again.record_id == first.record_id
    assert len(repository.list()) == 1


def test_get_event_history_rejects_an_unknown_id(
    repository: EventRepository,
) -> None:
    service = EventService(repository)

    with pytest.raises(LookupError):
        service.get_event_history(999)


@pytest.fixture
def replay_app(database_path: Path):
    return create_app(
        Settings(
            database_path=database_path,
            engine_enabled=False,
            replay_token="replay-secret",
        )
    )


@pytest.fixture
def replay_client(replay_app):
    with TestClient(replay_app) as client:
        yield client


def post_event(client: TestClient, event_id: str = "evt_1") -> int:
    response = client.post(
        "/webhooks/github",
        json={
            "event_id": event_id,
            "event_type": "invoice.paid",
            "occurred_at": "2026-11-07T12:00:00Z",
            "data": {"amount": 500},
        },
    )
    assert response.status_code == 202
    stored = client.app.state.events.find("github", event_id)
    assert stored is not None
    return stored.id


def mark_processed(client: TestClient, record_id: int) -> None:
    service = client.app.state.event_service
    attempt_id = service.start_processing(record_id)
    service.finish_processing(attempt_id, succeeded=True)


def test_replay_requires_a_configured_token(database_path: Path) -> None:
    app = create_app(
        Settings(
            database_path=database_path,
            engine_enabled=False,
            replay_token="",
        )
    )

    with TestClient(app) as client:
        record_id = post_event(client)
        response = client.post(
            f"/events/{record_id}/replay",
            json={"reason": "try again"},
            headers=AUTH,
        )

    assert response.status_code == 403
    assert "disabled" in response.json()["detail"]


def test_replay_rejects_a_missing_token(replay_client: TestClient) -> None:
    record_id = post_event(replay_client)

    response = replay_client.post(
        f"/events/{record_id}/replay",
        json={"reason": "try again"},
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_replay_rejects_a_wrong_token(replay_client: TestClient) -> None:
    record_id = post_event(replay_client)

    response = replay_client.post(
        f"/events/{record_id}/replay",
        json={"reason": "try again"},
        headers={"Authorization": "Bearer wrong"},
    )

    assert response.status_code == 401


def test_replay_queues_a_processed_event(replay_client: TestClient) -> None:
    record_id = post_event(replay_client)
    mark_processed(replay_client, record_id)

    response = replay_client.post(
        f"/events/{record_id}/replay",
        json={"reason": "re-run after a configuration change"},
        headers={**AUTH, "X-Operator": "alice"},
    )

    assert response.status_code == 202
    assert response.json() == {
        "status": "queued",
        "id": record_id,
        "replay_number": 1,
    }

    detail = replay_client.get(f"/events/{record_id}").json()
    assert detail["status"] == "received"
    assert detail["replay_count"] == 1
    assert len(detail["replays"]) == 1
    assert detail["replays"][0]["replay_number"] == 1
    assert detail["replays"][0]["requested_by"] == "alice"
    assert detail["replays"][0]["reason"] == "re-run after a configuration change"


def test_replay_records_an_unspecified_operator(replay_client: TestClient) -> None:
    record_id = post_event(replay_client)
    mark_processed(replay_client, record_id)

    replay_client.post(
        f"/events/{record_id}/replay",
        json={"reason": "no operator header"},
        headers=AUTH,
    )

    detail = replay_client.get(f"/events/{record_id}").json()
    assert detail["replays"][0]["requested_by"] == "unspecified"


def test_replay_refuses_an_event_that_is_still_queued(
    replay_client: TestClient,
) -> None:
    record_id = post_event(replay_client)

    response = replay_client.post(
        f"/events/{record_id}/replay",
        json={"reason": "too early"},
        headers=AUTH,
    )

    assert response.status_code == 409
    assert "already queued" in response.json()["detail"]


def test_replay_refuses_an_event_that_is_processing(
    replay_client: TestClient,
) -> None:
    record_id = post_event(replay_client)
    assert replay_client.app.state.events.claim(record_id) is not None

    response = replay_client.post(
        f"/events/{record_id}/replay",
        json={"reason": "too early"},
        headers=AUTH,
    )

    assert response.status_code == 409
    assert "currently being processed" in response.json()["detail"]


def test_replay_cannot_be_issued_twice_while_queued(
    replay_client: TestClient,
) -> None:
    record_id = post_event(replay_client)
    mark_processed(replay_client, record_id)
    first = replay_client.post(
        f"/events/{record_id}/replay",
        json={"reason": "first replay"},
        headers=AUTH,
    )
    second = replay_client.post(
        f"/events/{record_id}/replay",
        json={"reason": "second replay"},
        headers=AUTH,
    )

    assert first.status_code == 202
    assert second.status_code == 409


def test_replay_rejects_a_missing_reason(replay_client: TestClient) -> None:
    record_id = post_event(replay_client)
    mark_processed(replay_client, record_id)

    response = replay_client.post(
        f"/events/{record_id}/replay",
        json={"reason": "no"},
        headers=AUTH,
    )

    assert response.status_code == 422


def test_replay_returns_404_for_an_unknown_event(replay_client: TestClient) -> None:
    response = replay_client.post(
        "/events/999/replay",
        json={"reason": "nothing there"},
        headers=AUTH,
    )

    assert response.status_code == 404


def test_replay_keeps_the_single_event_row(replay_client: TestClient) -> None:
    record_id = post_event(replay_client)
    mark_processed(replay_client, record_id)
    replay_client.post(
        f"/events/{record_id}/replay",
        json={"reason": "replay once"},
        headers=AUTH,
    )

    duplicate = replay_client.post(
        "/webhooks/github",
        json={
            "event_id": "evt_1",
            "event_type": "invoice.paid",
            "occurred_at": "2026-11-07T12:00:00Z",
            "data": {"amount": 500},
        },
    )

    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    assert len(replay_client.get("/events").json()) == 1


def test_replay_rejects_unknown_body_fields(replay_client: TestClient) -> None:
    record_id = post_event(replay_client)
    mark_processed(replay_client, record_id)

    response = replay_client.post(
        f"/events/{record_id}/replay",
        json={"reason": "valid reason", "extra": True},
        headers=AUTH,
    )

    assert response.status_code == 422
