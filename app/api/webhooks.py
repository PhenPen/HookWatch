"""The webhook receiving endpoint."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError

from app.api.deps import get_event_repository
from app.repositories.events import EventRepository
from app.schemas.event import WebhookEvent

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

# The source identifies the sending service, for example "github" or "stripe".
# It is constrained because it comes from an untrusted URL and will later be
# written to logs, stored in the database and used to select per-source secrets.
SourceName = Annotated[
    str,
    Path(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
        description="Name of the sending service, for example github or stripe.",
    ),
]


@router.post("/webhooks/{source}", status_code=status.HTTP_202_ACCEPTED)
def receive_webhook(
    source: SourceName,
    event: WebhookEvent,
    repository: Annotated[EventRepository, Depends(get_event_repository)],
) -> dict[str, str]:
    """Accept one webhook event and store it.

    Validation happens before this function runs. FastAPI parses the body into
    a WebhookEvent and returns 422 without calling us if anything is wrong, so
    by this point the event is known to satisfy the contract.

    The event is written to the database before the response is sent, so a 202
    means "stored", not merely "received". Processing still does not exist;
    that arrives in a later stage.
    """
    record_id = repository.add(source, event)
    logger.info(
        "webhook accepted source=%s event_id=%s event_type=%s row=%s",
        source,
        event.event_id,
        event.event_type,
        record_id,
    )
    return {"status": "accepted", "source": source, "event_id": event.event_id}


async def log_validation_error(request: Request, exc: RequestValidationError) -> Response:
    """Log why a request was rejected, then return FastAPI's standard response.

    The response body is unchanged; this handler exists so a rejected delivery
    leaves a log entry naming the path and the fields that failed. Without it,
    the access log shows a bare 422 with no explanation.
    """
    logger.warning(
        "request rejected path=%s fields=%s",
        request.url.path,
        [error["loc"] for error in exc.errors()],
    )
    return await request_validation_exception_handler(request, exc)
