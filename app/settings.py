"""Application configuration for the web process.

Configuration is read from the environment once, at startup, and validated
before the application or the engine uses it. Invalid values stop the process
with a message naming the variable, because a typo that silently falls back to
a default is harder to notice in production than a refusal to start.

Signature policy lives here too: which sources have a shared secret, whether
signatures are required, and how old a signed request may be. The signing
algorithm and header name are protocol constants and deliberately stay in
`app.api.signatures`, because a configurable protocol would let the sender and
the receiver disagree silently.

HTTP abuse limits are policy as well: the largest accepted body and the
per-minute allowance for reads and deliveries. The token bucket and the 413 and
429 responses stay in `app.api.limits`; only the numbers are configurable.

The MCP process reads its own variables in `app.mcp_server`; it does not need
the webhook or processing settings below.
"""

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.services.events import ProcessingSettings

DEFAULT_DATABASE_PATH = Path("hookwatch.db")
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_SIGNATURE_TOLERANCE_SECONDS = 300
MAX_SIGNATURE_TOLERANCE_SECONDS = 86400
DEFAULT_MAX_BODY_BYTES = 1024 * 1024
MAX_BODY_BYTES = 64 * 1024 * 1024
DEFAULT_RATE_LIMIT_PER_MINUTE = 600
DEFAULT_WEBHOOK_RATE_LIMIT_PER_MINUTE = 6000
MAX_RATE_LIMIT_PER_MINUTE = 1_000_000
LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})

# The same shape the webhook route accepts, so a name that cannot be stored in
# the path cannot be configured as a source either.
SOURCE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _integer(name: str, default: int) -> int:
    """Read an integer, or the default when the variable is unset or blank."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


def _number(name: str, default: float) -> float:
    """Read a finite float, or the default when the variable is unset or blank."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {raw!r}")
    return value


def _boolean(name: str, default: bool) -> bool:
    """Read a boolean from the documented spellings, rejecting anything else."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ValueError(
        f"{name} must be one of true, false, 1, 0, yes, no, on, off; got {raw!r}"
    )


def parse_webhook_secrets(raw: str) -> dict[str, str]:
    """Parse ``source=secret`` pairs separated by commas.

    The simulator imports this so it signs with the same secrets the web
    process verifies. Comma is the separator, so a secret cannot contain one;
    generated secrets are URL-safe and do not need it. A secret may contain
    ``=`` because each pair is split only at the first one.

    The error messages name the environment variable on purpose: this parser is
    used when reading configuration, and a message that only says "bad pair"
    would not tell an operator where to look.
    """
    if not raw.strip():
        return {}

    secrets: dict[str, str] = {}
    for entry in raw.split(","):
        pair = entry.strip()
        if not pair:
            raise ValueError("HOOKWATCH_WEBHOOK_SECRETS contains an empty entry")

        source, separator, secret = pair.partition("=")
        source = source.strip()
        secret = secret.strip()
        if not separator or not source or not secret:
            raise ValueError(
                "HOOKWATCH_WEBHOOK_SECRETS entries must look like source=secret, "
                f"got {pair!r}"
            )
        if not SOURCE_NAME.fullmatch(source):
            raise ValueError(
                "HOOKWATCH_WEBHOOK_SECRETS source "
                f"{source!r} must be 1 to 64 characters of letters, digits, "
                "dot, underscore or hyphen"
            )
        if source in secrets:
            raise ValueError(
                f"HOOKWATCH_WEBHOOK_SECRETS names source {source!r} more than once"
            )
        secrets[source] = secret

    return secrets


@dataclass(frozen=True)
class Settings:
    """Everything the web process needs to start.

    One object instead of scattered arguments and environment reads. Tests build
    it explicitly, so no test depends on the developer's environment.

    The webhook secrets use repr=False because this is a frozen dataclass and
    its generated repr would otherwise print the secrets whenever someone logs
    the settings. Operator API keys are not configuration at all: they live in
    the database and are read by KeyService.
    """

    database_path: Path = DEFAULT_DATABASE_PATH
    engine_enabled: bool = True
    log_level: str = DEFAULT_LOG_LEVEL
    webhook_secrets: dict[str, str] = field(default_factory=dict, repr=False)
    require_signatures: bool = True
    signature_tolerance_seconds: int = DEFAULT_SIGNATURE_TOLERANCE_SECONDS
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE
    webhook_rate_limit_per_minute: int = DEFAULT_WEBHOOK_RATE_LIMIT_PER_MINUTE
    processing: ProcessingSettings = field(default_factory=ProcessingSettings)

    def __post_init__(self) -> None:
        # Frozen, but normalising during construction is safe and keeps a
        # lowercase value from failing only because of its case.
        log_level = self.log_level.strip().upper()
        if log_level not in LOG_LEVELS:
            raise ValueError(
                "log_level must be one of "
                + ", ".join(sorted(LOG_LEVELS))
                + f", got {self.log_level!r}"
            )
        object.__setattr__(self, "log_level", log_level)

        # Copy the mapping so a caller cannot mutate configuration afterwards.
        secrets: dict[str, str] = {}
        for source, secret in self.webhook_secrets.items():
            if not isinstance(source, str) or not SOURCE_NAME.fullmatch(source):
                raise ValueError(
                    f"webhook secret source {source!r} is not a valid source name"
                )
            if not isinstance(secret, str) or not secret.strip():
                raise ValueError(
                    f"webhook secret for source {source!r} must not be empty"
                )
            secrets[source] = secret
        object.__setattr__(self, "webhook_secrets", secrets)

        tolerance = self.signature_tolerance_seconds
        if isinstance(tolerance, bool) or not isinstance(tolerance, int):
            raise ValueError("signature_tolerance_seconds must be an integer")
        if tolerance < 1:
            raise ValueError("signature_tolerance_seconds must be at least 1")
        if tolerance > MAX_SIGNATURE_TOLERANCE_SECONDS:
            raise ValueError(
                "signature_tolerance_seconds must be at most "
                f"{MAX_SIGNATURE_TOLERANCE_SECONDS}"
            )

        for name, value, maximum in (
            ("max_body_bytes", self.max_body_bytes, MAX_BODY_BYTES),
            (
                "rate_limit_per_minute",
                self.rate_limit_per_minute,
                MAX_RATE_LIMIT_PER_MINUTE,
            ),
            (
                "webhook_rate_limit_per_minute",
                self.webhook_rate_limit_per_minute,
                MAX_RATE_LIMIT_PER_MINUTE,
            ),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must be at least 0")
            if value > maximum:
                raise ValueError(f"{name} must be at most {maximum}")

        processing = self.processing
        if processing.batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if processing.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if not math.isfinite(processing.poll_interval_seconds):
            raise ValueError("poll_interval_seconds must be finite")
        if processing.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than 0")
        if not math.isfinite(processing.backoff_base_seconds):
            raise ValueError("backoff_base_seconds must be finite")
        if processing.backoff_base_seconds <= 0:
            raise ValueError("backoff_base_seconds must be greater than 0")
        if not math.isfinite(processing.backoff_cap_seconds):
            raise ValueError("backoff_cap_seconds must be finite")
        if processing.backoff_cap_seconds < processing.backoff_base_seconds:
            raise ValueError(
                "backoff_cap_seconds must be greater than or equal to "
                "backoff_base_seconds"
            )
        if not math.isfinite(processing.stale_after_seconds):
            raise ValueError("stale_after_seconds must be finite")
        if processing.stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be greater than 0")

    def validate_for_startup(self) -> None:
        """Reject a configuration that cannot enforce what it promises.

        This cross-field rule is checked when the application starts rather
        than during construction, so importing `app.main` stays side-effect
        free for tests. A process that requires signatures but has no secrets
        would answer 401 to every delivery, which looks like a sender problem.
        Refusing to start makes the misconfiguration obvious.
        """
        if self.require_signatures and not self.webhook_secrets:
            raise ValueError(
                "signature enforcement requires at least one webhook secret; "
                "set HOOKWATCH_WEBHOOK_SECRETS or set "
                "HOOKWATCH_REQUIRE_SIGNATURES=false"
            )

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from HOOKWATCH_* environment variables.

        Processing defaults are read from a single ProcessingSettings instance
        so the code defaults and the environment defaults cannot drift apart.
        """
        defaults = ProcessingSettings()
        raw_path = os.environ.get("HOOKWATCH_DB_PATH", "").strip()
        log_level = (
            os.environ.get("HOOKWATCH_LOG_LEVEL", "").strip() or DEFAULT_LOG_LEVEL
        )

        try:
            return cls(
                database_path=Path(raw_path) if raw_path else DEFAULT_DATABASE_PATH,
                engine_enabled=_boolean("HOOKWATCH_ENGINE_ENABLED", True),
                log_level=log_level,
                webhook_secrets=parse_webhook_secrets(
                    os.environ.get("HOOKWATCH_WEBHOOK_SECRETS", "")
                ),
                require_signatures=_boolean("HOOKWATCH_REQUIRE_SIGNATURES", True),
                signature_tolerance_seconds=_integer(
                    "HOOKWATCH_SIGNATURE_TOLERANCE_SECONDS",
                    DEFAULT_SIGNATURE_TOLERANCE_SECONDS,
                ),
                max_body_bytes=_integer(
                    "HOOKWATCH_MAX_BODY_BYTES",
                    DEFAULT_MAX_BODY_BYTES,
                ),
                rate_limit_per_minute=_integer(
                    "HOOKWATCH_RATE_LIMIT_PER_MINUTE",
                    DEFAULT_RATE_LIMIT_PER_MINUTE,
                ),
                webhook_rate_limit_per_minute=_integer(
                    "HOOKWATCH_WEBHOOK_RATE_LIMIT_PER_MINUTE",
                    DEFAULT_WEBHOOK_RATE_LIMIT_PER_MINUTE,
                ),
                processing=ProcessingSettings(
                    poll_interval_seconds=_number(
                        "HOOKWATCH_POLL_INTERVAL_SECONDS",
                        defaults.poll_interval_seconds,
                    ),
                    batch_size=_integer(
                        "HOOKWATCH_BATCH_SIZE",
                        defaults.batch_size,
                    ),
                    max_attempts=_integer(
                        "HOOKWATCH_MAX_ATTEMPTS",
                        defaults.max_attempts,
                    ),
                    backoff_base_seconds=_number(
                        "HOOKWATCH_BACKOFF_BASE_SECONDS",
                        defaults.backoff_base_seconds,
                    ),
                    backoff_cap_seconds=_number(
                        "HOOKWATCH_BACKOFF_CAP_SECONDS",
                        defaults.backoff_cap_seconds,
                    ),
                    stale_after_seconds=_number(
                        "HOOKWATCH_STALE_AFTER_SECONDS",
                        defaults.stale_after_seconds,
                    ),
                ),
            )
        except ValueError as error:
            raise ValueError(
                f"invalid HookWatch configuration: {error}"
            ) from None
