"""Dependencies shared by API routes."""

from fastapi import Request

from app.repositories.events import EventRepository


def get_event_repository(request: Request) -> EventRepository:
    """Return the repository built during application startup.

    Using the application state keeps tests able to build an app pointed at a
    temporary database, without any global configuration or monkeypatching.
    """
    return request.app.state.events
