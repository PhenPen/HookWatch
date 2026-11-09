"""Dependencies shared by API routes."""

from fastapi import Request

from app.repositories.events import EventRepository
from app.services.events import EventService


def get_event_repository(request: Request) -> EventRepository:
    """Return the repository built during application startup."""
    return request.app.state.events


def get_event_service(request: Request) -> EventService:
    """Return the service built during application startup.

    The same instance is used by the HTTP route and the processing engine, so
    ingestion and processing share one repository and one set of settings.
    """
    return request.app.state.event_service
