"""Application entry point.

Run locally with:

    uv run uvicorn app.main:app --reload
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.api.events import router as events_router
from app.api.webhooks import log_validation_error
from app.api.webhooks import router as webhooks_router
from app.db import migrate
from app.engine import run_forever
from app.repositories.events import EventRepository
from app.repositories.keys import KeyRepository
from app.services.events import EventService
from app.services.keys import KeyService
from app.settings import Settings

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Configuration is a Settings object so tests can build one explicitly and
    never depend on the environment. Without one, Settings.from_env() reads and
    validates the HOOKWATCH_* variables, so a bad value stops startup instead
    of failing later inside a request or the engine.

    Cross-field rules that depend on how the process will run, such as
    signature enforcement without any secret, are checked in the lifespan.
    That keeps importing this module free of configuration failures while still
    refusing to serve a process that cannot enforce what it claims.
    """
    config = settings or Settings.from_env()

    # uvicorn configures only its own loggers, so without this the application's
    # INFO messages would be dropped silently. Structured logging is left to a
    # later stage.
    logging.basicConfig(level=config.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        config.validate_for_startup()

        applied = migrate(config.database_path)
        if applied:
            logger.info(
                "applied %s database migration(s) to %s",
                applied,
                config.database_path,
            )

        repository = EventRepository(config.database_path)
        service = EventService(repository, settings=config.processing)
        keys = KeyService(KeyRepository(config.database_path))
        app.state.events = repository
        app.state.event_service = service
        app.state.keys = keys
        app.state.config = config
        app.state.processing = config.processing

        stop = asyncio.Event()
        engine_task: asyncio.Task[None] | None = None
        if config.engine_enabled:
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
