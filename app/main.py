"""Application entry point.

Run locally with:

    uv run uvicorn app.main:app --reload
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.api.events import router as events_router
from app.api.webhooks import log_validation_error
from app.api.webhooks import router as webhooks_router
from app.db import migrate
from app.engine import run_forever
from app.repositories.events import EventRepository
from app.services.events import EventService, ProcessingSettings

# uvicorn configures only its own loggers, so without this the application's
# INFO messages would be dropped silently. Structured logging is left to the
# production hardening stage.
logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_PATH = Path("hookwatch.db")


def create_app(
    database_path: Path | None = None,
    *,
    start_engine: bool = True,
    settings: ProcessingSettings | None = None,
    replay_token: str | None = None,
) -> FastAPI:
    """Build the application.

    The database path is a parameter so tests can use a temporary file and never
    touch the development database. The module level app below is the normal
    entry point for uvicorn.

    Tests that inspect stored rows set start_engine to False. Otherwise the
    background loop would process events while a test is asserting on their
    initial received state.

    replay_token is the shared secret that enables replay. Passing an empty
    string disables replay explicitly, which is how tests prove the closed
    default even on a machine where the environment variable is set.
    """
    path = database_path or Path(
        os.environ.get("HOOKWATCH_DB_PATH", str(DEFAULT_DATABASE_PATH))
    )
    processing_settings = settings or ProcessingSettings()
    token = (
        replay_token
        if replay_token is not None
        else os.environ.get("HOOKWATCH_REPLAY_TOKEN") or None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        applied = migrate(path)
        if applied:
            logger.info("applied %s database migration(s) to %s", applied, path)

        repository = EventRepository(path)
        service = EventService(repository, settings=processing_settings)
        app.state.events = repository
        app.state.event_service = service
        app.state.settings = processing_settings
        app.state.replay_token = token

        stop = asyncio.Event()
        engine_task: asyncio.Task[None] | None = None
        if start_engine:
            engine_task = asyncio.create_task(run_forever(service, stop))

        try:
            yield
        finally:
            stop.set()
            if engine_task is not None:
                await engine_task

    app = FastAPI(title="HookWatch", lifespan=lifespan)

    app.include_router(webhooks_router)
    app.include_router(events_router)
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
