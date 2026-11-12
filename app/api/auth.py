"""Bearer API key authentication and role checks for HTTP routes.

Every request presents an API key, and the key resolves to one row in the
principals table. The name on that row is a verified identity: it is what the
replay audit trail records, unlike a claimed header that any caller could set.

Authentication failure is always 401, whether the header is missing, malformed,
unknown or revoked. An authenticated principal without the required role gets
403. Keeping those two apart tells a legitimate viewer what to ask for without
telling an anonymous caller whether a key would have been enough.
"""

import logging
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.api.limits import enforce_rate_limit
from app.db import Principal, Role

logger = logging.getLogger(__name__)


def bearer_token(request: Request) -> str | None:
    """Return the token from ``Authorization: Bearer <token>``, or None.

    Exactly one space is expected, and the scheme is case-insensitive because
    HTTP authentication schemes are. The token is never logged.
    """
    authorization = request.headers.get("Authorization")
    if not authorization:
        return None

    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None

    token = token.strip()
    return token or None


def get_principal(request: Request) -> Principal:
    """Resolve the caller to an active principal, or refuse the request."""
    token = bearer_token(request)
    principal = request.app.state.keys.authenticate(token) if token else None

    if principal is None:
        logger.warning(
            "api request denied path=%s reason=invalid_or_missing_key",
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Record the verified name for the access log. It comes from the key row,
    # not from a header, so the line cannot be made to claim another principal.
    # Set before the limit so a refused read still says who asked.
    request.state.principal = principal.name

    # Limit after authentication, so a missing or invalid key is still a 401
    # rather than a 429 and cannot create a bucket. Keying by the row id rather
    # than the name means a revoked key that is reissued under the same name
    # starts with a fresh allowance.
    enforce_rate_limit(
        request.app.state.read_limits,
        f"principal:{principal.id}",
    )

    return principal


def require_viewer(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    """Allow any authenticated principal.

    Viewer is the lowest role, so this check only proves the caller holds a
    valid key. It exists as a named dependency so a route can state what it
    needs rather than repeat the role comparison.
    """
    return principal


def require_operator(
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    """Allow replay only to an operator, which includes viewers."""
    if not principal.role.covers(Role.OPERATOR):
        logger.warning(
            "api request denied path=%s principal=%s reason=operator_required",
            request.url.path,
            principal.name,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="operator role required",
        )
    return principal
