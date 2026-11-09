"""API level idempotency tests.

The repository tests prove the database rule. These tests prove the whole
request path honours it: a second delivery is answered with 200 instead of 202,
no second row appears, and the behaviour survives a restart.
"""

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lifecycle import EventStatus
from app.main import create_app

VALID_EVENT: dict[str, Any] = {
    "event_id": "evt_123",
    "event_type": "invoice.paid",
    "occurred_at": "2026-11-07T12:00:00Z",
    "data": {"amount": 500, "currency": "USD"},
}


def test_new_event_is_accepted_and_stored_as_received(
    client: TestClient,
    app: FastAPI,
) -> None:
    response = client.post("/webhooks/github", json=VALID_EVENT)

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "source": "github",
        "event_id": "evt_123",
    }
    events = app.state.events.list()
    assert len(events) == 1
    assert events[0].status is EventStatus.RECEIVED


def test_redelivery_is_reported_as_duplicate(
    client: TestClient,
    app: FastAPI,
) -> None:
    first = client.post("/webhooks/github", json=VALID_EVENT)
    second = client.post("/webhooks/github", json=VALID_EVENT)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json() == {
        "status": "duplicate",
        "source": "github",
        "event_id": "evt_123",
    }
    assert len(app.state.events.list()) == 1


def test_many_redeliveries_still_store_one_row(
    client: TestClient,
    app: FastAPI,
) -> None:
    first = client.post("/webhooks/github", json=VALID_EVENT)

    responses = [client.post("/webhooks/github", json=VALID_EVENT) for _ in range(5)]

    assert first.status_code == 202
    assert {response.status_code for response in responses} == {200}
    assert len(app.state.events.list()) == 1


def test_same_event_id_from_another_source_is_a_new_event(
    client: TestClient,
    app: FastAPI,
) -> None:
    github = client.post("/webhooks/github", json=VALID_EVENT)
    stripe = client.post("/webhooks/stripe", json=VALID_EVENT)

    assert github.status_code == 202
    assert stripe.status_code == 202
    assert len(app.state.events.list()) == 2


def test_duplicate_detection_survives_a_restart(database_path: Path) -> None:
    first_app = create_app(database_path, start_engine=False)
    with TestClient(first_app) as first_client:
        assert first_client.post("/webhooks/github", json=VALID_EVENT).status_code == 202

    # A fresh application object on the same file stands in for a restart.
    second_app = create_app(database_path, start_engine=False)
    with TestClient(second_app) as second_client:
        response = second_client.post("/webhooks/github", json=VALID_EVENT)

    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"
    assert len(second_app.state.events.list()) == 1


def test_a_changed_payload_with_the_same_event_id_is_still_a_duplicate(
    client: TestClient,
    app: FastAPI,
) -> None:
    client.post("/webhooks/github", json=VALID_EVENT)

    response = client.post(
        "/webhooks/github",
        json={**VALID_EVENT, "data": {"amount": 999}},
    )

    assert response.status_code == 200
    stored = app.state.events.list()[0]
    assert stored.data == {"amount": 500, "currency": "USD"}


def test_invalid_payload_is_rejected_and_nothing_is_stored(
    client: TestClient,
    app: FastAPI,
) -> None:
    response = client.post("/webhooks/github", json={"event_id": "evt_123"})

    assert response.status_code == 422
    assert app.state.events.list() == []
