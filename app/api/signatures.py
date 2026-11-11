"""Signature verification for webhook deliveries.

The sender and HookWatch share a secret that never travels with the request.
The sender signs the exact bytes it sends:

    X-HookWatch-Signature: t=<unix seconds>,v1=<hex digest>
    v1 = HMAC-SHA256(secret, f"{t}.".encode("ascii") + body)

The timestamp is inside the signed message, so it cannot be moved without the
secret. HookWatch rejects a timestamp outside the configured tolerance, which
bounds how long a captured delivery can be replayed. The digest covers the raw
bytes rather than parsed JSON, because re-serialising JSON can change key order
and whitespace and would invalidate a valid signature.

Nothing here stores or logs the secret or the digest. Callers translate a
SignatureError into a response; this module stays free of HTTP types.
"""

import hashlib
import hmac
import time
from dataclasses import dataclass

SIGNATURE_HEADER = "X-HookWatch-Signature"
SIGNATURE_VERSION = "v1"
_HEX_DIGITS = frozenset("0123456789abcdef")


class SignatureError(Exception):
    """A signature that cannot be accepted, with a short reason for logs."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SignatureParts:
    """The two fields of a signature header, already parsed and checked."""

    timestamp: int
    digest: str


def sign(secret: str, body: bytes, timestamp: int) -> str:
    """Return the hex HMAC-SHA256 for one timestamp and body."""
    message = f"{timestamp}.".encode("ascii") + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def signature_header(
    secret: str,
    body: bytes,
    timestamp: int | None = None,
) -> str:
    """Build a header value, for the simulator and for tests."""
    moment = int(time.time()) if timestamp is None else timestamp
    return f"t={moment},{SIGNATURE_VERSION}={sign(secret, body, moment)}"


def parse_signature_header(value: str) -> SignatureParts:
    """Parse ``t=<int>,v1=<hex>``, rejecting duplicates and extra fields."""
    fields: dict[str, str] = {}
    for part in value.split(","):
        name, separator, raw = part.partition("=")
        if not separator:
            raise SignatureError("malformed")

        name = name.strip().lower()
        raw = raw.strip()
        if not name or not raw or name in fields:
            raise SignatureError("malformed")
        fields[name] = raw

    if set(fields) != {"t", SIGNATURE_VERSION}:
        raise SignatureError("malformed")

    try:
        timestamp = int(fields["t"])
    except ValueError:
        raise SignatureError("malformed") from None

    digest = fields[SIGNATURE_VERSION].lower()
    if len(digest) != 64 or not set(digest) <= _HEX_DIGITS:
        raise SignatureError("malformed")

    return SignatureParts(timestamp=timestamp, digest=digest)


def verify_signature(
    secret: str,
    body: bytes,
    header: str | None,
    *,
    tolerance_seconds: int,
    now: int | None = None,
) -> None:
    """Raise SignatureError unless the header proves possession of the secret.

    ``now`` is injectable so tests do not depend on the wall clock. Age is
    checked in both directions: a clock that runs fast makes fresh signatures
    look stale, and a clock that runs slow makes them look like they come from
    the future. The comparison uses compare_digest so response timing does not
    leak how much of the expected digest matched.
    """
    if not header:
        raise SignatureError("missing")

    parts = parse_signature_header(header)
    current = int(time.time()) if now is None else now
    age = current - parts.timestamp
    if abs(age) > tolerance_seconds:
        raise SignatureError("stale" if age > 0 else "future")

    expected = sign(secret, body, parts.timestamp)
    if not hmac.compare_digest(expected, parts.digest):
        raise SignatureError("mismatch")
