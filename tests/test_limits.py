import json
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from app.api.limits import BodySizeLimitMiddleware, RateLimiter
from app.db import Role, connect
from app.main import create_app
from app.settings import Settings

EVENT = {
    "event_id": "evt_1",
    "event_type": "invoice.paid",
    "occurred_at": "2026-11-07T12:00:00Z",
    "data": {"amount": 500},
}


@pytest.fixture
def anyio_backend() -> str:
    """Run the async middleware tests on asyncio only."""
    return "asyncio"


class FakeClock:
    """A clock tests move by hand, so no test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def build_app(database_path: Path, **overrides: object) -> FastAPI:
    return create_app(
        Settings(
            database_path=database_path,
            engine_enabled=False,
            require_signatures=False,
            **overrides,
        )
    )


def stored_event_count(database_path: Path) -> int:
    with closing(connect(database_path)) as connection:
        row = connection.execute("SELECT COUNT(*) FROM events").fetchone()
    return int(row[0])


def test_a_caller_can_spend_a_full_minute_then_must_wait() -> None:
    limiter = RateLimiter(3, clock=FakeClock())

    assert [limiter.check("key") for _ in range(3)] == [None, None, None]
    assert limiter.check("key") == pytest.approx(20.0)


def test_tokens_refill_with_time() -> None:
    clock = FakeClock()
    limiter = RateLimiter(2, clock=clock)

    assert limiter.check("key") is None
    assert limiter.check("key") is None
    assert limiter.check("key") is not None

    clock.now = 30.0

    assert limiter.check("key") is None
    assert limiter.check("key") is not None


def test_keys_have_independent_buckets() -> None:
    limiter = RateLimiter(1)

    assert limiter.check("first") is None
    assert limiter.check("first") is not None
    assert limiter.check("second") is None


def test_zero_disables_the_limiter() -> None:
    limiter = RateLimiter(0)

    assert all(limiter.check("key") is None for _ in range(100))


def test_the_bucket_map_is_bounded() -> None:
    limiter = RateLimiter(1, max_buckets=2)

    for key in ("first", "second", "third"):
        limiter.check(key)

    assert limiter.bucket_count == 2


def test_concurrent_checks_do_not_over_admit() -> None:
    limiter = RateLimiter(10, clock=lambda: 0.0)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: limiter.check("key"), range(100)))

    assert sum(result is None for result in results) == 10


def test_denials_are_logged_at_most_once_per_second(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    limiter = RateLimiter(1, clock=clock)
    limiter.check("key")

    with caplog.at_level(logging.WARNING, logger="app.api.limits"):
        for _ in range(5):
            limiter.check("key")
        clock.now = 1.0
        limiter.check("key")

    assert caplog.text.count("rate limit exceeded") == 2


def http_scope(headers: list[tuple[str, str]] | None = None) -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": "/webhooks/github",
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in (headers or [])
        ],
    }


async def run_body_middleware(
    messages: list[dict],
    *,
    max_bytes: int,
    scope: dict | None = None,
) -> list[dict]:
    sent: list[dict] = []

    async def receive() -> dict:
        return messages.pop(0)

    async def send(message: dict) -> None:
        sent.append(message)

    async def app(scope: dict, receive, send) -> None:
        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        await JSONResponse({"length": len(body)})(scope, receive, send)

    middleware = BodySizeLimitMiddleware(app, max_bytes=max_bytes)
    await middleware(scope or http_scope(), receive, send)
    return sent


@pytest.mark.anyio
async def test_a_declared_length_over_the_cap_is_refused_without_reading() -> None:
    messages = [{"type": "http.request", "body": b"x" * 20, "more_body": False}]

    sent = await run_body_middleware(
        messages,
        max_bytes=10,
        scope=http_scope([("Content-Length", "20")]),
    )

    assert sent[0]["status"] == 413
    assert b"request body too large" in sent[1]["body"]
    assert messages


@pytest.mark.anyio
async def test_a_streamed_body_over_the_cap_is_refused() -> None:
    messages = [
        {"type": "http.request", "body": b"12345", "more_body": True},
        {"type": "http.request", "body": b"67890", "more_body": True},
        {"type": "http.request", "body": b"1", "more_body": False},
    ]

    sent = await run_body_middleware(messages, max_bytes=10)

    assert sent[0]["status"] == 413


@pytest.mark.anyio
async def test_a_body_at_the_cap_is_replayed_exactly() -> None:
    payload = b'{"event_id":"evt_1"}'
    messages = [
        {"type": "http.request", "body": payload[:5], "more_body": True},
        {"type": "http.request", "body": payload[5:], "more_body": False},
    ]

    sent = await run_body_middleware(messages, max_bytes=len(payload))

    assert sent[0]["status"] == 200
    assert json.loads(sent[1]["body"]) == {"length": len(payload)}


@pytest.mark.anyio
async def test_zero_disables_the_body_cap() -> None:
    messages = [
        {"type": "http.request", "body": b"x" * 1000, "more_body": False}
    ]

    sent = await run_body_middleware(messages, max_bytes=0)

    assert sent[0]["status"] == 200


@pytest.mark.anyio
async def test_a_disconnect_before_the_body_arrives_sends_nothing() -> None:
    sent = await run_body_middleware(
        [{"type": "http.disconnect"}],
        max_bytes=10,
    )

    assert sent == []


def test_an_oversized_delivery_is_refused_and_not_stored(
    database_path: Path,
) -> None:
    app = build_app(database_path, max_body_bytes=64)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/github",
            content=b"x" * 65,
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 413
        assert response.json()["detail"] == "request body too large"
        assert response.headers["connection"] == "close"
        assert stored_event_count(database_path) == 0


def test_reads_are_limited_per_key(database_path: Path) -> None:
    app = build_app(database_path, rate_limit_per_minute=2)

    with TestClient(app) as client:
        first = app.state.keys.create("first", Role.VIEWER).key
        second = app.state.keys.create("second", Role.VIEWER).key
        headers = {"Authorization": f"Bearer {first}"}

        assert client.get("/events", headers=headers).status_code == 200
        assert client.get("/events", headers=headers).status_code == 200

        denied = client.get("/events", headers=headers)

        assert denied.status_code == 429
        assert denied.headers["retry-after"] == "30"
        assert denied.json()["detail"] == "rate limit exceeded"

        other = {"Authorization": f"Bearer {second}"}
        assert client.get("/events", headers=other).status_code == 200


def test_an_invalid_key_is_still_a_401(database_path: Path) -> None:
    app = build_app(database_path, rate_limit_per_minute=1)

    with TestClient(app) as client:
        for _ in range(3):
            response = client.get(
                "/events",
                headers={"Authorization": "Bearer hw_unknown"},
            )
            assert response.status_code == 401


def test_health_is_not_rate_limited(database_path: Path) -> None:
    app = build_app(database_path, rate_limit_per_minute=1)

    with TestClient(app) as client:
        for _ in range(5):
            assert client.get("/health").status_code == 200


def test_deliveries_are_limited_per_source(database_path: Path) -> None:
    app = build_app(database_path, webhook_rate_limit_per_minute=2)

    with TestClient(app) as client:
        for event_id in ("evt_1", "evt_2"):
            response = client.post(
                "/webhooks/github",
                json={**EVENT, "event_id": event_id},
            )
            assert response.status_code == 202

        denied = client.post(
            "/webhooks/github",
            json={**EVENT, "event_id": "evt_3"},
        )

        assert denied.status_code == 429
        assert denied.headers["retry-after"] == "30"

        # A different source has its own bucket.
        response = client.post(
            "/webhooks/stripe",
            json={**EVENT, "event_id": "evt_4"},
        )
        assert response.status_code == 202
        assert stored_event_count(database_path) == 3


def test_replay_shares_the_read_allowance(database_path: Path) -> None:
    app = build_app(database_path, rate_limit_per_minute=2)

    with TestClient(app) as client:
        key = app.state.keys.create("operator", Role.OPERATOR).key
        headers = {"Authorization": f"Bearer {key}"}

        assert client.post("/webhooks/github", json=EVENT).status_code == 202
        stored = app.state.events.find("github", EVENT["event_id"])
        assert stored is not None

        service = app.state.event_service
        attempt_id = service.start_processing(stored.id)
        service.finish_processing(attempt_id, succeeded=True)

        assert client.get("/events", headers=headers).status_code == 200
        replay = client.post(
            f"/events/{stored.id}/replay",
            json={"reason": "retry after a fix"},
            headers=headers,
        )
        assert replay.status_code == 202

        denied = client.get("/events", headers=headers)
        assert denied.status_code == 429
