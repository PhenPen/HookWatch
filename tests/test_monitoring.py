from typing import Any

from fastapi.testclient import TestClient

from app.lifecycle import AttemptOutcome


def payload(
    event_id: str,
    event_type: str = "invoice.paid",
    occurred_at: str = "2026-11-07T12:00:00Z",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "data": data if data is not None else {"amount": 500},
    }


def record(
    client: TestClient,
    event_id: str,
    *,
    source: str = "github",
    event_type: str = "invoice.paid",
) -> int:
    response = client.post(
        f"/webhooks/{source}",
        json=payload(event_id, event_type),
    )
    assert response.status_code == 202
    stored = client.app.state.events.find(source, event_id)
    assert stored is not None
    return stored.id


def test_list_returns_events_newest_first(client: TestClient) -> None:
    record(client, "evt_1")
    record(client, "evt_2")

    response = client.get("/events")

    assert response.status_code == 200
    body = response.json()
    assert [event["event_id"] for event in body] == ["evt_2", "evt_1"]
    assert body[0]["source"] == "github"
    assert body[0]["status"] == "received"
    assert body[0]["dead_lettered_at"] is None
    assert body[0]["replay_count"] == 0
    # Listings stay small; the payload belongs to the detail view.
    assert "data" not in body[0]


def test_list_is_empty_for_a_new_database(client: TestClient) -> None:
    response = client.get("/events")

    assert response.status_code == 200
    assert response.json() == []


def test_list_filters_by_source(client: TestClient) -> None:
    record(client, "evt_1", source="github")
    record(client, "evt_2", source="stripe")

    response = client.get("/events", params={"source": "stripe"})

    assert [event["event_id"] for event in response.json()] == ["evt_2"]


def test_list_filters_by_event_type(client: TestClient) -> None:
    record(client, "evt_1", event_type="invoice.paid")
    record(client, "evt_2", event_type="invoice.failed")

    response = client.get("/events", params={"event_type": "invoice.failed"})

    assert [event["event_id"] for event in response.json()] == ["evt_2"]


def test_list_filters_by_status(client: TestClient) -> None:
    completed_id = record(client, "evt_done")
    record(client, "evt_waiting")
    service = client.app.state.event_service
    attempt_id = service.start_processing(completed_id)
    service.finish_processing(attempt_id, succeeded=True)

    response = client.get("/events", params={"status": "processed"})

    assert [event["event_id"] for event in response.json()] == ["evt_done"]


def test_list_filters_by_dead_letter_state(client: TestClient) -> None:
    dead_id = record(client, "evt_dead")
    record(client, "evt_live")
    client.app.state.events.mark_dead_letter(dead_id)

    dead = client.get("/events", params={"dead_lettered": "true"})
    alive = client.get("/events", params={"dead_lettered": "false"})

    assert [event["event_id"] for event in dead.json()] == ["evt_dead"]
    assert [event["event_id"] for event in alive.json()] == ["evt_live"]


def test_list_supports_limit_and_offset(client: TestClient) -> None:
    for index in range(4):
        record(client, f"evt_{index}")

    response = client.get("/events", params={"limit": 2, "offset": 1})

    assert [event["event_id"] for event in response.json()] == ["evt_2", "evt_1"]


def test_list_rejects_an_unknown_status(client: TestClient) -> None:
    response = client.get("/events", params={"status": "lost"})

    assert response.status_code == 422


def test_detail_returns_the_payload_and_history(client: TestClient) -> None:
    record_id = record(client, "evt_1")
    service = client.app.state.event_service
    attempt_id = service.start_processing(record_id)
    service.finish_processing(
        attempt_id,
        succeeded=False,
        error="downstream timeout",
        failure_kind=None,
    )

    response = client.get(f"/events/{record_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["event_id"] == "evt_1"
    assert body["data"] == {"amount": 500}
    assert body["status"] == "failed"
    assert body["replay_count"] == 0
    assert len(body["attempts"]) == 1
    assert body["attempts"][0]["attempt_number"] == 1
    assert body["attempts"][0]["replay_number"] == 0
    assert body["attempts"][0]["outcome"] == AttemptOutcome.FAILED.value
    assert body["attempts"][0]["failure_kind"] == "retryable"
    assert body["attempts"][0]["error"] == "downstream timeout"
    assert body["replays"] == []


def test_detail_does_not_leak_a_missing_event(client: TestClient) -> None:
    response = client.get("/events/999")

    assert response.status_code == 404
    assert "does not exist" in response.json()["detail"]
