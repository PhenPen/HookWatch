"""HTTP abuse limits: request body size and per-caller rate.

Authentication decides who may call. These limits decide how much one caller
may consume, and they live at the HTTP edge because they protect the process
before a request reaches a route, a service or the database.

Two independent controls:

- ``BodySizeLimitMiddleware`` caps the bytes a request may send, so a single
  payload cannot exhaust memory or be parsed at all.
- ``RateLimiter`` is a token bucket used by the auth and webhook dependencies,
  so one principal or one source cannot monopolise the process.

Both are deliberately in-process. A shared limiter would need another service
to hold the counters; the deployment runs one web process, and the README says
so. The limit values are configuration, while the algorithms and response
shapes are fixed here, the same split the signature code uses.
"""

import logging
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import HTTPException, status
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Buckets are cheap, but a caller can choose an arbitrary webhook source name.
# The map is capped so traffic cannot grow it without bound, evicting the least
# recently used bucket first. Eviction resets that caller's budget, a deliberate
# trade of strict accounting for bounded memory.
MAX_BUCKETS = 4096

# A denial is logged at most once per bucket per interval, so a flood of
# rejected requests cannot become a flood of log lines.
DENIAL_LOG_INTERVAL_SECONDS = 1.0


@dataclass
class _Bucket:
    """One caller's remaining allowance and when it was last refilled."""

    tokens: float
    updated_at: float
    denied_logged_at: float | None = None


class RateLimiter:
    """A token bucket per key, refilled continuously.

    ``per_minute`` is both the bucket capacity and the refill rate: a caller may
    spend a full minute's allowance at once and then waits for tokens to return.
    Zero disables the limiter entirely.

    The clock is injectable so tests can advance time without sleeping. The lock
    is required because synchronous FastAPI dependencies run in a thread pool
    while asynchronous ones run on the event loop, and both share this object.
    """

    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_buckets: int = MAX_BUCKETS,
    ) -> None:
        self.per_minute = per_minute
        self._clock = clock
        self._max_buckets = max_buckets
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, key: str) -> float | None:
        """Return None when the request may proceed, or seconds to wait.

        A caller that is out of tokens is refused, not queued. Waiting inside
        the request would hold a connection and a worker, which is the resource
        the limit exists to protect.
        """
        if self.per_minute <= 0:
            return None

        now = self._clock()
        retry_after: float | None = None
        should_log = False

        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(self.per_minute), updated_at=now)
                self._buckets[key] = bucket
                self._evict()
            else:
                elapsed = max(0.0, now - bucket.updated_at)
                bucket.tokens = min(
                    float(self.per_minute),
                    bucket.tokens + elapsed * self.per_minute / 60.0,
                )
                bucket.updated_at = now
                self._buckets.move_to_end(key)

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
            else:
                retry_after = (1.0 - bucket.tokens) * 60.0 / self.per_minute
                if (
                    bucket.denied_logged_at is None
                    or now - bucket.denied_logged_at >= DENIAL_LOG_INTERVAL_SECONDS
                ):
                    bucket.denied_logged_at = now
                    should_log = True

        if should_log and retry_after is not None:
            logger.warning(
                "rate limit exceeded key=%s retry_after=%.1fs",
                key,
                retry_after,
            )

        return retry_after

    @property
    def bucket_count(self) -> int:
        """How many callers currently have a bucket, for tests and diagnostics."""
        with self._lock:
            return len(self._buckets)

    def _evict(self) -> None:
        """Drop least recently used buckets while the map is over its cap."""
        while len(self._buckets) > self._max_buckets:
            self._buckets.popitem(last=False)


def enforce_rate_limit(limiter: RateLimiter, key: str) -> None:
    """Refuse the request when its bucket is empty.

    The 429 is raised here rather than in each route so every limited surface
    answers with the same status, detail and ``Retry-After`` header.
    """
    retry_after = limiter.check(key)
    if retry_after is None:
        return

    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="rate limit exceeded",
        headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
    )


class BodySizeLimitMiddleware:
    """Reject an HTTP request whose body is larger than ``max_bytes``.

    The body is buffered up to the cap and then replayed to the application, so
    downstream code still receives the exact bytes the client sent. That keeps
    signature verification honest: it covers raw bytes, never re-encoded JSON.

    ``Content-Length`` is checked first because it avoids reading anything, but
    it is only a hint. The streamed body is counted as well, so a missing
    header, chunked transfer or a lying length cannot bypass the cap. Zero
    disables the check.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            await _reject(scope, receive, send, self.max_bytes)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                # The client left before the body arrived. There is nobody to
                # answer, and the route must not run on a partial request.
                return
            if message["type"] != "http.request":
                continue

            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await _reject(scope, receive, send, self.max_bytes)
                return
            if not message.get("more_body", False):
                break

        buffered = bytes(body)
        replayed = False

        async def replay() -> Message:
            """Hand the buffered body to the application exactly once."""
            nonlocal replayed
            if replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {
                "type": "http.request",
                "body": buffered,
                "more_body": False,
            }

        await self.app(scope, replay, send)


def _content_length(scope: Scope) -> int | None:
    """Return a declared Content-Length, or None when absent or unusable."""
    for name, value in scope.get("headers", []):
        if name.lower() != b"content-length":
            continue
        try:
            return int(value)
        except ValueError:
            return None
    return None


async def _reject(
    scope: Scope,
    receive: Receive,
    send: Send,
    max_bytes: int,
) -> None:
    """Answer 413 and ask the client to close the connection.

    The rest of the body is not drained: the response tells the client the
    connection is finished, so a keep-alive request cannot be confused with the
    unread bytes.
    """
    logger.warning(
        "request body rejected path=%s reason=too_large limit=%s",
        scope.get("path", ""),
        max_bytes,
    )
    response = JSONResponse(
        {"detail": "request body too large"},
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        headers={"Connection": "close"},
    )
    await response(scope, receive, send)
