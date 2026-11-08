"""Application entry point.

Run locally with:

    uv run uvicorn app.main:app --reload
"""

import logging

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.api.webhooks import log_validation_error
from app.api.webhooks import router as webhooks_router

# uvicorn configures only its own loggers, so without this the application's
# INFO messages would be dropped silently. Structured logging is left to the
# production hardening stage.
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="HookWatch")

app.include_router(webhooks_router)
app.add_exception_handler(RequestValidationError, log_validation_error)


@app.get("/health")
def health() -> dict[str, str]:
    """Report whether the application process is running.

    This is a liveness check only: it answers "is the process up and serving
    requests?". It deliberately does not touch the database, because no
    database exists yet and a health check should not become slower or fail
    because of a dependency it does not own.
    """
    return {"status": "ok"}
