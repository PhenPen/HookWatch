from pathlib import Path

import pytest

from app.services.events import ProcessingSettings
from app.settings import Settings

VARIABLES = (
    "HOOKWATCH_DB_PATH",
    "HOOKWATCH_REPLAY_TOKEN",
    "HOOKWATCH_ENGINE_ENABLED",
    "HOOKWATCH_LOG_LEVEL",
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
    assert settings.replay_token is None
    assert settings.engine_enabled is True
    assert settings.log_level == "INFO"
    assert settings.processing == ProcessingSettings()


def test_reads_every_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOOKWATCH_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("HOOKWATCH_REPLAY_TOKEN", "a-secret")
    monkeypatch.setenv("HOOKWATCH_ENGINE_ENABLED", "false")
    monkeypatch.setenv("HOOKWATCH_LOG_LEVEL", "debug")
    monkeypatch.setenv("HOOKWATCH_POLL_INTERVAL_SECONDS", "0.5")
    monkeypatch.setenv("HOOKWATCH_BATCH_SIZE", "3")
    monkeypatch.setenv("HOOKWATCH_MAX_ATTEMPTS", "7")
    monkeypatch.setenv("HOOKWATCH_BACKOFF_BASE_SECONDS", "4")
    monkeypatch.setenv("HOOKWATCH_BACKOFF_CAP_SECONDS", "30")
    monkeypatch.setenv("HOOKWATCH_STALE_AFTER_SECONDS", "120")

    settings = Settings.from_env()

    assert settings.database_path == tmp_path / "events.db"
    assert settings.replay_token == "a-secret"
    assert settings.engine_enabled is False
    assert settings.log_level == "DEBUG"
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


def test_blank_variables_fall_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in VARIABLES:
        monkeypatch.setenv(name, "")

    settings = Settings.from_env()

    assert settings.database_path == Path("hookwatch.db")
    assert settings.replay_token is None
    assert settings.engine_enabled is True
    assert settings.log_level == "INFO"
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


def test_blank_replay_token_disables_replay() -> None:
    assert Settings(replay_token="   ").replay_token is None


def test_repr_does_not_expose_the_replay_token() -> None:
    settings = Settings(replay_token="super-secret")

    assert "super-secret" not in repr(settings)
    assert "super-secret" not in str(settings)
