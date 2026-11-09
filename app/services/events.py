"""Application rules for receiving and processing webhook events.

This layer is the shared entry point for every interface. The HTTP API uses it
now; the MCP server will use the same methods in a later stage instead of
reimplementing the rules.

It owns decisions, the repository owns SQL, and no transport concerns appear
here: nothing in this module knows about requests or responses.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.db import Attempt, StoredEvent
from app.lifecycle import (
    AttemptNotOpenError,
    AttemptOutcome,
    EventStatus,
    FailureKind,
    InvalidTransitionError,
    PermanentProcessingError,
    RetryableProcessingError,
    ensure_transition_allowed,
)
from app.repositories.events import EventRepository
from app.schemas.event import WebhookEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessingSettings:
    """Tunable processing behaviour.

    Defaults are deliberately small and local. They are code, not environment
    configuration, because production configuration is a later stage.
    """

    poll_interval_seconds: float = 1.0
    batch_size: int = 10
    max_attempts: int = 5
    backoff_base_seconds: float = 2.0
    backoff_cap_seconds: float = 60.0
    stale_after_seconds: float = 300.0

    def retry_delay(self, attempts_made: int) -> float:
        """Wait before the next attempt after this many failures.

        The first failure waits the base delay, then each further failure
        doubles the wait until the cap is reached.
        """
        if attempts_made < 1:
            return 0.0
        delay = self.backoff_base_seconds * (2 ** (attempts_made - 1))
        return min(delay, self.backoff_cap_seconds)


# The processor performs the side effect for one event. The engine does not
# know what that side effect is, which keeps this stage independent of any real
# third-party integration.
Processor = Callable[[StoredEvent], None]


def _log_only(event: StoredEvent) -> None:
    """Default processor: record that the event was handled and succeed."""
    logger.info(
        "processed event id=%s source=%s event_id=%s event_type=%s",
        event.id,
        event.source,
        event.event_id,
        event.event_type,
    )


@dataclass(frozen=True)
class DeliveryResult:
    """What happened when one delivery was recorded."""

    record_id: int
    duplicate: bool


class EventService:
    def __init__(
        self,
        repository: EventRepository,
        processor: Processor | None = None,
        settings: ProcessingSettings | None = None,
    ) -> None:
        self.repository = repository
        self.processor = processor or _log_only
        self.settings = settings or ProcessingSettings()

    def record_delivery(self, source: str, event: WebhookEvent) -> DeliveryResult:
        """Store a delivered event, or report that it was already stored.

        A duplicate is a normal outcome, not an error. Senders retry on
        timeouts, so receiving the same event twice says nothing about a fault.
        Raising here would make ordinary retries look like system failures.
        """
        record_id, created = self.repository.insert_if_new(source, event)
        return DeliveryResult(record_id=record_id, duplicate=not created)

    def start_processing(self, record_id: int) -> int:
        """Refuse illegal statuses, then claim the event and open an attempt.

        Returns the attempt id. The engine normally claims through process_due;
        this method remains the single-event entry point used by tests and by
        any later manual operation.
        """
        stored = self.repository.get(record_id)
        if stored is None:
            raise LookupError(f"event {record_id} does not exist")
        if stored.dead_lettered_at is not None:
            raise InvalidTransitionError(f"event {record_id} is dead-lettered")

        ensure_transition_allowed(stored.status, EventStatus.PROCESSING)

        attempts = self.repository.list_attempts(record_id)
        if (
            stored.status is EventStatus.FAILED
            and len(attempts) >= self.settings.max_attempts
        ):
            raise InvalidTransitionError(
                f"event {record_id} has exhausted its attempts"
            )

        attempt_id = self.repository.claim(record_id)
        if attempt_id is None:
            raise InvalidTransitionError(
                f"event {record_id} was claimed by another worker"
            )
        return attempt_id

    def finish_processing(
        self,
        attempt_id: int,
        succeeded: bool,
        error: str | None = None,
        failure_kind: FailureKind | None = None,
        dead_letter: bool = False,
        finished_at: datetime | None = None,
    ) -> None:
        """Close an attempt and move the event to processed or failed.

        A failure defaults to retryable. Permanent failures and exhausted
        retries are dead-lettered in the same database transaction.
        """
        if succeeded:
            outcome = AttemptOutcome.SUCCEEDED
            status = EventStatus.PROCESSED
            kind = None
            dead_letter = False
        else:
            outcome = AttemptOutcome.FAILED
            status = EventStatus.FAILED
            kind = failure_kind or FailureKind.RETRYABLE

        completed = self.repository.complete_attempt(
            attempt_id,
            event_status=status,
            outcome=outcome,
            error=error,
            failure_kind=kind,
            dead_letter=dead_letter,
            finished_at=finished_at,
        )
        if not completed:
            raise AttemptNotOpenError(
                f"attempt {attempt_id} is missing or already finished"
            )

    def process_due(self, now: datetime | None = None) -> int:
        """Recover abandoned work, then process one batch of due events.

        Returns how many events were handled. The engine sleeps only when this
        is zero, so a busy batch is followed immediately by another batch.

        The batch size bounds one call. Because claiming is atomic, two workers
        can call this at the same time without processing the same event twice.
        """
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self.recover_abandoned(current)

        handled = 0

        for event in self.repository.list_pending(limit=self.settings.batch_size):
            attempts = self.repository.list_attempts(event.id)

            if event.status is EventStatus.FAILED:
                if len(attempts) >= self.settings.max_attempts:
                    if self.repository.mark_dead_letter(event.id, at=current):
                        logger.warning(
                            "event dead-lettered after %s attempts id=%s source=%s event_id=%s",
                            len(attempts),
                            event.id,
                            event.source,
                            event.event_id,
                        )
                        handled += 1
                    continue

                if not self._retry_due(attempts, current):
                    continue

            attempt_id = self.repository.claim(event.id, started_at=current)
            if attempt_id is None:
                # Another worker claimed it between the list and this update.
                continue

            attempt = self.repository.get_attempt(attempt_id)
            attempt_number = attempt.attempt_number if attempt is not None else 1

            handled += 1
            self._run_attempt(event, attempt_id, attempt_number, current)

        return handled

    def recover_abandoned(self, now: datetime | None = None) -> int:
        """Close attempts whose worker disappeared before finishing."""
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = current - timedelta(seconds=self.settings.stale_after_seconds)
        recovered = self.repository.recover_abandoned(
            cutoff,
            recovered_at=current,
        )
        if recovered:
            logger.warning("recovered %s abandoned processing attempt(s)", recovered)
        return recovered

    def _retry_due(self, attempts: list[Attempt], now: datetime) -> bool:
        """Whether a failed event has waited long enough for another try."""
        if not attempts:
            return True

        last = attempts[-1]
        if last.finished_at is None:
            # The attempt is still open. Recovery owns this case.
            return False

        delay = self.settings.retry_delay(len(attempts))
        elapsed = (now - last.finished_at).total_seconds()
        return elapsed >= delay

    def _run_attempt(
        self,
        event: StoredEvent,
        attempt_id: int,
        attempt_number: int,
        now: datetime,
    ) -> None:
        """Run the processor once and record what happened."""
        exhausted = attempt_number >= self.settings.max_attempts

        try:
            self.processor(event)
        except PermanentProcessingError as error:
            self._record_failure(
                event,
                attempt_id,
                now,
                message=str(error) or type(error).__name__,
                kind=FailureKind.PERMANENT,
                dead_letter=True,
            )
        except RetryableProcessingError as error:
            self._record_failure(
                event,
                attempt_id,
                now,
                message=str(error) or type(error).__name__,
                kind=FailureKind.RETRYABLE,
                dead_letter=exhausted,
            )
        except Exception as error:
            # An unexpected bug is safer to retry than to dead-letter on the
            # first occurrence. The attempt limit still stops a poison event.
            logger.exception(
                "unexpected processing error id=%s source=%s event_id=%s",
                event.id,
                event.source,
                event.event_id,
            )
            self._record_failure(
                event,
                attempt_id,
                now,
                message=f"{type(error).__name__}: {error}",
                kind=FailureKind.RETRYABLE,
                dead_letter=exhausted,
            )
        else:
            self.finish_processing(attempt_id, succeeded=True, finished_at=now)
            logger.info(
                "event processed id=%s source=%s event_id=%s attempt=%s",
                event.id,
                event.source,
                event.event_id,
                attempt_number,
            )

    def _record_failure(
        self,
        event: StoredEvent,
        attempt_id: int,
        now: datetime,
        message: str,
        kind: FailureKind,
        dead_letter: bool,
    ) -> None:
        self.finish_processing(
            attempt_id,
            succeeded=False,
            error=message,
            failure_kind=kind,
            dead_letter=dead_letter,
            finished_at=now,
        )
        if dead_letter:
            logger.warning(
                "event dead-lettered id=%s source=%s event_id=%s reason=%s",
                event.id,
                event.source,
                event.event_id,
                message,
            )
        else:
            logger.warning(
                "event attempt failed id=%s source=%s event_id=%s kind=%s reason=%s",
                event.id,
                event.source,
                event.event_id,
                kind.value,
                message,
            )
