"""Application rules for receiving and processing webhook events.

This layer is the shared entry point for every interface. The HTTP API uses it
now; the MCP server will use the same methods in a later stage instead of
reimplementing the rules.

It owns decisions, the repository owns SQL, and no transport concerns appear
here: nothing in this module knows about requests or responses.
"""

from dataclasses import dataclass

from app.lifecycle import (
    AttemptNotOpenError,
    AttemptOutcome,
    EventStatus,
    ensure_transition_allowed,
)
from app.repositories.events import EventRepository
from app.schemas.event import WebhookEvent


@dataclass(frozen=True)
class DeliveryResult:
    """What happened when one delivery was recorded."""

    record_id: int
    duplicate: bool


class EventService:
    def __init__(self, repository: EventRepository) -> None:
        self.repository = repository

    def record_delivery(self, source: str, event: WebhookEvent) -> DeliveryResult:
        """Store a delivered event, or report that it was already stored.

        A duplicate is a normal outcome, not an error. Senders retry on
        timeouts, so receiving the same event twice says nothing about a fault.
        Raising here would make ordinary retries look like system failures.
        """
        record_id, created = self.repository.insert_if_new(source, event)
        return DeliveryResult(record_id=record_id, duplicate=not created)

    def start_processing(self, record_id: int) -> int:
        """Refuse illegal statuses, then open a new attempt.

        Returns the attempt id. The engine that calls this arrives in the next
        stage; the rules and the record it leaves behind exist now.
        """
        stored = self.repository.get(record_id)
        if stored is None:
            raise LookupError(f"event {record_id} does not exist")

        ensure_transition_allowed(stored.status, EventStatus.PROCESSING)
        return self.repository.begin_attempt(record_id)

    def finish_processing(
        self,
        attempt_id: int,
        succeeded: bool,
        error: str | None = None,
    ) -> None:
        """Close an attempt and move the event to processed or failed."""
        outcome = (
            AttemptOutcome.SUCCEEDED if succeeded else AttemptOutcome.FAILED
        )
        status = EventStatus.PROCESSED if succeeded else EventStatus.FAILED

        completed = self.repository.complete_attempt(
            attempt_id,
            event_status=status,
            outcome=outcome,
            error=error,
        )
        if not completed:
            raise AttemptNotOpenError(
                f"attempt {attempt_id} is missing or already finished"
            )
