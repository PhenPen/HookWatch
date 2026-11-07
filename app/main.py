"""Application entry point.

Run locally with:

    uv run uvicorn app.main:app --reload
"""

from fastapi import FastAPI

app = FastAPI(title="HookWatch")


@app.get("/health")
def health() -> dict[str, str]:
    """Report whether the application process is running.

    This is a liveness check only: it answers "is the process up and serving
    requests?". It deliberately does not touch the database, because no
    database exists yet and a health check should not become slower or fail
    because of a dependency it does not own.
    """
    return {"status": "ok"}
