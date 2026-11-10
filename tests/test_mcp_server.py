import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError

from app.db import migrate
from app.lifecycle import FailureKind
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
    return create_mcp_server(database_path)


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
