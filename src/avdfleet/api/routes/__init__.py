"""Route registry.

``app.create_app`` includes these routers in order; the WebSocket router lives
in :mod:`avdfleet.api.ws`.  Fixed-path routes are registered before templated
ones inside each module so FastAPI/Starlette never shadows e.g.
``/api/v1/images/refresh`` behind ``/api/v1/images/{image_id}``.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import appium, events, fleet, images, instances, portable, proxies, scripts, system, templates

#: Every REST router, in inclusion order (all are matched before the static
#: SPA catch-all that ``create_app`` registers last).
ROUTERS: tuple[APIRouter, ...] = (
    system.health_router,
    system.router,
    images.router,
    templates.router,
    instances.router,
    proxies.router,
    scripts.router,
    appium.router,
    fleet.router,
    portable.router,
    events.router,
)

__all__ = ["ROUTERS"]
