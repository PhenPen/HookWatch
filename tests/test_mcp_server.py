import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError

from app.db import migrate
from app.lifecycle import EventStatus, FailureKind
from app.mcp_server import create_mcp_server
from app.repositories.events import EventRepository
from app.schemas.event import WebhookEvent
from app.services.events import EventService


@pytest.fixture
def anyio_backend() -> str:
    """Run the async MCP client tests on asyncio only."""
    return "asyncio"


def event(event_id: str) -> WebhookEvent:
    return WebhookEvent(
        event_id=event_id,
        event_type="invoice.paid",
        occurred_at=datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc),
        data={"amount": 500},
    )


def seed(database_path: Path) -> dict[str, int]:
    """Create one event in each state the read tools need to show.

    Returns the database ids. The replayed event has two attempts, two
    dead-letter decisions and one replay audit row, so history tools have
    something real to return.
    """
    service = EventService(EventRepository(database_path))

    received = service.record_delivery("github", event("evt_received")).record_id

    processed = service.record_delivery("github", event("evt_processed")).record_id
    attempt = service.start_processing(processed)
    service.finish_processing(attempt, succeeded=True)

    failed = service.record_delivery("stripe", event("evt_failed")).record_id
    attempt = service.start_processing(failed)
    service.finish_processing(
        attempt,
        succeeded=False,
        error="downstream timeout",
        failure_kind=FailureKind.RETRYABLE,
        dead_letter=True,
    )

    replayed = service.record_delivery("stripe", event("evt_replayed")).record_id
    attempt = service.start_processing(replayed)
    service.finish_processing(
        attempt,
        succeeded=False,
        error="card declined",
        failure_kind=FailureKind.PERMANENT,
        dead_letter=True,
    )
    service.replay_event(replayed, reason="fixed the card", requested_by="alice")
    attempt = service.start_processing(replayed)
    service.finish_processing(
        attempt,
        succeeded=False,
        error="card still declined",
        failure_kind=FailureKind.PERMANENT,
        dead_letter=True,
    )

    return {
        "received": received,
        "processed": processed,
        "failed": failed,
        "replayed": replayed,
    }


@pytest.fixture
def seeded_database(database_path: Path) -> dict[str, int]:
    migrate(database_path)
    return seed(database_path)


@pytest.fixture
def server(seeded_database: dict[str, int], database_path: Path) -> MCPServer:
    """A server with replay disabled, so discovery is deterministic."""
    return create_mcp_server(database_path, replay_token="")


@pytest.fixture
def replay_server(seeded_database: dict[str, int], database_path: Path) -> MCPServer:
    """A server that holds the replay secret and names itself in the audit."""
    return create_mcp_server(
        database_path,
        replay_token="test-token",
        operator="hermes",
    )


@pytest.mark.anyio
async def test_discovers_the_read_only_tools(server: MCPServer) -> None:
    async with Client(server) as client:
        tools = await client.list_tools()

    names = {tool.name for tool in tools.tools}
    assert names == {
        "list_events",
        "get_event",
        "search_failed_events",
        "inspect_processing_history",
    }
    assert all(tool.description for tool in tools.tools)


@pytest.mark.anyio
async def test_list_events_returns_summaries_newest_first(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.call_tool("list_events", {})

    assert result.is_error is False
    events = result.structured_content["result"]
    assert [item["event_id"] for item in events] == [
        "evt_replayed",
        "evt_failed",
        "evt_processed",
        "evt_received",
    ]
    assert events[0]["status"] == "failed"
    assert events[0]["dead_lettered_at"] is not None
    assert events[1]["replay_count"] == 0
    # Listings stay small; the payload belongs to the detail tool.
    assert all("data" not in item for item in events)
    # The text blocks carry the same data for clients that do not read structured content.
    assert [json.loads(block.text) for block in result.content] == events


@pytest.mark.anyio
async def test_list_events_applies_filters_and_paging(server: MCPServer) -> None:
    async with Client(server) as client:
        by_source = await client.call_tool("list_events", {"source": "stripe"})
        by_status = await client.call_tool("list_events", {"status": "processed"})
        paged = await client.call_tool("list_events", {"limit": 1, "offset": 1})

    assert [item["event_id"] for item in by_source.structured_content["result"]] == [
        "evt_replayed",
        "evt_failed",
    ]
    assert [item["event_id"] for item in by_status.structured_content["result"]] == [
        "evt_processed"
    ]
    assert [item["event_id"] for item in paged.structured_content["result"]] == [
        "evt_failed"
    ]


@pytest.mark.anyio
async def test_get_event_returns_payload_attempts_and_replays(
    server: MCPServer, seeded_database: dict[str, int]
) -> None:
    async with Client(server) as client:
        result = await client.call_tool(
            "get_event", {"record_id": seeded_database["replayed"]}
        )

    assert result.is_error is False
    detail = result.structured_content
    assert detail["event_id"] == "evt_replayed"
    assert detail["data"] == {"amount": 500}
    assert detail["replay_count"] == 1
    assert detail["dead_lettered_at"] is not None
    assert [attempt["replay_number"] for attempt in detail["attempts"]] == [0, 1]
    assert detail["attempts"][0]["failure_kind"] == "permanent"
    assert len(detail["replays"]) == 1
    assert detail["replays"][0]["requested_by"] == "alice"
    assert detail["replays"][0]["reason"] == "fixed the card"


@pytest.mark.anyio
async def test_unknown_event_is_a_tool_error(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.call_tool("get_event", {"record_id": 999})

    assert result.is_error is True
    assert "event 999 does not exist" in result.content[0].text


@pytest.mark.anyio
async def test_invalid_arguments_are_rejected_before_the_service_runs(
    server: MCPServer,
) -> None:
    async with Client(server) as client:
        bad_limit = await client.call_tool("list_events", {"limit": 0})
        bad_status = await client.call_tool("list_events", {"status": "validated"})

    assert bad_limit.is_error is True
    assert "greater than or equal to 1" in bad_limit.content[0].text
    assert bad_status.is_error is True
    assert "validated" in bad_status.content[0].text


@pytest.mark.anyio
async def test_search_failed_events_filters_by_dead_letter_state(
    server: MCPServer,
) -> None:
    async with Client(server) as client:
        all_failed = await client.call_tool("search_failed_events", {})
        live_failures = await client.call_tool(
            "search_failed_events", {"dead_lettered": False}
        )

    failed = all_failed.structured_content["result"]
    assert [item["event_id"] for item in failed] == ["evt_replayed", "evt_failed"]
    assert all(item["status"] == "failed" for item in failed)
    assert live_failures.structured_content["result"] == []


@pytest.mark.anyio
async def test_inspect_processing_history_omits_the_payload(
    server: MCPServer, seeded_database: dict[str, int]
) -> None:
    async with Client(server) as client:
        result = await client.call_tool(
            "inspect_processing_history", {"record_id": seeded_database["replayed"]}
        )

    history = result.structured_content
    assert "data" not in history
    assert history["event_id"] == "evt_replayed"
    assert history["status"] == "failed"
    assert history["replay_count"] == 1
    assert len(history["attempts"]) == 2
    assert [attempt["replay_number"] for attempt in history["attempts"]] == [0, 1]
    assert history["replays"][0]["requested_by"] == "alice"


@pytest.mark.anyio
async def test_event_resource_returns_the_same_detail_as_the_tool(
    server: MCPServer, seeded_database: dict[str, int]
) -> None:
    processed_id = seeded_database["processed"]

    async with Client(server) as client:
        resource = await client.read_resource(f"hookwatch://events/{processed_id}")
        tool = await client.call_tool("get_event", {"record_id": processed_id})

    content = resource.contents[0]
    assert content.mime_type == "application/json"
    assert json.loads(content.text) == tool.structured_content


@pytest.mark.anyio
async def test_unknown_resource_is_an_error(server: MCPServer) -> None:
    async with Client(server) as client:
        with pytest.raises(MCPError, match="event 999 does not exist"):
            await client.read_resource("hookwatch://events/999")


@pytest.mark.anyio
async def test_annotations_separate_reads_from_replay(
    replay_server: MCPServer,
) -> None:
    async with Client(replay_server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    for name in (
        "list_events",
        "get_event",
        "search_failed_events",
        "inspect_processing_history",
    ):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
        assert annotations.destructive_hint is not True

    replay = tools["replay_event"].annotations
    assert replay is not None
    assert replay.read_only_hint is False
    assert replay.destructive_hint is True
    # Every call starts a new cycle, so repeating it is not the same outcome.
    assert replay.idempotent_hint is False


@pytest.mark.anyio
async def test_replay_is_absent_without_a_token(server: MCPServer) -> None:
    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}

    assert "replay_event" not in names


@pytest.mark.anyio
async def test_environment_token_enables_replay_and_empty_string_disables_it(
    database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOOKWATCH_REPLAY_TOKEN", "env-token")

    async with Client(create_mcp_server(database_path)) as client:
        from_environment = {tool.name for tool in (await client.list_tools()).tools}

    async with Client(
        create_mcp_server(database_path, replay_token="")
    ) as client:
        explicitly_closed = {tool.name for tool in (await client.list_tools()).tools}

    assert "replay_event" in from_environment
    assert "replay_event" not in explicitly_closed


@pytest.mark.anyio
async def test_replay_queues_the_event_and_records_the_operator(
    replay_server: MCPServer,
    seeded_database: dict[str, int],
    database_path: Path,
) -> None:
    record_id = seeded_database["failed"]

    async with Client(replay_server) as client:
        result = await client.call_tool(
            "replay_event",
            {"record_id": record_id, "reason": "fixed the downstream timeout"},
        )

    assert result.is_error is False
    assert result.structured_content == {
        "status": "queued",
        "id": record_id,
        "replay_number": 1,
    }

    service = EventService(EventRepository(database_path))
    stored = service.get_event(record_id)
    assert stored.status is EventStatus.RECEIVED
    assert stored.replay_count == 1

    history = service.get_event_history(record_id)
    assert history.replays[0].requested_by == "hermes"
    assert history.replays[0].reason == "fixed the downstream timeout"


@pytest.mark.anyio
async def test_replay_does_not_process_the_event_itself(
    replay_server: MCPServer,
    seeded_database: dict[str, int],
) -> None:
    record_id = seeded_database["processed"]

    async with Client(replay_server) as client:
        await client.call_tool(
            "replay_event",
            {"record_id": record_id, "reason": "send it again"},
        )
        detail = await client.call_tool("get_event", {"record_id": record_id})

    assert detail.structured_content["status"] == "received"
    assert detail.structured_content["replay_count"] == 1


@pytest.mark.anyio
async def test_replay_rejects_events_already_in_play(
    replay_server: MCPServer,
    seeded_database: dict[str, int],
    database_path: Path,
) -> None:
    received_id = seeded_database["received"]
    service = EventService(EventRepository(database_path))
    processing_id = service.record_delivery(
        "github", event("evt_in_flight")
    ).record_id
    service.start_processing(processing_id)

    async with Client(replay_server) as client:
        queued = await client.call_tool(
            "replay_event",
            {"record_id": received_id, "reason": "try again"},
        )
        in_flight = await client.call_tool(
            "replay_event",
            {"record_id": processing_id, "reason": "try again"},
        )

    assert queued.is_error is True
    assert "already queued for processing" in queued.content[0].text
    assert in_flight.is_error is True
    assert "currently being processed" in in_flight.content[0].text


@pytest.mark.anyio
async def test_replay_rejects_an_unknown_event(replay_server: MCPServer) -> None:
    async with Client(replay_server) as client:
        result = await client.call_tool(
            "replay_event",
            {"record_id": 999, "reason": "does not matter"},
        )

    assert result.is_error is True
    assert "event 999 does not exist" in result.content[0].text


@pytest.mark.anyio
async def test_replay_rejects_a_reason_that_is_too_short(
    replay_server: MCPServer, seeded_database: dict[str, int]
) -> None:
    async with Client(replay_server) as client:
        result = await client.call_tool(
            "replay_event",
            {"record_id": seeded_database["failed"], "reason": "no"},
        )

    assert result.is_error is True
    assert "at least 3 characters" in result.content[0].text


@pytest.mark.anyio
async def test_replay_token_never_reaches_the_client(
    replay_server: MCPServer, seeded_database: dict[str, int]
) -> None:
    async with Client(replay_server) as client:
        tools = await client.list_tools()
        result = await client.call_tool(
            "replay_event",
            {"record_id": seeded_database["failed"], "reason": "retry it"},
        )

    replay_tool = next(tool for tool in tools.tools if tool.name == "replay_event")
    assert set(replay_tool.input_schema["properties"]) == {"record_id", "reason"}

    exposed = json.dumps(
        {"tools": tools.model_dump(), "result": result.model_dump()},
        default=str,
    )
    assert "test-token" not in exposed
