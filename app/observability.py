"""Request IDs, access logging and log formatting for the HTTP edge.

A webhook receiver is hard to debug without correlation: a sender reports a
delivery, the operator sees a stored row, and the log has several lines about
the same request with nothing tying them together. Every HTTP request now gets
an ID, the ID is echoed to the caller, and every log line written while that
request is handled carries it.

The ID travels in a context variable rather than as an argument through every
function. A logging filter copies it onto each record, and a formatter appends
it to the line. Outside a request, such as the engine loop, the field is ``-``
so the format stays uniform and a log parser does not need a special case.

An inbound ``X-Request-ID`` is accepted only after a strict character and length
check. It is a correlation token, not an identity: a caller may choose its own
value and thereby group its lines, but it cannot claim a principal or a source.
Rejecting anything outside the pattern also means a header cannot inject extra
text into a log line.

The access line deliberately contains no query string, body, header, API key,
signature, replay reason or client IP. The path is enough to identify the route,
and the verified principal or source is added from request state, never from
anything the caller sent.
"""

import logging
import re
import time
import uuid
from contextvars import ContextVar

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
ACCESS_LOGGER = "app.access"
DEFAULT_LOG_FORMAT = "%(levelname)s:%(name)s:%(message)s"
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")

# A request that ends before any response was started has no HTTP status. 499 is
# the conventional "client closed request" value and keeps the access line
# honest instead of reporting a server error that did not happen.
CLIENT_CLOSED_REQUEST = 499

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def current_request_id() -> str | None:
    """Return the ID of the request being handled, or None outside one."""
    return _request_id.get()


class RequestIdFilter(logging.Filter):
    """Copy the current request ID onto every log record.

    The value is ``-`` outside a request, so every line carries the field and a
    log parser does not have to treat engine lines differently.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id() or "-"
        return True


class RequestIdFormatter(logging.Formatter):
    """Append ``request_id`` to the standard application log line."""

    def __init__(self, fmt: str = DEFAULT_LOG_FORMAT) -> None:
        super().__init__(f"{fmt} request_id=%(request_id)s")

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return super().format(record)


def configure_logging(level: str) -> None:
    """Install the application log format, including the request ID.

    uvicorn configures only its own loggers, so without this the application's
    INFO messages would be dropped silently. ``basicConfig`` does nothing when
    the root logger already has handlers, which is what lets a host or the test
    runner keep its own configuration; the application format takes over only in
    the plain uvicorn process.
    """
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(RequestIdFormatter())
    logging.basicConfig(level=level, handlers=[handler])


class RequestIdMiddleware:
    """Assign a request ID, echo it, and write one access line per request.

    The middleware sits outside the body limit, so a request refused with 413
    still gets an ID and an access line. It is still inside Starlette's error
    middleware, so an exception that escapes the application is logged here as
    500 and then answered by the outer handler; that response may not carry the
    header, but the log line carries the ID.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        token = _request_id.set(request_id)
        started = time.perf_counter()
        status_code: int | None = None
        failed = False

        async def send_with_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            failed = True
            raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000.0
            try:
                _log_access(scope, state, status_code, failed, duration_ms)
            finally:
                _request_id.reset(token)


def _incoming_request_id(scope: Scope) -> str | None:
    """Return a caller-supplied request ID only when it is safe to log."""
    for name, value in scope.get("headers", []):
        if name.lower() != b"x-request-id":
            continue
        candidate = value.decode("latin-1")
        return candidate if REQUEST_ID_PATTERN.fullmatch(candidate) else None
    return None


def _log_access(
    scope: Scope,
    state: dict,
    status_code: int | None,
    failed: bool,
    duration_ms: float,
) -> None:
    """Write the one line that describes how the request ended."""
    if status_code is None:
        status = 500 if failed else CLIENT_CLOSED_REQUEST
    else:
        status = status_code

    fields = [
        f"method={scope.get('method', '-')}",
        f"path={scope.get('path', '-')}",
        f"status={status}",
        f"duration_ms={duration_ms:.1f}",
    ]
    principal = state.get("principal")
    if principal is not None:
        fields.append(f"principal={principal}")
    source = state.get("source")
    if source is not None:
        fields.append(f"source={source}")

    level = logging.DEBUG if scope.get("path") == "/health" else logging.INFO
    logging.getLogger(ACCESS_LOGGER).log(level, "request %s", " ".join(fields))
