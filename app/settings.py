"""Application configuration for the web process.

Configuration is read from the environment once, at startup, and validated
before the application or the engine uses it. Invalid values stop the process
with a message naming the variable, because a typo that silently falls back to
a default is harder to notice in production than a refusal to start.

The MCP process reads its own three variables in `app.mcp_server`; it does not
need the processing settings below.
"""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from app.services.events import ProcessingSettings

DEFAULT_DATABASE_PATH = Path("hookwatch.db")
DEFAULT_LOG_LEVEL = "INFO"
LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})


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


@dataclass(frozen=True)
class Settings:
    """Everything the web process needs to start.

    One object instead of scattered arguments and environment reads. Tests build
    it explicitly, so no test depends on the developer's environment.

    The replay token uses repr=False because this is a frozen dataclass and its
    generated repr would otherwise print the secret whenever someone logs it.
    """

    database_path: Path = DEFAULT_DATABASE_PATH
    replay_token: str | None = field(default=None, repr=False)
    engine_enabled: bool = True
    log_level: str = DEFAULT_LOG_LEVEL
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

        # An empty or whitespace-only token means replay is disabled, not that
        # a blank secret is configured.
        if self.replay_token is not None and not self.replay_token.strip():
            object.__setattr__(self, "replay_token", None)

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
                replay_token=os.environ.get("HOOKWATCH_REPLAY_TOKEN"),
                engine_enabled=_boolean("HOOKWATCH_ENGINE_ENABLED", True),
                log_level=log_level,
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
