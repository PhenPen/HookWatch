from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings


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
    """
    return Settings(
        database_path=database_path,
        engine_enabled=False,
        require_signatures=False,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
