"""Read-side views shared by the HTTP API and the MCP server.

The database row models are not response contracts. Converting them lives here
once, so both interfaces return the same shape and neither has to know how the
other presents an event. Nothing in this module decides anything: it only
copies stored fields into the Pydantic models clients see.
"""

from app.db import Attempt, Replay, StoredEvent
from app.schemas.inspection import (
    AttemptView,
    EventDetail,
    EventSummary,
    ProcessingHistory,
    ReplayView,
)
from app.services.events import EventHistory


def attempt_view(attempt: Attempt) -> AttemptView:
    """One stored attempt as shown to an operator."""
    return AttemptView(
        attempt_number=attempt.attempt_number,
        replay_number=attempt.replay_number,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
        outcome=attempt.outcome,
        failure_kind=attempt.failure_kind,
        error=attempt.error,
    )


def replay_view(replay: Replay) -> ReplayView:
    """One stored replay request as shown to an operator."""
    return ReplayView(
        replay_number=replay.replay_number,
        requested_at=replay.requested_at,
        requested_by=replay.requested_by,
        reason=replay.reason,
    )


def event_summary(event: StoredEvent) -> EventSummary:
    """The compact form used in listings, without the payload."""
    return EventSummary(
        id=event.id,
        source=event.source,
        event_id=event.event_id,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        received_at=event.received_at,
        status=event.status,
        dead_lettered_at=event.dead_lettered_at,
        replay_count=event.replay_count,
    )


def event_detail(history: EventHistory) -> EventDetail:
    """One event with its payload, attempts and replay audit trail."""
    return EventDetail(
        **event_summary(history.event).model_dump(),
        data=history.event.data,
        attempts=[attempt_view(attempt) for attempt in history.attempts],
        replays=[replay_view(replay) for replay in history.replays],
    )


def processing_history(history: EventHistory) -> ProcessingHistory:
    """Attempts and replays without repeating the payload.

    Useful when the caller already has the event and only wants to know what
    happened to it.
    """
    return ProcessingHistory(
        id=history.event.id,
        source=history.event.source,
        event_id=history.event.event_id,
        status=history.event.status,
        replay_count=history.event.replay_count,
        attempts=[attempt_view(attempt) for attempt in history.attempts],
        replays=[replay_view(replay) for replay in history.replays],
    )
