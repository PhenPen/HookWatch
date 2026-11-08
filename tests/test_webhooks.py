import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

VALID_EVENT: dict[str, Any] = {
    "event_id": "evt_123",
    "event_type": "invoice.paid",
    "occurred_at": "2026-11-07T12:00:00Z",
    "data": {"amount": 500, "currency": "USD"},
}


def test_accepts_a_valid_event(client: TestClient) -> None:
    response = client.post("/webhooks/github", json=VALID_EVENT)

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "source": "github",
        "event_id": "evt_123",
    }


def test_reports_the_source_from_the_path(client: TestClient) -> None:
    response = client.post("/webhooks/shopify-prod", json=VALID_EVENT)

    assert response.status_code == 202
    assert response.json()["source"] == "shopify-prod"


def test_logs_an_accepted_event(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="app.api.webhooks"):
        client.post("/webhooks/github", json=VALID_EVENT)

    messages = [record.getMessage() for record in caplog.records]
    assert any("event_id=evt_123" in message for message in messages)
    assert any("source=github" in message for message in messages)
    assert any("event_type=invoice.paid" in message for message in messages)


def test_logs_a_rejected_request(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.api.webhooks"):
        client.post("/webhooks/github", json={"event_id": "evt_1"})

    messages = [record.getMessage() for record in caplog.records]
    assert any("/webhooks/github" in message for message in messages)
    assert any("event_type" in message for message in messages)


def test_rejects_malformed_json(client: TestClient) -> None:
    response = client.post(
        "/webhooks/github",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422


def test_rejects_a_non_json_body(client: TestClient) -> None:
    response = client.post(
        "/webhooks/github",
        content="not json",
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("field", ["event_id", "event_type", "occurred_at", "data"])
def test_rejects_a_missing_field(client: TestClient, field: str) -> None:
    payload = {key: value for key, value in VALID_EVENT.items() if key != field}

    response = client.post("/webhooks/github", json=payload)

    assert response.status_code == 422
    assert field in response.text


def test_rejects_an_unknown_field(client: TestClient) -> None:
    response = client.post(
        "/webhooks/github",
        json={**VALID_EVENT, "extra_field": "unexpected"},
    )

    assert response.status_code == 422
    assert "extra_field" in response.text


def test_rejects_a_naive_timestamp(client: TestClient) -> None:
    response = client.post(
        "/webhooks/github",
        json={**VALID_EVENT, "occurred_at": "2026-11-07T12:00:00"},
    )

    assert response.status_code == 422
    assert "occurred_at" in response.text


def test_rejects_data_that_is_not_an_object(client: TestClient) -> None:
    response = client.post("/webhooks/github", json={**VALID_EVENT, "data": [1, 2]})

    assert response.status_code == 422
    assert "data" in response.text


@pytest.mark.parametrize("source", ["%20", "a" * 65, "bad$name", "with%20space"])
def test_rejects_an_invalid_source(client: TestClient, source: str) -> None:
    response = client.post(f"/webhooks/{source}", json=VALID_EVENT)

    assert response.status_code == 422


def test_encoded_slash_cannot_reach_the_handler(client: TestClient) -> None:
    # %2F is decoded to "/" before routing, so this becomes
    # /webhooks/with/slash, which matches no route at all.
    response = client.post("/webhooks/with%2Fslash", json=VALID_EVENT)

    assert response.status_code == 404


def test_rejects_an_unsupported_method(client: TestClient) -> None:
    response = client.get("/webhooks/github")

    assert response.status_code == 405


def test_unknown_path_returns_not_found(client: TestClient) -> None:
    response = client.post("/webhooks", json=VALID_EVENT)

    assert response.status_code == 404


def test_health_still_works(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
