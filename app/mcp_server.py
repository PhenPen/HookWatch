"""MCP server for inspecting and replaying stored webhook events.

The server is a second interface onto the same application services the HTTP
API uses. It opens the SQLite database directly and calls `EventService`, so
listing, filtering, history and replay rules are not reimplemented here. It
deliberately does not start the processing engine: the web process owns
processing, and this process only observes and re-queues what that engine
recorded.

Run it over stdio with:

    uv run python -m app.mcp_server

Four tools are always read-only. `replay_event` is registered only when the
process holds `HOOKWATCH_REPLAY_TOKEN`; the token is never a tool argument and
never leaves this process, so a client cannot see, supply or bypass it. The
tool is annotated as destructive so MCP clients that require approval for
write-capable tools keep asking the operator.
"""

import logging
import os
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, StringConstraints

from app.db import migrate
from app.lifecycle import EventStatus, ReplayNotAllowedError
from app.repositories.events import EventRepository
from app.schemas.inspection import (
    EventDetail,
    EventSummary,
    ProcessingHistory,
    ReplayResponse,
)
from app.services.events import EventHistory, EventService
from app.views import event_detail, event_summary, processing_history

DEFAULT_DATABASE_PATH = Path("hookwatch.db")

logger = logging.getLogger(__name__)

Source = Annotated[
    str | None,
    Field(max_length=64, description="Exact sending service, for example github."),
]
EventType = Annotated[
    str | None,
    Field(
        max_length=100,
        description="Exact provider event type, for example invoice.paid.",
    ),
]
StatusFilter = Annotated[
    EventStatus | None,
    Field(description="Lifecycle status to match."),
]
DeadLetter = Annotated[
    bool | None,
    Field(
        description="true returns dead letters, false returns events still in play."
    ),
]
Limit = Annotated[
    int,
    Field(ge=1, le=200, description="Maximum number of events to return."),
]
Offset = Annotated[int, Field(ge=0, description="Events to skip for paging.")]
RecordId = Annotated[
    int,
    Field(ge=1, description="HookWatch event id, as shown in listings and URLs."),
]
Reason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=3, max_length=500),
    Field(description="Why the event should be processed again."),
]

# Read tools never change state, so clients that trust annotations can run them
# without asking. openWorldHint is false because every tool is confined to the
# local database.
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

# Replay repeats a side effect, so clients that gate write-capable tools must
# ask a human first. idempotentHint is false because each call starts a new
# replay cycle rather than repeating the previous result.
REPLAY = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)


def _history(service: EventService, record_id: int) -> EventHistory:
    """Return one event history, or raise a tool error the caller can read."""
    try:
        return service.get_event_history(record_id)
    except LookupError as error:
        raise ToolError(str(error)) from error


def create_mcp_server(
    database_path: Path | None = None,
    *,
    replay_token: str | None = None,
    operator: str | None = None,
) -> MCPServer:
    """Build the MCP server against a database file.

    The path is a parameter so tests can use a temporary database and never
    touch the development one. The environment variable is read here, not at
    import time, which keeps the module import side effect free apart from the
    migration that ensures the expected schema exists.

    replay_token is the shared secret that enables replay. None reads
    HOOKWATCH_REPLAY_TOKEN, and an empty string disables replay explicitly,
    mirroring create_app so tests can prove the closed default even on a
    machine where the variable is set. The token is never a tool argument.

    operator is recorded as requested_by in the replay audit trail. None reads
    HOOKWATCH_MCP_OPERATOR and falls back to mcp-agent. It names the configured
    process, not a verified person.
    """
    path = database_path or Path(
        os.environ.get("HOOKWATCH_DB_PATH", str(DEFAULT_DATABASE_PATH))
    )
    token = (
        replay_token
        if replay_token is not None
        else os.environ.get("HOOKWATCH_REPLAY_TOKEN")
    )
    actor = (
        operator
        if operator is not None
        else os.environ.get("HOOKWATCH_MCP_OPERATOR", "mcp-agent")
    ).strip() or "mcp-agent"

    applied = migrate(path)
    if applied:
        logger.info("applied %s database migration(s) to %s", applied, path)

    service = EventService(EventRepository(path))

    server = MCPServer(
        name="hookwatch",
        instructions=(
            "Inspect stored webhook events and their processing history. Call "
            "list_events to browse, search_failed_events to start from "
            "failures, get_event for the payload and full history, and "
            "inspect_processing_history when only the attempts and replays "
            "matter. When a replay tool is available, it queues a failed or "
            "processed event for another cycle; it does not process anything."
        ),
    )

    @server.tool(annotations=READ_ONLY)
    def list_events(
        source: Source = None,
        event_type: EventType = None,
        status: StatusFilter = None,
        dead_lettered: DeadLetter = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> list[EventSummary]:
        """List stored events newest first, with optional exact-match filters.

        Filters combine with AND. Payloads are omitted to keep the response
        small; call get_event for the data and the full history.
        """
        events = service.list_events(
            source=source,
            event_type=event_type,
            status=status,
            dead_lettered=dead_lettered,
            limit=limit,
            offset=offset,
        )
        return [event_summary(event) for event in events]

    @server.tool(annotations=READ_ONLY)
    def get_event(record_id: RecordId) -> EventDetail:
        """Return one event with its payload, attempts and replay history.

        This is the failure investigation view: each attempt shows its number,
        replay cycle, outcome, failure kind and stored error.
        """
        return event_detail(_history(service, record_id))

    @server.tool(annotations=READ_ONLY)
    def search_failed_events(
        source: Source = None,
        event_type: EventType = None,
        dead_lettered: DeadLetter = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> list[EventSummary]:
        """List failed events, newest first.

        Set dead_lettered to true to see only events the engine has given up
        on, or false to see failures that are still eligible for a retry.
        """
        events = service.list_events(
            source=source,
            event_type=event_type,
            status=EventStatus.FAILED,
            dead_lettered=dead_lettered,
            limit=limit,
            offset=offset,
        )
        return [event_summary(event) for event in events]

    @server.tool(annotations=READ_ONLY)
    def inspect_processing_history(record_id: RecordId) -> ProcessingHistory:
        """Return the attempts and replay audit trail for one event.

        The event payload is omitted because this tool answers "what happened
        to it?", not "what did the sender send?".
        """
        return processing_history(_history(service, record_id))

    if token:

        @server.tool(annotations=REPLAY)
        def replay_event(record_id: RecordId, reason: Reason) -> ReplayResponse:
            """Queue a failed or processed event for another processing cycle.

            A received event is already queued and a processing event is in
            flight, so both are refused. Nothing is processed here: the event
            returns to received and the web process's engine picks it up, just
            like a fresh delivery. Older attempts are kept and the audit trail
            records this process and the reason.
            """
            try:
                replay = service.replay_event(
                    record_id,
                    reason=reason,
                    requested_by=actor,
                )
            except (LookupError, ReplayNotAllowedError) as error:
                raise ToolError(str(error)) from error

            return ReplayResponse(
                id=record_id,
                replay_number=replay.replay_number,
            )

    @server.resource(
        "hookwatch://events/{record_id}",
        name="event",
        title="Stored webhook event",
        description="One event with its payload, attempts and replay history.",
        mime_type="application/json",
    )
    def read_event(record_id: RecordId) -> EventDetail:
        """Read one stored event by id, the same view get_event returns."""
        try:
            return event_detail(_history(service, record_id))
        except ToolError as error:
            raise ResourceError(str(error)) from error

    return server


def main() -> None:
    """Run the server over stdio, which is what MCP clients launch."""
    logging.basicConfig(level=logging.INFO)
    create_mcp_server().run("stdio")


if __name__ == "__main__":
    main()
