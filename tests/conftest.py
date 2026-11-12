from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import Role
from app.main import create_app
from app.settings import Settings

OPERATOR_NAME = "test-operator"


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    """A database file that exists only for the duration of one test."""
    return tmp_path / "hookwatch-test.db"


@pytest.fixture
def settings(database_path: Path) -> Settings:
    """Explicit configuration, so tests never depend on the environment.

    Signature enforcement is disabled here because most tests are about the
    event lifecycle, not about sender authentication. The signature suite turns
    it on explicitly and signs every delivery.

    Rate limits are disabled for the same reason: most tests care about
    behaviour, not load. The limit suite builds its own app and turns them on.
    """
    return Settings(
        database_path=database_path,
        engine_enabled=False,
        require_signatures=False,
        rate_limit_per_minute=0,
        webhook_rate_limit_per_minute=0,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """A client that is authenticated as an operator by default.

    The key is created after the lifespan has run, because that is what
    migrates the database. Most tests are about behaviour rather than
    authorization, so they inherit this header; tests that prove a refusal
    replace or remove it.
    """
    with TestClient(app) as test_client:
        issued = app.state.keys.create(OPERATOR_NAME, Role.OPERATOR)
        test_client.headers["Authorization"] = f"Bearer {issued.key}"
        yield test_client
