"""The webhook receiving endpoint."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.deps import get_event_service
from app.schemas.event import WebhookEvent
from app.services.events import EventService

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
    service: Annotated[EventService, Depends(get_event_service)],
) -> JSONResponse:
    """Accept one webhook event and store it unless it is a redelivery.

    Validation happens before this function runs. FastAPI parses the body into
    a WebhookEvent and returns 422 without calling us if anything is wrong, so
    by this point the event is known to satisfy the contract.

    Two successful outcomes exist. A new event is stored and answered with 202.
    An event we already have is answered with 200 and status "duplicate": the
    delivery succeeded, nothing new was queued, and the sender should stop
    retrying.
    """
    result = service.record_delivery(source, event)

    if result.duplicate:
        logger.info(
            "webhook duplicate source=%s event_id=%s row=%s",
            source,
            event.event_id,
            result.record_id,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "duplicate",
                "source": source,
                "event_id": event.event_id,
            },
        )

    logger.info(
        "webhook accepted source=%s event_id=%s event_type=%s row=%s",
        source,
        event.event_id,
        event.event_type,
        result.record_id,
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "status": "accepted",
            "source": source,
            "event_id": event.event_id,
        },
    )


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
