from pathlib import Path

import pytest

from app.services.events import ProcessingSettings
from app.settings import (
    MAX_BODY_BYTES,
    MAX_RATE_LIMIT_PER_MINUTE,
    Settings,
    parse_webhook_secrets,
)

VARIABLES = (
    "HOOKWATCH_DB_PATH",
    "HOOKWATCH_ENGINE_ENABLED",
    "HOOKWATCH_LOG_LEVEL",
    "HOOKWATCH_WEBHOOK_SECRETS",
    "HOOKWATCH_REQUIRE_SIGNATURES",
    "HOOKWATCH_SIGNATURE_TOLERANCE_SECONDS",
    "HOOKWATCH_MAX_BODY_BYTES",
    "HOOKWATCH_RATE_LIMIT_PER_MINUTE",
    "HOOKWATCH_WEBHOOK_RATE_LIMIT_PER_MINUTE",
    "HOOKWATCH_POLL_INTERVAL_SECONDS",
    "HOOKWATCH_BATCH_SIZE",
    "HOOKWATCH_MAX_ATTEMPTS",
    "HOOKWATCH_BACKOFF_BASE_SECONDS",
    "HOOKWATCH_BACKOFF_CAP_SECONDS",
    "HOOKWATCH_STALE_AFTER_SECONDS",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every configuration variable so tests start from the defaults."""
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_defaults_match_the_code_defaults() -> None:
    settings = Settings.from_env()

    assert settings.database_path == Path("hookwatch.db")
    assert settings.engine_enabled is True
    assert settings.log_level == "INFO"
    assert settings.webhook_secrets == {}
    assert settings.require_signatures is True
    assert settings.signature_tolerance_seconds == 300
    assert settings.max_body_bytes == 1024 * 1024
    assert settings.rate_limit_per_minute == 600
    assert settings.webhook_rate_limit_per_minute == 6000
    assert settings.processing == ProcessingSettings()


def test_enforcement_is_on_by_default_and_needs_a_secret() -> None:
    settings = Settings.from_env()

    with pytest.raises(ValueError, match="at least one webhook secret"):
        settings.validate_for_startup()


def test_reads_every_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOOKWATCH_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("HOOKWATCH_ENGINE_ENABLED", "false")
    monkeypatch.setenv("HOOKWATCH_LOG_LEVEL", "debug")
    monkeypatch.setenv(
        "HOOKWATCH_WEBHOOK_SECRETS", "github=one,stripe=two"
    )
    monkeypatch.setenv("HOOKWATCH_REQUIRE_SIGNATURES", "true")
    monkeypatch.setenv("HOOKWATCH_SIGNATURE_TOLERANCE_SECONDS", "120")
    monkeypatch.setenv("HOOKWATCH_MAX_BODY_BYTES", "2048")
    monkeypatch.setenv("HOOKWATCH_RATE_LIMIT_PER_MINUTE", "12")
    monkeypatch.setenv("HOOKWATCH_WEBHOOK_RATE_LIMIT_PER_MINUTE", "34")
    monkeypatch.setenv("HOOKWATCH_POLL_INTERVAL_SECONDS", "0.5")
    monkeypatch.setenv("HOOKWATCH_BATCH_SIZE", "3")
    monkeypatch.setenv("HOOKWATCH_MAX_ATTEMPTS", "7")
    monkeypatch.setenv("HOOKWATCH_BACKOFF_BASE_SECONDS", "4")
    monkeypatch.setenv("HOOKWATCH_BACKOFF_CAP_SECONDS", "30")
    monkeypatch.setenv("HOOKWATCH_STALE_AFTER_SECONDS", "120")

    settings = Settings.from_env()

    assert settings.database_path == tmp_path / "events.db"
    assert settings.engine_enabled is False
    assert settings.log_level == "DEBUG"
    assert settings.webhook_secrets == {"github": "one", "stripe": "two"}
    assert settings.require_signatures is True
    assert settings.signature_tolerance_seconds == 120
    assert settings.max_body_bytes == 2048
    assert settings.rate_limit_per_minute == 12
    assert settings.webhook_rate_limit_per_minute == 34
    assert settings.processing.poll_interval_seconds == 0.5
    assert settings.processing.batch_size == 3
    assert settings.processing.max_attempts == 7
    assert settings.processing.backoff_base_seconds == 4
    assert settings.processing.backoff_cap_seconds == 30
    assert settings.processing.stale_after_seconds == 120


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_accepts_true_spellings(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("HOOKWATCH_ENGINE_ENABLED", raw)

    assert Settings.from_env().engine_enabled is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off"])
def test_accepts_false_spellings(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("HOOKWATCH_ENGINE_ENABLED", raw)

    assert Settings.from_env().engine_enabled is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_accepts_true_signature_spellings(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("HOOKWATCH_REQUIRE_SIGNATURES", raw)

    assert Settings.from_env().require_signatures is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off"])
def test_accepts_false_signature_spellings(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("HOOKWATCH_REQUIRE_SIGNATURES", raw)

    assert Settings.from_env().require_signatures is False


def test_blank_variables_fall_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in VARIABLES:
        monkeypatch.setenv(name, "")

    settings = Settings.from_env()

    assert settings.database_path == Path("hookwatch.db")
    assert settings.engine_enabled is True
    assert settings.log_level == "INFO"
    assert settings.webhook_secrets == {}
    assert settings.require_signatures is True
    assert settings.signature_tolerance_seconds == 300
    assert settings.max_body_bytes == 1024 * 1024
    assert settings.rate_limit_per_minute == 600
    assert settings.webhook_rate_limit_per_minute == 6000
    assert settings.processing == ProcessingSettings()


@pytest.mark.parametrize(
    ("name", "raw", "message"),
    [
        ("HOOKWATCH_BATCH_SIZE", "ten", "must be an integer"),
        ("HOOKWATCH_BATCH_SIZE", "1.5", "must be an integer"),
        ("HOOKWATCH_POLL_INTERVAL_SECONDS", "fast", "must be a number"),
        ("HOOKWATCH_POLL_INTERVAL_SECONDS", "nan", "must be finite"),
        ("HOOKWATCH_BACKOFF_CAP_SECONDS", "inf", "must be finite"),
        ("HOOKWATCH_ENGINE_ENABLED", "maybe", "must be one of"),
        ("HOOKWATCH_REQUIRE_SIGNATURES", "maybe", "must be one of"),
        (
            "HOOKWATCH_SIGNATURE_TOLERANCE_SECONDS",
            "soon",
            "must be an integer",
        ),
        ("HOOKWATCH_MAX_BODY_BYTES", "big", "must be an integer"),
        ("HOOKWATCH_RATE_LIMIT_PER_MINUTE", "fast", "must be an integer"),
        (
            "HOOKWATCH_WEBHOOK_RATE_LIMIT_PER_MINUTE",
            "often",
            "must be an integer",
        ),
        ("HOOKWATCH_WEBHOOK_SECRETS", "github", "source=secret"),
        ("HOOKWATCH_WEBHOOK_SECRETS", "github=a,github=b", "more than once"),
        ("HOOKWATCH_WEBHOOK_SECRETS", "bad name=one", "source"),
    ],
)
def test_unparsable_values_name_the_variable(
    monkeypatch: pytest.MonkeyPatch, name: str, raw: str, message: str
) -> None:
    monkeypatch.setenv(name, raw)

    with pytest.raises(ValueError, match=message) as error:
        Settings.from_env()

    assert name in str(error.value)
    assert "invalid HookWatch configuration" in str(error.value)


@pytest.mark.parametrize(
    ("processing", "message"),
    [
        (ProcessingSettings(batch_size=0), "batch_size"),
        (ProcessingSettings(max_attempts=0), "max_attempts"),
        (ProcessingSettings(poll_interval_seconds=0), "poll_interval_seconds"),
        (ProcessingSettings(backoff_base_seconds=0), "backoff_base_seconds"),
        (
            ProcessingSettings(backoff_base_seconds=10, backoff_cap_seconds=5),
            "backoff_cap_seconds",
        ),
        (ProcessingSettings(stale_after_seconds=0), "stale_after_seconds"),
    ],
)
def test_impossible_processing_values_are_rejected(
    processing: ProcessingSettings, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Settings(processing=processing)


def test_log_level_is_normalised() -> None:
    assert Settings(log_level="warning").log_level == "WARNING"


def test_unknown_log_level_is_rejected() -> None:
    with pytest.raises(ValueError, match="log_level"):
        Settings(log_level="verbose")


def test_repr_does_not_expose_webhook_secrets() -> None:
    settings = Settings(webhook_secrets={"github": "signing-secret"})

    assert "signing-secret" not in repr(settings)
    assert "signing-secret" not in str(settings)


def test_webhook_secrets_mapping_is_copied() -> None:
    configured = {"github": "one"}
    settings = Settings(webhook_secrets=configured)

    configured["github"] = "changed"

    assert settings.webhook_secrets == {"github": "one"}


def test_webhook_secrets_are_parsed() -> None:
    assert parse_webhook_secrets("") == {}
    assert parse_webhook_secrets("   ") == {}
    assert parse_webhook_secrets(" github = one , stripe = two ") == {
        "github": "one",
        "stripe": "two",
    }
    # Only the first equals sign separates the pair, so a secret may contain
    # one. This keeps base64-style secrets usable.
    assert parse_webhook_secrets("github=a=b") == {"github": "a=b"}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("github", "source=secret"),
        ("github=", "source=secret"),
        ("=one", "source=secret"),
        ("github=one,", "empty entry"),
        (",github=one", "empty entry"),
        ("github=one,stripe", "source=secret"),
        ("github=one,github=two", "more than once"),
        ("bad name=one", "source"),
        ("a" * 65 + "=one", "source"),
        ("github=one,stripe=", "source=secret"),
    ],
)
def test_webhook_secrets_reject_bad_entries(raw: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_webhook_secrets(raw)


def test_explicit_webhook_secrets_are_validated() -> None:
    with pytest.raises(ValueError, match="source"):
        Settings(webhook_secrets={"bad name": "one"}, require_signatures=False)

    with pytest.raises(ValueError, match="must not be empty"):
        Settings(webhook_secrets={"github": "   "}, require_signatures=False)


@pytest.mark.parametrize("tolerance", [0, -1, 86401])
def test_impossible_tolerances_are_rejected(tolerance: int) -> None:
    with pytest.raises(ValueError, match="signature_tolerance_seconds"):
        Settings(
            signature_tolerance_seconds=tolerance,
            require_signatures=False,
        )


@pytest.mark.parametrize("tolerance", [1, 300, 86400])
def test_acceptable_tolerances_are_kept(tolerance: int) -> None:
    settings = Settings(
        signature_tolerance_seconds=tolerance,
        require_signatures=False,
    )

    assert settings.signature_tolerance_seconds == tolerance


def test_http_limits_can_be_disabled() -> None:
    settings = Settings(
        max_body_bytes=0,
        rate_limit_per_minute=0,
        webhook_rate_limit_per_minute=0,
    )

    assert settings.max_body_bytes == 0
    assert settings.rate_limit_per_minute == 0
    assert settings.webhook_rate_limit_per_minute == 0


@pytest.mark.parametrize(
    "field",
    [
        "max_body_bytes",
        "rate_limit_per_minute",
        "webhook_rate_limit_per_minute",
    ],
)
def test_negative_http_limits_are_rejected(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        Settings(**{field: -1})


@pytest.mark.parametrize(
    "field",
    [
        "max_body_bytes",
        "rate_limit_per_minute",
        "webhook_rate_limit_per_minute",
    ],
)
def test_boolean_http_limits_are_rejected(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        Settings(**{field: True})


def test_absurd_http_limits_are_rejected() -> None:
    with pytest.raises(ValueError, match="max_body_bytes"):
        Settings(max_body_bytes=MAX_BODY_BYTES + 1)

    with pytest.raises(ValueError, match="rate_limit_per_minute"):
        Settings(rate_limit_per_minute=MAX_RATE_LIMIT_PER_MINUTE + 1)

    with pytest.raises(ValueError, match="webhook_rate_limit_per_minute"):
        Settings(
            webhook_rate_limit_per_minute=MAX_RATE_LIMIT_PER_MINUTE + 1
        )


def test_enforcement_passes_with_a_secret() -> None:
    Settings(webhook_secrets={"github": "one"}).validate_for_startup()


def test_enforcement_can_be_disabled_without_secrets() -> None:
    Settings(require_signatures=False).validate_for_startup()
