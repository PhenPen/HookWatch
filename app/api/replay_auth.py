"""Authorization for replay, the one operation that repeats a side effect.

Receiving and inspecting events is open in this stage. Replay is different: it
asks the processor to run again, which may call a third party a second time.
That is why replay is disabled unless a token is configured, and why the check
lives in its own dependency instead of inside the route body.
"""

import hmac
import logging
from typing import Annotated

from fastapi import Header, HTTPException, Request, status

logger = logging.getLogger(__name__)


def require_replay_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Refuse replay unless the configured bearer token is supplied.

    When ``HOOKWATCH_REPLAY_TOKEN`` is unset, replay is unavailable rather than
    unprotected. The default has to be closed, because forgetting a variable
    must not expose an operation that calls external services.

    A shared token proves knowledge of the operator secret, not who the caller
    is. The optional ``X-Operator`` header is recorded for the audit trail but
    is not trusted or verified. Per-user authentication belongs to a later
    stage.
    """
    configured = getattr(request.app.state, "replay_token", None)
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "event replay is disabled: set HOOKWATCH_REPLAY_TOKEN to enable it"
            ),
        )

    scheme, _, supplied = (authorization or "").partition(" ")
    valid = (
        scheme.lower() == "bearer"
        and bool(supplied)
        and hmac.compare_digest(
            supplied.encode("utf-8"),
            configured.encode("utf-8"),
        )
    )
    if not valid:
        logger.warning(
            "replay denied path=%s reason=invalid_or_missing_token",
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing replay token",
            headers={"WWW-Authenticate": "Bearer"},
        )
