"""Service-container adaptation for the API layer.

The daemon reaches the rest of ``avdfleet`` exclusively through the
:class:`avdfleet.core.interfaces.ServiceContainer` protocol.  Two container
shapes are supported:

1. The production container built by :func:`avdfleet.container.build_container`
   (wired by the root/integration agent).  It is imported lazily so the API
   tests and the CLI can import this package without pulling in every service.
2. A duck-typed stub used by tests and tooling: any object that exposes the
   protocol getters (``images()``, ``runtime()`` …), service attributes, or a
   subset of them.

:class:`ContainerAdapter` normalises both.  Missing services surface as HTTP
503 ``service_unavailable`` and missing service *methods* as HTTP 501
``not_implemented`` instead of a generic 500, so the UI can distinguish a
not-yet-wired feature from a bug.

Route handlers never import concrete service modules; they only call
``request.app.state.container`` through this adapter.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping
from typing import Any

from avdfleet.core.config import Config
from avdfleet.core.errors import AvdFleetError
from avdfleet.core.events import EventBus
from avdfleet.core.interfaces import ServiceContainer as ServiceContainerProtocol
from avdfleet.core.paths import Paths

logger = logging.getLogger("avdfleet.api.container")

#: Getter names of the core ServiceContainer protocol, in declaration order.
SERVICE_GETTERS: tuple[str, ...] = (
    "doctor",
    "installer",
    "images",
    "avds",
    "runtime",
    "proxies",
    "assignments",
    "bridge",
    "oobe",
    "scripts",
    "appium",
    "screen",
    "fleet",
    "portable",
)

#: Extra duck-typed getters used when the concrete container provides them.
EXTRA_GETTERS: tuple[str, ...] = (
    "verifier",
    "host",
    "repository",
    "store",
)


class ServiceUnavailable(AvdFleetError):
    """A service required by this endpoint has not been wired into the daemon."""

    code = "service_unavailable"
    http_status = 503

    def __init__(self, message: str, *, service: str | None = None, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.service = service


class UnsupportedOperation(AvdFleetError):
    """The service exists but does not implement the operation this route needs."""

    code = "not_implemented"
    http_status = 501


class BoundService:
    """Thin attribute proxy that turns missing methods into HTTP 501s.

    The proxy is intentionally transparent for everything the service does
    provide; only attribute lookups that fail raise :class:`UnsupportedOperation`
    with a message suitable for an API error envelope.
    """

    __slots__ = ("_target", "service_name")

    def __init__(self, service_name: str, target: Any) -> None:
        self._target = target
        self.service_name = service_name

    def __getattr__(self, item: str) -> Any:
        try:
            return getattr(self._target, item)
        except AttributeError as exc:
            raise UnsupportedOperation(
                f"the {self.service_name} service does not implement '{item}'",
                hint="this capability is not available in the current build",
            ) from exc

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"BoundService({self.service_name!r}, {self._target!r})"


class ContainerAdapter:
    """Adapt any container object to the core ServiceContainer protocol."""

    def __init__(
        self,
        container: Any,
        *,
        config: Any | None = None,
        paths: Paths | None = None,
    ) -> None:
        self._raw = container
        self._config = config
        self._paths = paths
        self._bus_cache: Any | None = None
        self._bus_resolved = False

    # -- introspection ---------------------------------------------------
    @property
    def raw(self) -> Any:
        """The underlying container object (used only for capability probes)."""
        return self._raw

    @property
    def config(self) -> Config:
        if self._config is not None:
            return self._config  # type: ignore[no-any-return]
        value = getattr(self._raw, "config", None)
        if value is None:
            raise ServiceUnavailable("the daemon configuration is not available")
        return value  # type: ignore[no-any-return]

    @property
    def paths(self) -> Paths:
        if self._paths is not None:
            return self._paths
        value = getattr(self._raw, "paths", None)
        if value is None:
            raise ServiceUnavailable("the daemon paths are not available")
        return value  # type: ignore[no-any-return]

    @property
    def event_bus(self) -> Any | None:
        """Return the container's :class:`EventBus`, if it exposes one."""
        if self._bus_resolved:
            return self._bus_cache
        self._bus_resolved = True
        for attr in ("event_bus", "bus"):
            try:
                value = getattr(self._raw, attr, None)
            except AttributeError:  # pragma: no cover - defensive
                value = None
            if value is None:
                continue
            if callable(value) and not isinstance(value, EventBus):
                try:
                    value = value()
                except Exception:  # pragma: no cover - container specific
                    logger.debug("container event bus getter failed", exc_info=True)
                    continue
            if hasattr(value, "subscribe") and hasattr(value, "emit"):
                self._bus_cache = value
                return value
        return None

    # -- raw access ------------------------------------------------------
    def raw_service(self, name: str) -> Any | None:
        """Return the raw service object for *name*, or None when absent."""
        return self._resolve(name, required=False)

    def has_service(self, name: str) -> bool:
        return self.raw_service(name) is not None

    def probe(self, service_name: str, method: str) -> Any | None:
        """Return a callable method of a service, or None when it is missing."""
        target = self.raw_service(service_name)
        if target is None:
            return None
        candidate = getattr(target, method, None)
        return candidate if callable(candidate) else None

    def _resolve(self, name: str, *, required: bool = True) -> Any | None:
        try:
            attr = getattr(self._raw, name, None)
        except AttributeError:  # pragma: no cover - defensive
            attr = None
        if attr is None:
            return self._missing(name) if required else None
        target = attr
        # Protocol getters are methods: call them.  Stubs may also expose the
        # service directly as an attribute, which is supported as well.
        if callable(attr) and not isinstance(attr, (type, str, bytes)):
            try:
                target = attr()
            except AvdFleetError:
                raise
            except Exception as exc:  # noqa: BLE001 - service construction failure
                if required:
                    raise ServiceUnavailable(
                        f"the {name} service failed to initialise",
                        service=name,
                        hint="check the daemon log for details",
                    ) from exc
                logger.debug("optional service %s failed to initialise", name, exc_info=True)
                return None
        if target is None:
            return self._missing(name) if required else None
        return target

    def _missing(self, name: str) -> Any:
        raise ServiceUnavailable(
            f"the {name} service is not available in this daemon",
            service=name,
            hint="this capability is not enabled in the current build or configuration",
        )

    def _service(self, name: str) -> BoundService:
        return BoundService(name, self._resolve(name))

    # -- core ServiceContainer protocol ---------------------------------
    def doctor(self) -> BoundService:
        return self._service("doctor")

    def installer(self) -> BoundService:
        return self._service("installer")

    def images(self) -> BoundService:
        return self._service("images")

    def avds(self) -> BoundService:
        return self._service("avds")

    def runtime(self) -> BoundService:
        return self._service("runtime")

    def proxies(self) -> BoundService:
        return self._service("proxies")

    def assignments(self) -> BoundService:
        return self._service("assignments")

    def bridge(self) -> BoundService:
        return self._service("bridge")

    def oobe(self) -> BoundService:
        return self._service("oobe")

    def scripts(self) -> BoundService:
        return self._service("scripts")

    def appium(self) -> BoundService:
        return self._service("appium")

    def screen(self) -> BoundService:
        return self._service("screen")

    def fleet(self) -> BoundService:
        return self._service("fleet")

    def portable(self) -> BoundService:
        return self._service("portable")

    # -- extra (duck-typed) services ------------------------------------
    def verifier(self) -> BoundService:
        return self._service("verifier")

    def host(self) -> BoundService:
        return self._service("host")

    def repository(self) -> BoundService:
        return self._service("repository")

    # -- lifecycle -------------------------------------------------------
    def close(self) -> Any:
        """Close the underlying container, if it supports it."""
        closer = getattr(self._raw, "close", None)
        if not callable(closer):
            return None
        return closer()

    async def aclose(self) -> None:
        """Await the container close when it is asynchronous."""
        result = self.close()
        if inspect.isawaitable(result):
            await result

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"ContainerAdapter({self._raw!r})"


def create_adapter(
    container: Any,
    *,
    config: Any | None = None,
    paths: Paths | None = None,
) -> ContainerAdapter:
    """Wrap *container* in a :class:`ContainerAdapter` (idempotent)."""
    if isinstance(container, ContainerAdapter):
        return container
    return ContainerAdapter(container, config=config, paths=paths)


def event_bus_from(container: Any | None) -> EventBus | None:
    """Best-effort extraction of an EventBus from a raw container."""
    if container is None:
        return None
    if isinstance(container, ContainerAdapter):
        bus = container.event_bus
        return bus if isinstance(bus, EventBus) or bus is not None else None
    for attr in ("event_bus", "bus"):
        value = getattr(container, attr, None)
        if value is None:
            continue
        if callable(value) and not isinstance(value, EventBus):
            try:
                value = value()
            except Exception:  # pragma: no cover - container specific
                continue
        if hasattr(value, "subscribe") and hasattr(value, "emit"):
            return value  # type: ignore[no-any-return]
    return None


def build_default_container(config: Config, paths: Paths) -> Any:
    """Build the production service container.

    Imported lazily by design: ``avdfleet.container`` is provided by the
    root/integration wiring and is *not* required to run the API tests, the CLI
    or documentation tooling.  The expected signature is
    ``build_container(config: Config, paths: Paths)``; keyword arguments are
    passed only when the callable actually accepts them.
    """
    try:
        from avdfleet.container import build_container  # noqa: PLC0415 - deliberately lazy
    except ImportError as exc:  # pragma: no cover - depends on integration wiring
        raise RuntimeError(
            "no service container was injected and avdfleet.container.build_container "
            "is not available; call create_app(container=...) or install the full daemon"
        ) from exc
    if not callable(build_container):
        raise RuntimeError("avdfleet.container.build_container is not callable")
    kwargs: dict[str, Any] = {}
    parameters: Mapping[str, Any] = {}
    try:
        parameters = inspect.signature(build_container).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins/C functions
        parameters = {}
    if "config" in parameters or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        kwargs["config"] = config
    if "paths" in parameters or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        kwargs["paths"] = paths
    try:
        container = build_container(**kwargs)
    except TypeError:  # pragma: no cover - legacy positional signature
        container = build_container(config, paths)
    if container is None:  # pragma: no cover - defensive
        raise RuntimeError("avdfleet.container.build_container returned None")
    return container


__all__ = [
    "EXTRA_GETTERS",
    "SERVICE_GETTERS",
    "BoundService",
    "ContainerAdapter",
    "ServiceContainerProtocol",
    "ServiceUnavailable",
    "UnsupportedOperation",
    "build_default_container",
    "create_adapter",
    "event_bus_from",
]
