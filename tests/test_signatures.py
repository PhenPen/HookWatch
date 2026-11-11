import json
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.signatures import (
    SIGNATURE_HEADER,
    SignatureError,
    parse_signature_header,
    sign,
    signature_header,
    verify_signature,
)
from app.main import create_app
from app.settings import Settings

SECRET = "github-signing-secret"
STRIPE_SECRET = "stripe-signing-secret"
TOLERANCE = 300

VALID_EVENT: dict[str, Any] = {
    "event_id": "evt_123",
    "event_type": "invoice.paid",
    "occurred_at": "2026-11-07T12:00:00Z",
    "data": {"amount": 500, "currency": "USD"},
}


def make_body(payload: dict[str, Any] | None = None) -> bytes:
    """Serialise one payload exactly once, so the signature covers real bytes."""
    return json.dumps(
        VALID_EVENT if payload is None else payload,
        separators=(",", ":"),
    ).encode("utf-8")


def headers_for(
    secret: str,
    body: bytes,
    timestamp: int | None = None,
) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: signature_header(secret, body, timestamp),
    }


def post_signed(
    client: TestClient,
    source: str,
    body: bytes,
    secret: str = SECRET,
    timestamp: int | None = None,
):
    return client.post(
        f"/webhooks/{source}",
        content=body,
        headers=headers_for(secret, body, timestamp),
    )


@pytest.fixture
def signed_app(database_path) -> FastAPI:
    return create_app(
        Settings(
            database_path=database_path,
            engine_enabled=False,
            webhook_secrets={"github": SECRET, "stripe": STRIPE_SECRET},
            require_signatures=True,
            signature_tolerance_seconds=TOLERANCE,
        )
    )


@pytest.fixture
def signed_client(signed_app: FastAPI):
    with TestClient(signed_app) as client:
        yield client


def test_accepts_a_correctly_signed_delivery(
    signed_client: TestClient,
    signed_app: FastAPI,
) -> None:
    body = make_body()

    response = post_signed(signed_client, "github", body)

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert len(signed_app.state.events.list()) == 1


def test_rejects_a_missing_signature(
    signed_client: TestClient,
    signed_app: FastAPI,
) -> None:
    response = signed_client.post("/webhooks/github", json=VALID_EVENT)

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid or missing webhook signature"
    assert signed_app.state.events.list() == []


def test_rejects_the_wrong_secret(
    signed_client: TestClient,
    signed_app: FastAPI,
) -> None:
    body = make_body()

    response = post_signed(signed_client, "github", body, secret="not-the-secret")

    assert response.status_code == 401
    assert signed_app.state.events.list() == []


def test_rejects_a_tampered_body(
    signed_client: TestClient,
    signed_app: FastAPI,
) -> None:
    signed_body = make_body({"event_id": "evt_signed"})
    tampered_body = make_body(
        {
            "event_id": "evt_signed",
            "event_type": "invoice.paid",
            "occurred_at": "2026-11-07T12:00:00Z",
            "data": {"amount": 1},
        }
    )
    response = signed_client.post(
        "/webhooks/github",
        content=tampered_body,
        headers=headers_for(SECRET, signed_body),
    )

    assert response.status_code == 401
    assert signed_app.state.events.list() == []


def test_rejects_a_stale_signature(
    signed_client: TestClient,
    signed_app: FastAPI,
) -> None:
    body = make_body()

    response = post_signed(
        signed_client,
        "github",
        body,
        timestamp=int(time.time()) - TOLERANCE - 100,
    )

    assert response.status_code == 401
    assert signed_app.state.events.list() == []


def test_rejects_a_signature_from_the_future(
    signed_client: TestClient,
    signed_app: FastAPI,
) -> None:
    body = make_body()

    response = post_signed(
        signed_client,
        "github",
        body,
        timestamp=int(time.time()) + TOLERANCE + 100,
    )

    assert response.status_code == 401
    assert signed_app.state.events.list() == []


def test_accepts_a_signature_at_the_edge_of_the_tolerance(
    signed_client: TestClient,
) -> None:
    body = make_body()

    response = post_signed(
        signed_client,
        "github",
        body,
        timestamp=int(time.time()) - TOLERANCE + 5,
    )

    assert response.status_code == 202


def test_rejects_an_unconfigured_source(
    signed_client: TestClient,
    signed_app: FastAPI,
) -> None:
    body = make_body()

    response = post_signed(signed_client, "shopify", body)

    assert response.status_code == 401
    assert signed_app.state.events.list() == []


def test_rejects_an_unsigned_unconfigured_source(
    signed_client: TestClient,
    signed_app: FastAPI,
) -> None:
    response = signed_client.post("/webhooks/shopify", json=VALID_EVENT)

    assert response.status_code == 401
    assert signed_app.state.events.list() == []


def test_uses_the_secret_for_the_named_source(
    signed_client: TestClient,
) -> None:
    github_body = make_body({**VALID_EVENT, "event_id": "evt_github"})
    stripe_body = make_body({**VALID_EVENT, "event_id": "evt_stripe"})

    assert post_signed(signed_client, "github", github_body).status_code == 202
    assert (
        post_signed(
            signed_client,
            "stripe",
            stripe_body,
            secret=STRIPE_SECRET,
        ).status_code
        == 202
    )

    response = post_signed(
        signed_client,
        "github",
        make_body({**VALID_EVENT, "event_id": "evt_cross"}),
        secret=STRIPE_SECRET,
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "value",
    [
        "t=1",
        f"v1={'0' * 64}",
        f"t=abc,v1={'0' * 64}",
        "t=1,v1=short",
        "t=1,v1=" + "z" * 64,
        "t=1,v1=" + "0" * 63,
        f"t=1,v1={'0' * 64},v1={'0' * 64}",
        f"t=1,v1={'0' * 64},v2=extra",
        f"1,v1={'0' * 64}",
        "",
    ],
)
def test_rejects_a_malformed_signature_header(
    signed_client: TestClient,
    signed_app: FastAPI,
    value: str,
) -> None:
    body = make_body()

    response = signed_client.post(
        "/webhooks/github",
        content=body,
        headers={"Content-Type": "application/json", SIGNATURE_HEADER: value},
    )

    assert response.status_code == 401
    assert signed_app.state.events.list() == []


def test_enforcement_off_accepts_an_unsigned_delivery(database_path) -> None:
    app = create_app(
        Settings(
            database_path=database_path,
            engine_enabled=False,
            webhook_secrets={"github": SECRET},
            require_signatures=False,
        )
    )

    with TestClient(app) as client:
        response = client.post("/webhooks/github", json=VALID_EVENT)

    assert response.status_code == 202


def test_enforcement_off_still_checks_a_supplied_signature(database_path) -> None:
    app = create_app(
        Settings(
            database_path=database_path,
            engine_enabled=False,
            webhook_secrets={"github": SECRET},
            require_signatures=False,
        )
    )
    body = make_body()

    with TestClient(app) as client:
        accepted = post_signed(client, "github", body)
        rejected = post_signed(client, "github", body, secret="wrong")

    assert accepted.status_code == 202
    assert rejected.status_code == 401


def test_enforcement_off_rejects_a_signature_it_cannot_check(database_path) -> None:
    app = create_app(
        Settings(
            database_path=database_path,
            engine_enabled=False,
            require_signatures=False,
        )
    )

    with TestClient(app) as client:
        unsigned = client.post("/webhooks/github", json=VALID_EVENT)
        body = make_body()
        signed = post_signed(client, "github", body)

    assert unsigned.status_code == 202
    assert signed.status_code == 401


def test_startup_fails_when_enforcement_has_no_secrets(database_path) -> None:
    app = create_app(
        Settings(database_path=database_path, engine_enabled=False)
    )

    with pytest.raises(ValueError, match="at least one webhook secret"):
        with TestClient(app):
            pass


def test_signature_depends_on_the_exact_bytes() -> None:
    compact = b'{"a":1,"b":2}'
    spaced = b'{"a": 1, "b": 2}'
    header = signature_header(SECRET, compact, int(time.time()))

    with pytest.raises(SignatureError):
        verify_signature(SECRET, spaced, header, tolerance_seconds=TOLERANCE)


def test_signature_covers_the_timestamp() -> None:
    body = make_body()
    current = int(time.time())
    header = signature_header(SECRET, body, current).replace(
        f"t={current}",
        f"t={current + 1}",
        1,
    )

    with pytest.raises(SignatureError):
        verify_signature(SECRET, body, header, tolerance_seconds=TOLERANCE)


def test_signature_depends_on_the_secret() -> None:
    body = make_body()
    now = int(time.time())

    assert sign(SECRET, body, now) != sign(STRIPE_SECRET, body, now)


def test_verify_accepts_the_edge_of_the_tolerance() -> None:
    body = make_body()
    header = signature_header(SECRET, body, 1000)

    verify_signature(SECRET, body, header, tolerance_seconds=300, now=1300)
    verify_signature(SECRET, body, header, tolerance_seconds=300, now=700)


def test_verify_rejects_a_stale_or_future_timestamp() -> None:
    body = make_body()
    header = signature_header(SECRET, body, 1000)

    with pytest.raises(SignatureError, match="stale"):
        verify_signature(SECRET, body, header, tolerance_seconds=300, now=1301)

    with pytest.raises(SignatureError, match="future"):
        verify_signature(SECRET, body, header, tolerance_seconds=300, now=699)


def test_parse_signature_header_returns_the_fields() -> None:
    body = make_body()
    header = signature_header(SECRET, body, 1000)

    parts = parse_signature_header(header)

    assert parts.timestamp == 1000
    assert parts.digest == sign(SECRET, body, 1000)
