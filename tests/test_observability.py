import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.db import Role
from app.main import create_app
from app.observability import (
    REQUEST_ID_HEADER,
    REQUEST_ID_PATTERN,
    RequestIdFilter,
    RequestIdFormatter,
    RequestIdMiddleware,
    current_request_id,
)
from app.settings import Settings

EVENT = {
    "event_id": "evt_1",
    "event_type": "invoice.paid",
    "occurred_at": "2026-11-07T12:00:00Z",
    "data": {"amount": 500},
}


class CaptureHandler(logging.Handler):
    """Collect formatted lines from the access logger."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def access_log() -> Iterator[CaptureHandler]:
    """Capture only the access lines, formatted as the application formats them."""
    logger = logging.getLogger("app.access")
    handler = CaptureHandler()
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(RequestIdFormatter())

    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


def build_app(database_path: Path, **overrides: object) -> FastAPI:
    values: dict[str, object] = {
        "database_path": database_path,
        "engine_enabled": False,
        "require_signatures": False,
        "rate_limit_per_minute": 0,
        "webhook_rate_limit_per_minute": 0,
    }
    values.update(overrides)
    return create_app(Settings(**values))


def test_generates_and_echoes_a_request_id(
    database_path: Path,
    access_log: CaptureHandler,
) -> None:
    app = build_app(database_path)

    with TestClient(app) as client:
        response = client.get("/health")

    request_id = response.headers[REQUEST_ID_HEADER]
    assert REQUEST_ID_PATTERN.fullmatch(request_id)
    assert len(access_log.lines) == 1

    line = access_log.lines[0]
    assert "method=GET" in line
    assert "path=/health" in line
    assert "status=200" in line
    assert f"request_id={request_id}" in line
    assert "duration_ms=" in line
    # Health is probe traffic, so it is logged at DEBUG rather than INFO.
    assert "DEBUG" in line


def test_reuses_a_valid_inbound_request_id(
    database_path: Path,
    access_log: CaptureHandler,
) -> None:
    app = build_app(database_path)

    with TestClient(app) as client:
        response = client.get(
            "/health",
            headers={REQUEST_ID_HEADER: "deploy-42"},
        )

    assert response.headers[REQUEST_ID_HEADER] == "deploy-42"
    assert "request_id=deploy-42" in access_log.lines[0]


@pytest.mark.parametrize(
    "candidate",
    ["bad id", "x" * 65, "id=forged", "id/forged"],
)
def test_replaces_an_invalid_inbound_request_id(
    database_path: Path,
    access_log: CaptureHandler,
    candidate: str,
) -> None:
    app = build_app(database_path)

    with TestClient(app) as client:
        response = client.get(
            "/health",
            headers={REQUEST_ID_HEADER: candidate},
        )

    request_id = response.headers[REQUEST_ID_HEADER]
    assert request_id != candidate
    assert REQUEST_ID_PATTERN.fullmatch(request_id)
    assert f"request_id={request_id}" in access_log.lines[0]


def test_every_request_gets_its_own_id(database_path: Path) -> None:
    app = build_app(database_path)

    with TestClient(app) as client:
        first = client.get("/health").headers[REQUEST_ID_HEADER]
        second = client.get("/health").headers[REQUEST_ID_HEADER]

    assert first != second


def test_access_line_names_the_verified_principal(
    database_path: Path,
    access_log: CaptureHandler,
) -> None:
    app = build_app(database_path)

    with TestClient(app) as client:
        key = app.state.keys.create("alice", Role.VIEWER).key
        response = client.get(
            "/events",
            headers={"Authorization": f"Bearer {key}"},
        )

    assert response.status_code == 200
    line = access_log.lines[0]
    assert "status=200" in line
    assert "principal=alice" in line
    assert "INFO" in line


def test_access_line_names_the_webhook_source(
    database_path: Path,
    access_log: CaptureHandler,
) -> None:
    app = build_app(database_path)

    with TestClient(app) as client:
        response = client.post("/webhooks/github", json=EVENT)

    assert response.status_code == 202
    line = access_log.lines[0]
    assert "status=202" in line
    assert "source=github" in line


def test_a_rate_limited_delivery_still_names_the_source(
    database_path: Path,
    access_log: CaptureHandler,
) -> None:
    app = build_app(database_path, webhook_rate_limit_per_minute=1)

    with TestClient(app) as client:
        assert client.post("/webhooks/github", json=EVENT).status_code == 202
        denied = client.post("/webhooks/github", json=EVENT)

    assert denied.status_code == 429
    assert denied.headers[REQUEST_ID_HEADER]
    line = access_log.lines[-1]
    assert "status=429" in line
    assert "source=github" in line


def test_an_anonymous_read_has_no_principal(
    database_path: Path,
    access_log: CaptureHandler,
) -> None:
    app = build_app(database_path)

    with TestClient(app) as client:
        response = client.get("/events")

    assert response.status_code == 401
    assert response.headers[REQUEST_ID_HEADER]
    line = access_log.lines[0]
    assert "status=401" in line
    assert "principal=" not in line


def test_an_oversized_body_gets_an_id_and_a_413_line(
    database_path: Path,
    access_log: CaptureHandler,
) -> None:
    app = build_app(database_path, max_body_bytes=16)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/github",
            content=b"x" * 17,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    request_id = response.headers[REQUEST_ID_HEADER]
    line = access_log.lines[-1]
    assert "status=413" in line
    assert f"request_id={request_id}" in line


def test_a_validation_error_still_names_the_source(
    database_path: Path,
    access_log: CaptureHandler,
) -> None:
    app = build_app(database_path)

    with TestClient(app) as client:
        response = client.post("/webhooks/github", json={"event_id": "only"})

    assert response.status_code == 422
    line = access_log.lines[-1]
    assert "status=422" in line
    assert "source=github" in line


def test_handler_lines_share_the_request_id(database_path: Path) -> None:
    app = build_app(database_path)
    handler = CaptureHandler()
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(RequestIdFormatter())
    logger = logging.getLogger("app.api.webhooks")
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        with TestClient(app) as client:
            response = client.post("/webhooks/github", json=EVENT)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    request_id = response.headers[REQUEST_ID_HEADER]
    assert any(
        "webhook accepted" in line and f"request_id={request_id}" in line
        for line in handler.lines
    )


def test_the_access_log_never_contains_credentials_or_bodies(
    database_path: Path,
    access_log: CaptureHandler,
) -> None:
    app = build_app(database_path)

    with TestClient(app) as client:
        key = app.state.keys.create("alice", Role.OPERATOR).key
        client.get(
            "/events",
            headers={
                "Authorization": f"Bearer {key}",
                REQUEST_ID_HEADER: "trace-1",
            },
        )
        client.post("/webhooks/github", json=EVENT)

    text = "\n".join(access_log.lines)
    assert key not in text
    assert "Authorization" not in text
    assert "invoice.paid" not in text
    assert "amount" not in text


def test_concurrent_requests_keep_their_own_id(
    access_log: CaptureHandler,
) -> None:
    started = 0
    release = asyncio.Event()
    seen: dict[str, str | None] = {}

    async def app(scope: dict, receive, send) -> None:
        nonlocal started
        started += 1
        if started == 2:
            release.set()
        await release.wait()
        seen[scope["path"]] = current_request_id()
        await JSONResponse({"ok": True})(scope, receive, send)

    middleware = RequestIdMiddleware(app)

    async def call(path: str, request_id: str) -> None:
        scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [(b"x-request-id", request_id.encode())],
        }

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict) -> None:
            return None

        await middleware(scope, receive, send)

    async def run() -> None:
        await asyncio.gather(
            call("/first", "id-first"),
            call("/second", "id-second"),
        )

    asyncio.run(run())

    assert seen == {"/first": "id-first", "/second": "id-second"}


def test_a_disconnect_before_a_response_is_logged_as_499(
    access_log: CaptureHandler,
) -> None:
    async def app(scope: dict, receive, send) -> None:
        return None

    middleware = RequestIdMiddleware(app)

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        raise AssertionError("no response should be sent")

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/webhooks/github",
        "headers": [],
    }

    asyncio.run(middleware(scope, receive, send))

    assert "status=499" in access_log.lines[0]


def test_an_unhandled_error_is_logged_as_500(
    access_log: CaptureHandler,
) -> None:
    async def app(scope: dict, receive, send) -> None:
        raise RuntimeError("boom")

    middleware = RequestIdMiddleware(app)

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        return None

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/events",
        "headers": [],
    }

    with pytest.raises(RuntimeError):
        asyncio.run(middleware(scope, receive, send))

    assert "status=500" in access_log.lines[0]


def test_the_formatter_marks_lines_outside_a_request() -> None:
    record = logging.LogRecord(
        "app.engine",
        logging.INFO,
        __file__,
        1,
        "tick",
        None,
        None,
    )

    line = RequestIdFormatter().format(record)

    assert line == "INFO:app.engine:tick request_id=-"


def test_the_filter_marks_lines_outside_a_request() -> None:
    record = logging.LogRecord(
        "app.engine",
        logging.INFO,
        __file__,
        1,
        "tick",
        None,
        None,
    )

    assert RequestIdFilter().filter(record)
    assert record.request_id == "-"
