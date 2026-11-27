"""FastAPI dependencies: configuration, paths, container and authentication.

Routes depend on these instead of reaching into ``request.app.state`` directly.
The dependency aliases at the bottom keep route signatures short, for example::

    def list_images(container: ContainerDep, include_remote: bool = True): ...
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from avdfleet.core.config import Config
from avdfleet.core.errors import UnauthorizedError
from avdfleet.core.paths import Paths

from .auth import TokenStore, extract_bearer
from .container import ContainerAdapter, ServiceUnavailable

T = TypeVar("T")

#: Registers the ``bearerAuth`` security scheme in the generated OpenAPI and
#: extracts credentials.  ``auto_error=False`` keeps control of the 401 body in
#: our own exception handler so it always matches the contract envelope.
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="bearerAuth",
    description="Daemon bearer token; read it from the token file in the data directory.",
    bearerFormat="opaque",
)


def _state(request: Request) -> Any:
    return request.app.state


def get_config(request: Request) -> Config:
    """Return the effective daemon configuration."""
    config = getattr(_state(request), "config", None)
    if config is None:  # pragma: no cover - create_app always sets this
        raise ServiceUnavailable("the daemon configuration is not available")
    return config  # type: ignore[no-any-return]


def get_paths(request: Request) -> Paths:
    """Return the resolved filesystem layout."""
    paths = getattr(_state(request), "paths", None)
    if paths is None:  # pragma: no cover - create_app always sets this
        raise ServiceUnavailable("the daemon paths are not available")
    return paths  # type: ignore[no-any-return]


def get_container(request: Request) -> ContainerAdapter:
    """Return the service-container adapter (never imports concrete services)."""
    container = getattr(_state(request), "container", None)
    if container is None:  # pragma: no cover - create_app solves this in lifespan
        raise ServiceUnavailable("the daemon services are still starting")
    return container  # type: ignore[no-any-return]


def get_event_bus(request: Request) -> Any | None:
    """Return the daemon event bus when one is configured."""
    return getattr(_state(request), "event_bus", None)


def get_token_store(request: Request) -> TokenStore:
    """Return the bearer-token store."""
    store = getattr(_state(request), "token_store", None)
    if store is None:  # pragma: no cover - create_app always sets this
        raise ServiceUnavailable("the API token store is not available")
    return store  # type: ignore[no-any-return]


def require_auth(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    config: Annotated[Config, Depends(get_config)],
) -> None:
    """Enforce bearer-token authentication (no-op when ``auth_required`` is off)."""
    if not bool(getattr(config, "auth_required", True)):
        return
    token = credentials.credentials if credentials is not None else None
    if not token:
        token = extract_bearer(request.headers)
    if not token:
        raise UnauthorizedError(
            "missing bearer token",
            hint="send 'Authorization: Bearer <token>' using the token from the data directory",
        )
    store = get_token_store(request)
    if not store.matches(token):
        raise UnauthorizedError(
            "invalid bearer token",
            hint="read the token from <data_dir>/token or start the daemon with AVDFLEET_TOKEN",
        )


#: Convenience aliases for route signatures.
ConfigDep = Annotated[Config, Depends(get_config)]
PathsDep = Annotated[Paths, Depends(get_paths)]
ContainerDep = Annotated[ContainerAdapter, Depends(get_container)]
EventBusDep = Annotated[Any | None, Depends(get_event_bus)]
TokenStoreDep = Annotated[TokenStore, Depends(get_token_store)]


def callable_or_none(target: Any, name: str) -> Callable[..., Any] | None:
    """Return ``target.name`` when it is callable, else None (no exceptions)."""
    candidate = getattr(target, name, None)
    return candidate if callable(candidate) else None


__all__ = [
    "ConfigDep",
    "ContainerDep",
    "EventBusDep",
    "PathsDep",
    "TokenStoreDep",
    "bearer_scheme",
    "callable_or_none",
    "get_config",
    "get_container",
    "get_event_bus",
    "get_paths",
    "get_token_store",
    "require_auth",
]
