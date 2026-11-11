from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import Role

EVENT: dict[str, Any] = {
    "event_id": "evt_1",
    "event_type": "invoice.paid",
    "occurred_at": "2026-11-07T12:00:00Z",
    "data": {"amount": 500},
}


@pytest.fixture
def anonymous(app: FastAPI) -> Iterator[TestClient]:
    """A client with no default credentials.

    The shared `client` fixture is an authenticated operator, which is what most
    tests want. This one starts from the outside so the refusal paths are real.
    """
    with TestClient(app) as test_client:
        yield test_client


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def issue(
    client: TestClient,
    name: str = "operator",
    role: Role = Role.OPERATOR,
) -> str:
    return client.app.state.keys.create(name, role).key


def record_processed_event(client: TestClient) -> int:
    response = client.post("/webhooks/github", json=EVENT)
    assert response.status_code == 202
    record_id = client.app.state.events.find("github", EVENT["event_id"]).id
    service = client.app.state.event_service
    attempt_id = service.start_processing(record_id)
    service.finish_processing(attempt_id, succeeded=True)
    return record_id


def test_health_is_open(anonymous: TestClient) -> None:
    assert anonymous.get("/health").status_code == 200


def test_reads_require_a_key(anonymous: TestClient) -> None:
    response = anonymous.get("/events")

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid or missing API key"
    assert response.headers["www-authenticate"] == "Bearer"


def test_event_detail_requires_a_key(anonymous: TestClient) -> None:
    assert anonymous.get("/events/1").status_code == 401


def test_replay_requires_a_key(anonymous: TestClient) -> None:
    response = anonymous.post(
        "/events/1/replay",
        json={"reason": "try again"},
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "authorization",
    ["", "Bearer", "Basic abc", "Token abc", "Bearer    "],
)
def test_malformed_authorization_headers_are_refused(
    anonymous: TestClient,
    authorization: str,
) -> None:
    response = anonymous.get("/events", headers={"Authorization": authorization})

    assert response.status_code == 401


def test_an_unknown_key_is_refused(anonymous: TestClient) -> None:
    response = anonymous.get("/events", headers=auth("hw_unknown"))

    assert response.status_code == 401


def test_a_revoked_key_is_refused(anonymous: TestClient) -> None:
    key = issue(anonymous, "retired", Role.VIEWER)
    assert anonymous.get("/events", headers=auth(key)).status_code == 200

    anonymous.app.state.keys.revoke("retired")

    assert anonymous.get("/events", headers=auth(key)).status_code == 401


def test_a_viewer_can_read(anonymous: TestClient) -> None:
    key = issue(anonymous, "dashboard", Role.VIEWER)

    response = anonymous.get("/events", headers=auth(key))

    assert response.status_code == 200
    assert response.json() == []


def test_a_viewer_cannot_replay(anonymous: TestClient) -> None:
    record_id = record_processed_event(anonymous)
    key = issue(anonymous, "dashboard", Role.VIEWER)

    response = anonymous.post(
        f"/events/{record_id}/replay",
        json={"reason": "try again"},
        headers=auth(key),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "operator role required"
    detail = anonymous.get(f"/events/{record_id}", headers=auth(key)).json()
    assert detail["status"] == "processed"
    assert detail["replays"] == []


def test_a_viewer_cannot_claim_the_operator_role_with_a_header(
    anonymous: TestClient,
) -> None:
    record_id = record_processed_event(anonymous)
    key = issue(anonymous, "dashboard", Role.VIEWER)

    response = anonymous.post(
        f"/events/{record_id}/replay",
        json={"reason": "try again"},
        headers={**auth(key), "X-Operator": "operator"},
    )

    assert response.status_code == 403


def test_an_operator_can_replay(anonymous: TestClient) -> None:
    record_id = record_processed_event(anonymous)
    key = issue(anonymous, "hermes", Role.OPERATOR)

    response = anonymous.post(
        f"/events/{record_id}/replay",
        json={"reason": "send it again"},
        headers=auth(key),
    )

    assert response.status_code == 202
    detail = anonymous.get(f"/events/{record_id}", headers=auth(key)).json()
    assert detail["replays"][0]["requested_by"] == "hermes"


def test_rotation_takes_effect_immediately(anonymous: TestClient) -> None:
    old_key = issue(anonymous, "hermes", Role.OPERATOR)
    new_key = anonymous.app.state.keys.rotate("hermes").key

    assert anonymous.get("/events", headers=auth(old_key)).status_code == 401
    assert anonymous.get("/events", headers=auth(new_key)).status_code == 200


def test_a_successful_request_records_the_last_use(
    anonymous: TestClient,
) -> None:
    key = issue(anonymous, "dashboard", Role.VIEWER)

    anonymous.get("/events", headers=auth(key))

    principal = anonymous.app.state.keys.list()[0]
    assert principal.last_used_at is not None
