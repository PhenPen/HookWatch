from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import create_app

VALID_EVENT: dict[str, Any] = {
    "event_id": "evt_123",
    "event_type": "invoice.paid",
    "occurred_at": "2026-11-07T12:00:00Z",
    "data": {"amount": 500, "currency": "USD"},
}


def test_accepted_event_is_stored(client: TestClient, app: FastAPI) -> None:
    response = client.post("/webhooks/github", json=VALID_EVENT)

    assert response.status_code == 202
    events = app.state.events.list()
    assert len(events) == 1
    stored = events[0]
    assert stored.source == "github"
    assert stored.event_id == "evt_123"
    assert stored.event_type == "invoice.paid"
    assert stored.data == {"amount": 500, "currency": "USD"}


def test_stored_timestamps_are_utc(client: TestClient, app: FastAPI) -> None:
    client.post(
        "/webhooks/github",
        json={**VALID_EVENT, "occurred_at": "2026-11-07T13:00:00+01:00"},
    )

    stored = app.state.events.list()[0]

    assert stored.occurred_at == datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    assert stored.occurred_at.utcoffset() == timedelta(0)
    assert stored.received_at.utcoffset() == timedelta(0)


def test_rejected_event_is_not_stored(client: TestClient, app: FastAPI) -> None:
    response = client.post("/webhooks/github", json={"event_id": "evt_1"})

    assert response.status_code == 422
    assert app.state.events.list() == []


def test_duplicate_deliveries_store_one_row(
    client: TestClient,
    app: FastAPI,
) -> None:
    first = client.post("/webhooks/github", json=VALID_EVENT)
    second = client.post("/webhooks/github", json=VALID_EVENT)

    assert first.status_code == 202
    assert second.status_code == 200
    assert len(app.state.events.list()) == 1


def test_events_are_scoped_by_source(client: TestClient, app: FastAPI) -> None:
    client.post("/webhooks/github", json=VALID_EVENT)
    client.post("/webhooks/stripe", json={**VALID_EVENT, "event_id": "evt_456"})

    sources = {event.source for event in app.state.events.list()}

    assert sources == {"github", "stripe"}


def test_events_survive_an_application_restart(database_path: Path) -> None:
    first_app = create_app(database_path)
    with TestClient(first_app) as first_client:
        response = first_client.post("/webhooks/github", json=VALID_EVENT)
        assert response.status_code == 202

    # A fresh application object on the same file stands in for a restart.
    second_app = create_app(database_path)
    with TestClient(second_app) as second_client:
        assert second_client.get("/health").status_code == 200

    stored = second_app.state.events.list()
    assert [event.event_id for event in stored] == ["evt_123"]
    assert stored[0].data == {"amount": 500, "currency": "USD"}
