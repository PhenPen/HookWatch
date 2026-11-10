"""Read models for the monitoring and replay endpoints.

These are response contracts, not database rows and not request payloads. They
exist so the HTTP API documents exactly what it returns and so the shape can be
reused by the MCP layer later without copying dictionaries around.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.lifecycle import AttemptOutcome, EventStatus, FailureKind


class AttemptView(BaseModel):
    """One processing attempt as shown to an operator."""

    attempt_number: int
    replay_number: int
    started_at: datetime
    finished_at: datetime | None
    outcome: AttemptOutcome | None
    failure_kind: FailureKind | None
    error: str | None


class ReplayView(BaseModel):
    """One recorded replay request."""

    replay_number: int
    requested_at: datetime
    requested_by: str
    reason: str


class EventSummary(BaseModel):
    """The fields shown for every event in a listing.

    The payload is omitted here because listings can contain many events and
    the data belongs to the sender. It is returned by the detail endpoint.
    """

    id: int
    source: str
    event_id: str
    event_type: str
    occurred_at: datetime
    received_at: datetime
    status: EventStatus
    dead_lettered_at: datetime | None
    replay_count: int


class EventDetail(EventSummary):
    """One event with its payload, attempts and replay audit trail."""

    data: dict[str, Any]
    attempts: list[AttemptView]
    replays: list[ReplayView]


class ReplayRequest(BaseModel):
    """Why an operator wants an event processed again.

    A reason is required because replay is an audited action. "It failed and I
    fixed the cause" is enough; an empty body is not.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: Annotated[str, StringConstraints(min_length=3, max_length=500)]


class ReplayResponse(BaseModel):
    """Acknowledgement that an event was queued for another cycle."""

    status: Literal["queued"] = "queued"
    id: int
    replay_number: int
