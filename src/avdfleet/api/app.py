"""FastAPI application factory for the AVD Fleet daemon.

``create_app`` composes the REST routers, the WebSocket endpoints, the security
middleware and the static UI hosting.  It deliberately never imports concrete
service modules: everything reaches the daemon through ``app.state.container``
(a :class:`avdfleet.core.interfaces.ServiceContainer`, wrapped in
:class:`avdfleet.api.container.ContainerAdapter`).

Injection contract
------------------
* ``config``  — :class:`avdfleet.core.config.Config`; loaded with ``Config.load()``
  when omitted (env/config.toml precedence).
* ``paths``   — :class:`avdfleet.core.paths.Paths`; derived from the config or the
  container when omitted.
* ``container`` — a core ``ServiceContainer`` implementation or a duck-typed stub.
  When omitted the lifespan imports ``avdfleet.container.build_container`` lazily
  (integration wiring lives there) and builds it at startup.

The lifespan binds the event bus to the running loop, creates the API token when
authentication is enabled, and closes the bus and container on shutdown.

Security posture
----------------
* Bearer-token auth (see :mod:`avdfleet.api.auth`) on every ``/api/v1`` route.
* CORS restricted to ``config.allow_origins``; ``TrustedHostMiddleware`` restricted
  to ``config.trusted_hosts`` (loopback by default).
* ``X-Request-ID`` correlation, ``nosniff``/frame-deny/referrer-policy headers on
  every response (including errors), no stack traces in bodies.
* OpenAPI/docs are served only when enabled — default on for development, turn
  off with ``AVDFLEET_DOCS=0`` (or a ``docs_enabled=False`` attribute on an
  injected config object).
* The static UI is served at ``/`` only when ``config.serve_ui`` is true and an
  ``index.html`` build exists; otherwise ``/`` returns a small JSON banner.  The
  catch-all never shadows ``/api``, ``/health`` or the WebSocket routes.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from avdfleet.core.config import API_PREFIX, APP_VERSION, Config
from avdfleet.core.errors import AvdFleetError, NotFoundError, UnauthorizedError
from avdfleet.core.events import EventBus
from avdfleet.core.paths import Paths

from .auth import BootstrapCodeStore, TokenStore, TokenStoreError
from .container import ContainerAdapter, build_default_container, create_adapter, event_bus_from
from .deps import require_auth
from .errors import apply_common_headers, common_headers, install_exception_handlers
from .routes import ROUTERS
from .ws import router as ws_router

logger = logging.getLogger("avdfleet.api.app")

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_API_DESCRIPTION = """\
REST API for the AVD Fleet daemon.

All ``/api/v1`` routes require a bearer token unless ``auth_required`` is
disabled.  The token is stored in the data directory with owner-only
permissions.  WebSocket clients pass the token as ``?token=`` and must send an
allowed ``Origin``.

Secrets are never returned: proxy passwords are always masked and error bodies
are redacted.  Destructive operations accept ``dry_run`` where documented.
"""

_OPENAPI_TAGS = [
    {"name": "system", "description": "Health, configuration, host information and doctor."},
    {"name": "images", "description": "System-image catalogue, downloads and custom imports."},
    {"name": "oobe", "description": "Out-of-box-experience verification and reset."},
    {"name": "templates", "description": "AVD templates and device profiles."},
    {"name": "instances", "description": "AVD lifecycle, snapshots, capacity and screen."},
    {"name": "proxies", "description": "Authenticated proxies, assignments and bridges (secrets masked)."},
    {"name": "scripts", "description": "Script definitions and fan-out runs."},
    {"name": "appium", "description": "Appium server lifecycle and capabilities."},
    {"name": "fleet", "description": "fleet.yaml plan and apply."},
    {"name": "portable", "description": "Portable-edition status and preflight checks."},
    {"name": "events", "description": "Recent daemon events."},
]

#: Tiny same-origin page used by ``avdfleet gui`` to auto-authenticate a browser.
#: The one-time code travels in the URL fragment (never sent to the server) and is
#: exchanged for the bearer token, which is stored in localStorage and never
#: rendered or placed in a URL.
_BOOTSTRAP_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="referrer" content="no-referrer">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AVD Fleet - signing in</title>
<style>
  body { font-family: system-ui, sans-serif; background: #0d1117; color: #e6edf3;
         display: grid; place-items: center; min-height: 100vh; margin: 0; }
  main { max-width: 34rem; padding: 2rem; }
</style>
</head>
<body>
<main>
  <h1>AVD Fleet</h1>
  <p id="status">Signing in...</p>
</main>
<script>
(async () => {
  const status = document.getElementById('status');
  const params = new URLSearchParams(window.location.hash.slice(1));
  const code = params.get('code');
  if (!code) {
    status.textContent = 'No sign-in code in this link. Open / and paste the token from the data directory.';
    return;
  }
  try {
    const response = await fetch('/api/v1/auth/bootstrap', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code})
    });
    if (!response.ok) {
      throw new Error('HTTP ' + response.status);
    }
    const payload = await response.json();
    if (!payload || typeof payload.token !== 'string' || !payload.token) {
      throw new Error('missing token');
    }
    window.localStorage.setItem('avdfleet.token', payload.token);
    history.replaceState(null, '', '/');
    window.location.replace('/');
  } catch (error) {
    status.textContent = 'This sign-in link is invalid, expired or already used. Open / and paste the token from the data directory.';
  }
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def docs_enabled(config: Any, environ: Mapping[str, str] | None = None) -> bool:
    """Return whether OpenAPI/docs should be exposed (default: yes, dev-friendly)."""
    env = os.environ if environ is None else environ
    raw = env.get("AVDFLEET_DOCS")
    if raw is not None and raw.strip():
        return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    for attribute in ("api_docs", "docs_enabled"):
        value = getattr(config, attribute, None)
        if isinstance(value, bool):
            return value
    return True


def sanitize_request_id(value: str | None) -> str:
    """Accept a well-formed client correlation id, otherwise mint a fresh one."""
    if value:
        candidate = value.strip()
        if _REQUEST_ID_PATTERN.match(candidate):
            return candidate
    return uuid.uuid4().hex


def _resolve_static_file(static_dir: Path | None, relative: str) -> Path | None:
    """Resolve *relative* under *static_dir*, refusing traversal/odd paths."""
    if static_dir is None or not relative:
        return None
    try:
        root = static_dir.resolve()
        candidate = (root / relative).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if candidate == root or not candidate.is_relative_to(root):
        return None
    try:
        return candidate if candidate.is_file() else None
    except OSError:  # pragma: no cover - filesystem failure
        return None


def _ui_banner(docs: bool) -> dict[str, Any]:
    banner: dict[str, Any] = {
        "name": "AVD Fleet",
        "version": APP_VERSION,
        "status": "ok",
        "message": (
            "The web UI build is not installed in this environment; the REST API "
            f"is available under {API_PREFIX}."
        ),
        "api": API_PREFIX,
    }
    if docs:
        banner["docs"] = "/docs"
        banner["openapi"] = "/openapi.json"
    else:
        banner["docs"] = None
    return banner


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


def _make_lifespan() -> Any:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.start_monotonic = time.monotonic()
        config = app.state.config
        paths = app.state.paths
        if app.state.container is None:
            raw = build_default_container(config, paths)
            app.state.container = create_adapter(raw, config=config, paths=paths)
            if app.state.event_bus is None:
                app.state.event_bus = event_bus_from(raw)
        if app.state.event_bus is None:
            # No container bus was exposed: run a private bus so /ws/events and
            # /api/v1/events still work for system-level notifications.
            app.state.event_bus = EventBus()
        bus = app.state.event_bus
        if bus is not None:
            bind = getattr(bus, "bind_loop", None)
            if callable(bind):
                try:
                    bind()
                except Exception:  # pragma: no cover - exotic bus implementation
                    logger.warning("could not bind the event bus to the running loop", exc_info=True)
        if bool(getattr(config, "auth_required", True)):
            try:
                app.state.token_store.ensure()
            except TokenStoreError as exc:
                # Fail closed: a daemon that cannot authenticate must not serve.
                logger.error("API token could not be initialised: %s", exc.message)
                raise
        try:
            yield
        finally:
            bus = app.state.event_bus
            if bus is not None:
                close_bus = getattr(bus, "close", None)
                if callable(close_bus):
                    try:
                        close_bus()
                    except Exception:  # pragma: no cover - bus close failure
                        logger.warning("failed to close the event bus", exc_info=True)
            container = app.state.container
            if isinstance(container, ContainerAdapter):
                try:
                    await container.aclose()
                except Exception:  # pragma: no cover - container close failure
                    logger.warning("failed to close the service container", exc_info=True)

    return lifespan


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_app(
    config: Config | None = None,
    container: Any | None = None,
    paths: Paths | None = None,
    *,
    bootstrap_codes: Any | None = None,
) -> FastAPI:
    """Build the daemon's FastAPI application (see the module docstring).

    ``bootstrap_codes`` is an optional :class:`BootstrapCodeStore`; when omitted
    a fresh in-memory store is used.  ``avdfleet gui`` obtains a one-time code
    through the authenticated endpoint and opens the auto-login page.
    """
    resolved_config: Any = config if config is not None else Config.load()

    adapter: ContainerAdapter | None = None
    if container is not None:
        adapter = create_adapter(container, config=resolved_config)

    resolved_paths = paths
    if resolved_paths is None and adapter is not None:
        try:
            resolved_paths = adapter.paths
        except AvdFleetError:
            resolved_paths = None
    if resolved_paths is None:
        resolve = getattr(resolved_config, "resolved_paths", None)
        resolved_paths = resolve() if callable(resolve) else None
    if resolved_paths is None:  # pragma: no cover - duck-typed config without paths
        resolved_paths = Paths.discover()

    docs = docs_enabled(resolved_config)
    app = FastAPI(
        title="AVD Fleet API",
        version=APP_VERSION,
        description=_API_DESCRIPTION,
        openapi_tags=_OPENAPI_TAGS,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
        lifespan=_make_lifespan(),
    )

    # -- state ------------------------------------------------------
    app.state.config = resolved_config
    app.state.paths = resolved_paths
    app.state.container = adapter
    app.state.raw_container = container
    app.state.token_store = TokenStore(resolved_paths)
    app.state.bootstrap_codes = bootstrap_codes or BootstrapCodeStore()
    initial_bus = event_bus_from(container) if container is not None else None
    # When no container is injected the lifespan builds it and adopts its bus;
    # an explicit ``None`` here means "resolve during startup".
    app.state.event_bus = initial_bus
    app.state.start_monotonic = time.monotonic()
    app.state.static_dir = Path(__file__).resolve().parent / "static"
    app.state.serve_ui = bool(getattr(resolved_config, "serve_ui", True))
    app.state.docs_enabled = docs

    # -- middleware -------------------------------------------------
    # Order: request-context (outermost) -> trusted host -> CORS -> app.
    # ``request_context`` is registered last so error/warning responses from the
    # host filter still carry security and correlation headers.
    origins = [str(origin) for origin in (getattr(resolved_config, "allow_origins", []) or []) if origin]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Accept", REQUEST_ID_HEADER],
            expose_headers=[REQUEST_ID_HEADER],
            max_age=600,
        )
    trusted_hosts = [str(host) for host in (getattr(resolved_config, "trusted_hosts", []) or []) if host]
    if trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts, www_redirect=False)

    @app.middleware("http")
    async def _request_context(request: Request, call_next: Any) -> Response:
        request.state.request_id = sanitize_request_id(request.headers.get(REQUEST_ID_HEADER))
        response = await call_next(request)
        apply_common_headers(request, response)
        return response  # type: ignore[no-any-return]

    # -- errors and routes -----------------------------------------
    install_exception_handlers(app)
    for router in ROUTERS:
        app.include_router(router)
    app.include_router(ws_router)

    # -- browser auto-login (one-time code, never exposes the token in a URL) ----
    @app.get("/auth/bootstrap", include_in_schema=False)
    def bootstrap_page() -> HTMLResponse:
        """Serve the one-time-code auto-login page opened by ``avdfleet gui``."""
        return HTMLResponse(
            _BOOTSTRAP_PAGE,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
            },
        )

    @app.post(
        "/api/v1/auth/bootstrap-code",
        include_in_schema=False,
        dependencies=[Depends(require_auth)],
    )
    def issue_bootstrap_code(request: Request) -> JSONResponse:
        """Issue a one-time browser login code (requires the bearer token)."""
        store = getattr(request.app.state, "bootstrap_codes", None)
        if store is None:
            raise NotFoundError("browser auto-login is not enabled")
        code = store.issue()
        return JSONResponse(
            {"code": code, "expires_in": int(store.ttl_s)},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/v1/auth/bootstrap", include_in_schema=False)
    async def redeem_bootstrap_code(request: Request) -> JSONResponse:
        """Exchange a one-time code for the bearer token (loopback browser only)."""
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001 - malformed JSON is a bad code
            raise UnauthorizedError("bootstrap code is invalid or expired") from exc
        code = payload.get("code") if isinstance(payload, dict) else None
        store = getattr(request.app.state, "bootstrap_codes", None)
        if store is None or not store.redeem(code):
            raise UnauthorizedError(
                "bootstrap code is invalid or expired",
                hint="run 'avdfleet gui' again to open a fresh sign-in link",
            )
        token = request.app.state.token_store.ensure()
        return JSONResponse(
            {"token": token},
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    # -- static UI / SPA fallback (registered last: never shadows API) --
    @app.get("/{full_path:path}", include_in_schema=False)
    async def _serve_ui(full_path: str, request: Request) -> Response:
        relative = (full_path or "").strip("/")
        if relative == "api" or relative.startswith("api/"):
            raise NotFoundError("the requested API endpoint does not exist")
        static_dir: Path | None = getattr(request.app.state, "static_dir", None)
        if getattr(request.app.state, "serve_ui", False):
            resolved = _resolve_static_file(static_dir, relative)
            if resolved is not None:
                return FileResponse(resolved)
            index = _resolve_static_file(static_dir, "index.html")
            if index is not None:
                return FileResponse(index)
        if not relative:
            return JSONResponse(
                _ui_banner(bool(getattr(request.app.state, "docs_enabled", True))),
                headers=common_headers(request),
            )
        raise NotFoundError("not found")

    logger.debug(
        "AVD Fleet API app created (version=%s, docs=%s, static=%s)",
        APP_VERSION,
        docs,
        app.state.static_dir,
    )
    return app


__all__ = ["create_app", "docs_enabled", "sanitize_request_id"]
