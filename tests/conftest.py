from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    """A database file that exists only for the duration of one test."""
    return tmp_path / "hookwatch-test.db"


@pytest.fixture
def app(database_path: Path) -> FastAPI:
    return create_app(database_path, start_engine=False)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
