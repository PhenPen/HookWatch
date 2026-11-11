"""The webhook receiving endpoint."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.deps import get_event_service
from app.api.signatures import (
    SIGNATURE_HEADER,
    SignatureError,
    verify_signature,
)
from app.schemas.event import WebhookEvent
from app.services.events import EventService
from app.settings import Settings

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


def _rejected_signature() -> HTTPException:
    """Answer every signature failure the same way.

    A caller learns that the delivery was refused, not whether the source is
    configured, whether the timestamp was stale or whether the digest was
    wrong. The log keeps the specific reason for the operator.
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing webhook signature",
    )


async def require_signed_delivery(
    source: SourceName,
    request: Request,
) -> None:
    """Refuse a delivery that cannot be proven to come from its source.

    FastAPI may have read the body before this dependency runs, but Starlette
    caches the raw bytes, so ``await request.body()`` returns exactly what was
    sent. That matters: the signature covers raw bytes, never re-serialised
    JSON.

    With enforcement on, an unconfigured source is refused as well. The source
    name comes from an attacker-controlled path, so accepting an unlisted one
    would let a sender bypass signatures entirely by picking a new name.
    """
    config: Settings = request.app.state.config
    header = request.headers.get(SIGNATURE_HEADER)
    secret = config.webhook_secrets.get(source)

    if secret is None:
        if header is not None or config.require_signatures:
            logger.warning(
                "webhook signature rejected source=%s reason=no_secret",
                source,
            )
            raise _rejected_signature()
        return

    if header is None:
        if not config.require_signatures:
            logger.warning(
                "webhook accepted unsigned source=%s enforcement=off",
                source,
            )
            return
        logger.warning(
            "webhook signature rejected source=%s reason=missing",
            source,
        )
        raise _rejected_signature()

    try:
        verify_signature(
            secret,
            await request.body(),
            header,
            tolerance_seconds=config.signature_tolerance_seconds,
        )
    except SignatureError as error:
        logger.warning(
            "webhook signature rejected source=%s reason=%s",
            source,
            error.reason,
        )
        raise _rejected_signature() from error


@router.post(
    "/webhooks/{source}",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_signed_delivery)],
)
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
