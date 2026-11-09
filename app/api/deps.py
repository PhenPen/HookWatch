"""Dependencies shared by API routes."""

from fastapi import Request

from app.repositories.events import EventRepository
from app.services.events import EventService


def get_event_repository(request: Request) -> EventRepository:
    """Return the repository built during application startup.

    Using the application state keeps tests able to build an app pointed at a
    temporary database, without any global configuration or monkeypatching.
    """
    return request.app.state.events


def get_event_service(request: Request) -> EventService:
    """Build the service around the shared repository.

    The service is stateless, so constructing one per request costs nothing and
    keeps the rules out of the route.
    """
    return EventService(get_event_repository(request))
