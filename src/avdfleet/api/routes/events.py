"""Event history route.

The event bus (``avdfleet.core.events.EventBus``) keeps a bounded in-memory
history for the GUI; ``GET /api/v1/events`` exposes the most recent entries so a
late-connecting client can paint state without replaying every log line.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from avdfleet.core.config import API_PREFIX
from avdfleet.core.events import Event

from ..deps import ContainerDep, EventBusDep, require_auth
from ..errors import ErrorResponse

router = APIRouter(
    prefix=API_PREFIX,
    tags=["events"],
    dependencies=[Depends(require_auth)],
    responses={401: {"model": ErrorResponse}},
)


@router.get(
    "/events",
    operation_id="listEvents",
    summary="Recent daemon events",
    response_model=list[Event],
)
def list_events(
    container: ContainerDep,
    bus: EventBusDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[Event]:
    if bus is None:
        # Fall back to a container-provided bus accessor if create_app could not
        # find one up front (duck-typed containers may expose it lazily).
        bus = container.event_bus
    if bus is None:
        return []
    history = getattr(bus, "history", None)
    if not callable(history):
        return []
    events = list(history() or [])
    return events[-int(limit) :]


__all__ = ["router"]
