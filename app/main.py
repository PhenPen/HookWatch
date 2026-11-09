"""Application entry point.

Run locally with:

    uv run uvicorn app.main:app --reload
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.api.webhooks import log_validation_error
from app.api.webhooks import router as webhooks_router
from app.db import migrate
from app.repositories.events import EventRepository

# uvicorn configures only its own loggers, so without this the application's
# INFO messages would be dropped silently. Structured logging is left to the
# production hardening stage.
logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_PATH = Path("hookwatch.db")


def create_app(database_path: Path | None = None) -> FastAPI:
    """Build the application.

    The database path is a parameter so tests can use a temporary file and never
    touch the development database. The module level app below is the normal
    entry point for uvicorn.
    """
    path = database_path or Path(
        os.environ.get("HOOKWATCH_DB_PATH", str(DEFAULT_DATABASE_PATH))
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        applied = migrate(path)
        if applied:
            logger.info("applied %s database migration(s) to %s", applied, path)
        app.state.events = EventRepository(path)
        yield

    app = FastAPI(title="HookWatch", lifespan=lifespan)

    app.include_router(webhooks_router)
    app.add_exception_handler(RequestValidationError, log_validation_error)

    @app.get("/health")
    def health() -> dict[str, str]:
        """Report whether the application process is running.

        This is a liveness check only: it answers "is the process up and serving
        requests?". Checking the database here would turn a liveness probe into
        a readiness probe, which is a different question answered in a later
        stage.
        """
        return {"status": "ok"}

    return app


app = create_app()
