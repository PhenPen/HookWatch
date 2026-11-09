"""Background processing loop.

The loop is deliberately small. It asks the service for one batch, then sleeps
when there was nothing to do. All decisions about what to process and how to
retry live in the service; this module only drives it.

Why an in-process task: the database is already the queue, SQLite is local, and
the roadmap explicitly excludes Celery, Redis and brokers until a demonstrated
need exists. Starting a task in the FastAPI lifespan is the simplest execution
model that keeps HTTP requests non-blocking.
"""

import asyncio
import logging

from app.services.events import EventService

logger = logging.getLogger(__name__)


async def run_forever(service: EventService, stop: asyncio.Event) -> None:
    """Process due events until stop is set.

    process_due is synchronous because sqlite3 and the processor are blocking,
    so it runs in a worker thread. That keeps the event loop free to answer
    HTTP requests.

    A failure in one batch is logged and the loop continues. Without that, a
    single unexpected error would silently stop all processing.
    """
    settings = service.settings

    while not stop.is_set():
        try:
            handled = await asyncio.to_thread(service.process_due)
        except Exception:
            logger.exception("processing loop failed; continuing")
            handled = 0

        if handled == 0:
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=settings.poll_interval_seconds,
                )
            except TimeoutError:
                pass
