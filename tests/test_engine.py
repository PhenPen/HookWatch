import asyncio
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.engine import run_forever
from app.lifecycle import EventStatus
from app.main import create_app
from app.services.events import ProcessingSettings
from app.settings import Settings

VALID_EVENT: dict[str, Any] = {
    "event_id": "evt_engine",
    "event_type": "invoice.paid",
    "occurred_at": "2026-11-07T12:00:00Z",
    "data": {"amount": 500},
}


class FakeService:
    """Minimal stand-in for EventService, so loop tests need no database."""

    def __init__(self, results: list[object]) -> None:
        self.settings = ProcessingSettings(poll_interval_seconds=0.01)
        self.results = list(results)
        self.calls = 0

    def process_due(self) -> int:
        self.calls += 1
        result = self.results.pop(0) if self.results else 0
        if isinstance(result, Exception):
            raise result
        return int(result)


def test_loop_keeps_processing_until_stopped() -> None:
    async def scenario() -> int:
        service = FakeService([1, 1, 1])
        stop = asyncio.Event()
        task = asyncio.create_task(run_forever(service, stop))

        for _ in range(1000):
            if service.calls >= 3:
                break
            await asyncio.sleep(0.001)

        stop.set()
        await asyncio.wait_for(task, timeout=2)
        return service.calls

    assert asyncio.run(scenario()) >= 3


def test_loop_survives_an_exception() -> None:
    async def scenario() -> int:
        service = FakeService([RuntimeError("boom"), 0, 0])
        stop = asyncio.Event()
        task = asyncio.create_task(run_forever(service, stop))

        for _ in range(1000):
            if service.calls >= 3:
                break
            await asyncio.sleep(0.001)

        stop.set()
        await asyncio.wait_for(task, timeout=2)
        return service.calls

    assert asyncio.run(scenario()) >= 3


def test_loop_stops_promptly_when_idle() -> None:
    async def scenario() -> None:
        service = FakeService([])
        stop = asyncio.Event()
        task = asyncio.create_task(run_forever(service, stop))

        await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())


def test_engine_processes_events_inside_fastapi(database_path: Path) -> None:
    settings = Settings(
        database_path=database_path,
        engine_enabled=True,
        processing=ProcessingSettings(poll_interval_seconds=0.01),
    )
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post("/webhooks/github", json=VALID_EVENT)
        assert response.status_code == 202

        deadline = time.monotonic() + 3
        stored = None
        while time.monotonic() < deadline:
            events = app.state.events.list()
            if events and events[0].status is EventStatus.PROCESSED:
                stored = events[0]
                break
            time.sleep(0.01)

    assert stored is not None
    assert stored.status is EventStatus.PROCESSED


def test_engine_can_be_disabled(database_path: Path) -> None:
    app = create_app(Settings(database_path=database_path, engine_enabled=False))

    with TestClient(app) as client:
        response = client.post("/webhooks/github", json=VALID_EVENT)
        assert response.status_code == 202
        time.sleep(0.05)
        stored = app.state.events.list()[0]

    assert stored.status is EventStatus.RECEIVED
