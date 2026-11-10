"""Event inspection and controlled replay over HTTP.

Listing and detail routes are read-only. The replay route is the only one that
changes an event's status, and it goes through EventService like every other
interface so the MCP server reuses the same rules.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from app.api.deps import get_event_service
from app.api.replay_auth import require_replay_token
from app.lifecycle import EventStatus, ReplayNotAllowedError
from app.schemas.inspection import (
    EventDetail,
    EventSummary,
    ReplayRequest,
    ReplayResponse,
)
from app.services.events import EventService
from app.views import event_detail, event_summary

router = APIRouter(prefix="/events", tags=["events"])

# Filters are exact matches. Free-text search is deliberately absent: the
# payload belongs to the sender and may be large, so scanning it is a different
# feature with different costs.
SourceFilter = Annotated[
    str | None,
    Query(max_length=64, description="Exact sending service, for example github."),
]
EventTypeFilter = Annotated[
    str | None,
    Query(max_length=100, description="Exact provider event type."),
]
StatusFilter = Annotated[
    EventStatus | None,
    Query(alias="status", description="Lifecycle status to match."),
]
DeadLetterFilter = Annotated[
    bool | None,
    Query(
        alias="dead_lettered",
        description="true returns dead letters, false returns live events.",
    ),
]
Limit = Annotated[int, Query(ge=1, le=200, description="Maximum rows to return.")]
Offset = Annotated[int, Query(ge=0, description="Rows to skip for paging.")]
Operator = Annotated[
    str | None,
    Header(
        alias="X-Operator",
        max_length=100,
        description="Optional operator name recorded in the replay audit trail.",
    ),
]


@router.get("", response_model=list[EventSummary])
def list_events(
    service: Annotated[EventService, Depends(get_event_service)],
    source: SourceFilter = None,
    event_type: EventTypeFilter = None,
    status_filter: StatusFilter = None,
    dead_lettered: DeadLetterFilter = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[EventSummary]:
    """Return stored events, newest first, with optional filters.

    Filters combine with AND. The listing omits payloads; fetch one event for
    the full data plus its attempt and replay history.
    """
    events = service.list_events(
        source=source,
        event_type=event_type,
        status=status_filter,
        dead_lettered=dead_lettered,
        limit=limit,
        offset=offset,
    )
    return [event_summary(event) for event in events]


@router.get("/{record_id}", response_model=EventDetail)
def get_event(
    record_id: int,
    service: Annotated[EventService, Depends(get_event_service)],
) -> EventDetail:
    """Return one event with its payload, attempts and replay audit trail.

    This is the failure inspection view: the attempts show which try failed,
    whether the failure was retryable or permanent, and the stored error.
    """
    try:
        history = service.get_event_history(record_id)
    except LookupError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error

    return event_detail(history)


@router.post(
    "/{record_id}/replay",
    response_model=ReplayResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_replay_token)],
)
def replay_event(
    record_id: int,
    request: ReplayRequest,
    service: Annotated[EventService, Depends(get_event_service)],
    operator: Operator = None,
) -> ReplayResponse:
    """Queue an existing event for another processing cycle.

    Replay answers 202 because the engine does the work later, exactly like a
    fresh delivery. It does not copy the event, so the idempotency index still
    has one row for this source and event id.

    Only failed and processed events are replayable. A received event is
    already queued, and a processing event is in flight; both answer 409.
    """
    requested_by = (operator or "").strip() or "unspecified"

    try:
        replay = service.replay_event(
            record_id,
            reason=request.reason,
            requested_by=requested_by,
        )
    except LookupError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except ReplayNotAllowedError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    return ReplayResponse(id=record_id, replay_number=replay.replay_number)
