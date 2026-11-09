"""Event lifecycle: the statuses an event can have and the moves allowed.

These rules are transport independent. The HTTP API, and later the MCP layer,
both go through the same service, so the rules must not live in a route.
"""

from enum import StrEnum


class EventStatus(StrEnum):
    """Where an event is in its processing lifecycle."""

    RECEIVED = "received"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class AttemptOutcome(StrEnum):
    """How a single processing attempt ended."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class FailureKind(StrEnum):
    """Whether a failed attempt is worth retrying."""

    RETRYABLE = "retryable"
    PERMANENT = "permanent"


# The only status changes allowed. Anything absent from this table is refused,
# which is what stops an event from skipping stages or restarting after success.
ALLOWED_TRANSITIONS: dict[EventStatus, set[EventStatus]] = {
    EventStatus.RECEIVED: {EventStatus.PROCESSING},
    EventStatus.PROCESSING: {EventStatus.PROCESSED, EventStatus.FAILED},
    # A retry after failure opens a new attempt. Retrying is a decision made by
    # the processing engine, not a free pass to reopen any other status.
    EventStatus.FAILED: {EventStatus.PROCESSING},
    # Processed is terminal. Replay, when it arrives, copies the event into a
    # new attempt through its own explicit path.
    EventStatus.PROCESSED: set(),
}


class InvalidTransitionError(Exception):
    """Raised when an event status change is not allowed."""


class AttemptNotOpenError(Exception):
    """Raised when finishing an attempt that is missing or already finished."""


class RetryableProcessingError(Exception):
    """A temporary failure. The event should be tried again later."""


class PermanentProcessingError(Exception):
    """A failure that will not fix itself. The event should not be retried."""


def ensure_transition_allowed(current: EventStatus, target: EventStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(
            f"cannot move event from {current.value} to {target.value}"
        )
