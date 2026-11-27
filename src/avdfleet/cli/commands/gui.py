"""avdfleet gui - open the web GUI, starting the daemon when necessary.

``avdfleet gui`` is the operator-facing launcher from BRIEF section2: probe
``/health`` on the configured/requested host:port, start the *same* uvicorn
daemon ``avdfleet serve`` runs (in a background daemon thread) when nothing is
listening, poll ``/health`` until it is ready, then open
``http://host:port/`` in the default browser and keep the process in the
foreground until Ctrl+C.

The API token value is never printed.  With ``--json`` the browser is not
opened (machine-readable output only); use ``--no-browser`` to keep the daemon
running without opening a browser.

The network-facing pieces are small, patchable functions (``_probe_health``,
``_start_background_server``, ``_wait_until_healthy``, ``_open_browser``,
``_wait_for_shutdown``) so the CLI tests can exercise the flow without binding
a real socket.
"""

import json
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from typing import Any

import typer

from avdfleet.core.logging import redact_text

from ..context import get_context
from ..errors import EXIT_ENVIRONMENT, CliEnvironmentError, CliUsageError, command_errors
from ..output import json_flag
from . import serve as serve_module

#: Unauthenticated liveness endpoint served by the daemon.
HEALTH_PATH = "/health"

#: Short timeout for one probe; a local daemon answers immediately.
HEALTH_PROBE_TIMEOUT_S = 1.0

#: How long ``avdfleet gui`` waits for a freshly started daemon to answer.
DAEMON_READY_TIMEOUT_S = 30.0

#: Poll interval while waiting for /health.
DAEMON_READY_POLL_S = 0.2

#: One-time browser login endpoint (authenticated issue, unauthenticated redeem).
BOOTSTRAP_CODE_PATH = "/api/v1/auth/bootstrap-code"
BOOTSTRAP_PAGE_PATH = "/auth/bootstrap"

#: Timeout for the local token exchange; a loopback daemon answers immediately.
BOOTSTRAP_REQUEST_TIMEOUT_S = 10.0


class _BackgroundServer:
    """A daemon running uvicorn in a daemon thread (so Ctrl+C stays on the main thread)."""

    def __init__(self, app: Any, *, host: str, port: int) -> None:
        self.app = app
        self.host = host
        self.port = port
        self.errors: list[BaseException] = []
        self._thread = threading.Thread(target=self._serve, name="avdfleet-daemon", daemon=True)

    def _serve(self) -> None:
        try:
            serve_module.run_uvicorn(self.app, host=self.host, port=self.port)
        except BaseException as exc:  # noqa: BLE001 - reported by error_message()/the caller
            self.errors.append(exc)

    def start(self) -> "_BackgroundServer":
        self._thread.start()
        return self

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def error_message(self) -> str:
        """Return the redacted startup/shutdown error text ('' when none)."""
        messages = [redact_text(str(error)) or type(error).__name__ for error in self.errors]
        return "; ".join(message for message in messages if message)


def _probe_health(url: str, *, timeout_s: float = HEALTH_PROBE_TIMEOUT_S) -> bool:
    """Return True when ``<url>/health`` answers with a 2xx status."""
    endpoint = url.rstrip("/") + HEALTH_PATH
    request = urllib.request.Request(endpoint, headers={"Accept": "application/json"})
    # Never route a loopback liveness probe through a configured proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=max(0.05, float(timeout_s))) as response:
            status = int(getattr(response, "status", 0) or 0)
            return 200 <= status < 300
    except Exception:  # noqa: BLE001 - any failure means "not healthy yet"
        return False


def _build_daemon_app(context: Any) -> Any:
    """Build the FastAPI app for the daemon using the serve implementation."""
    return serve_module.create_daemon_app(context.config, context.container(), context.paths)


def _start_background_server(app: Any, *, host: str, port: int) -> _BackgroundServer:
    """Start uvicorn in a daemon thread and return its handle."""
    return _BackgroundServer(app, host=host, port=port).start()


def _wait_until_healthy(
    url: str,
    *,
    timeout_s: float = DAEMON_READY_TIMEOUT_S,
    server: _BackgroundServer | None = None,
) -> bool:
    """Poll /health until it answers, the server dies, or *timeout_s* expires."""
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        if _probe_health(url):
            return True
        if server is not None and not server.is_alive():
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(DAEMON_READY_POLL_S)


def _open_browser(url: str) -> bool:
    """Open *url* in the default browser; return False when that fails."""
    try:
        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001 - a headless host has no browser
        return False


def _daemon_token(paths: Any) -> str | None:
    """Return the bearer token used by the local daemon, or None when unavailable."""
    try:
        from avdfleet.api.auth import TokenStore  # noqa: PLC0415 - keep CLI startup cheap

        return TokenStore(paths).ensure()
    except Exception:  # noqa: BLE001 - absent/unreadable token means manual login
        return None


def _request_bootstrap_code(
    base_url: str,
    token: str,
    *,
    timeout_s: float = BOOTSTRAP_REQUEST_TIMEOUT_S,
) -> tuple[str | None, str]:
    """Ask a running daemon for a one-time browser login code.

    Returns ``(code, error)``.  The bearer token is sent in the Authorization
    header and never printed or embedded in the request URL.
    """
    endpoint = base_url.rstrip("/") + BOOTSTRAP_CODE_PATH
    body = json.dumps({}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=max(0.2, float(timeout_s))) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            error_payload = json.loads(exc.read().decode("utf-8") or "{}")
            error = error_payload.get("error") if isinstance(error_payload, dict) else None
            detail = str((error or {}).get("message") or "") if isinstance(error, dict) else ""
        except Exception:  # noqa: BLE001 - keep the message generic
            detail = ""
        return None, redact_text(detail or f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001 - fall back to manual login
        return None, redact_text(str(exc)) or type(exc).__name__
    code = payload.get("code") if isinstance(payload, dict) else None
    if not isinstance(code, str) or not code.strip():
        return None, "the daemon did not return a bootstrap code"
    return code.strip(), ""


def _browser_url(base_url: str, *, token: str | None) -> tuple[str, str]:
    """Return ``(url, warning)`` for opening the GUI with automatic sign-in.

    Falls back to the normal GUI URL (manual token paste) when no code can be
    obtained.  The token itself is never placed in the URL.
    """
    fallback = f"{base_url}/"
    if not token:
        return fallback, "could not read the daemon token for automatic sign-in; the Connect screen is shown"
    code, error = _request_bootstrap_code(base_url, token)
    if not code:
        detail = f": {error}" if error else ""
        return fallback, f"could not create a browser sign-in link{detail}; the Connect screen is shown"
    return f"{base_url.rstrip('/')}{BOOTSTRAP_PAGE_PATH}#code={code}", ""


def _wait_for_shutdown(server: _BackgroundServer) -> bool:
    """Block until Ctrl+C; return True when the user interrupted the daemon."""
    try:
        while server.is_alive():
            server.join(0.5)
    except KeyboardInterrupt:
        return True
    return False


def gui(
    ctx: typer.Context,
    host: str = typer.Option(None, "--host", help="Daemon bind address (default: configuration api_host)."),
    port: int = typer.Option(None, "--port", help="Daemon port (default: configuration api_port)."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open the web GUI in a browser."),
    no_auto_auth: bool = typer.Option(
        False,
        "--no-auto-auth",
        help="Open the normal Connect screen instead of a one-time browser sign-in link.",
    ),
    json_output: bool = json_flag(),
) -> None:
    """Start the daemon when needed and open the web GUI in the default browser."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    config = context.config
    paths = context.paths
    bind_host, bind_port = serve_module.resolve_binding(host, port, config)
    base_url = serve_module.display_url(bind_host, bind_port)
    url = f"{base_url}/"

    already_running = _probe_health(base_url)
    server: _BackgroundServer | None = None
    if not already_running:
        if not serve_module.is_loopback_host(bind_host):
            raise CliUsageError(
                f"avdfleet gui will not start the daemon on the non-loopback address {bind_host}",
                hint=(
                    "start it explicitly with 'avdfleet serve --host "
                    f"{bind_host} --port {bind_port} --allow-remote' and then run 'avdfleet gui'"
                ),
            )
        app = _build_daemon_app(context)
        server = _start_background_server(app, host=bind_host, port=bind_port)
        if not _wait_until_healthy(base_url, timeout_s=DAEMON_READY_TIMEOUT_S, server=server):
            detail = server.error_message()
            message = (
                f"the daemon did not become healthy at {base_url} "
                f"within {int(DAEMON_READY_TIMEOUT_S)}s"
            )
            if detail:
                message = f"{message}: {detail}"
            raise CliEnvironmentError(
                message,
                hint="run 'avdfleet serve' to see the daemon log, then retry 'avdfleet gui'",
            )

    opened = False
    browser_url = url
    auth_warning = ""
    auto_authenticated = False
    if not no_browser and not out.json_output:
        if not no_auto_auth and serve_module.is_loopback_host(bind_host):
            browser_url, auth_warning = _browser_url(base_url, token=_daemon_token(paths))
            auto_authenticated = not auth_warning
        elif not no_auto_auth:
            auth_warning = (
                "automatic sign-in is restricted to loopback daemons; the Connect screen is shown"
            )
        opened = _open_browser(browser_url)
    payload = {
        "url": url,
        "host": bind_host,
        "port": bind_port,
        "started": server is not None,
        "already_running": already_running,
        "opened": opened,
        "auto_auth": auto_authenticated,
        "healthy": True,
        "docs_url": f"{base_url}/docs",
        "data_dir": str(paths.data_dir),
    }

    def render() -> None:
        out.success(f"AVD Fleet web GUI: {url}")
        if already_running:
            out.info("daemon already running; using the existing instance")
        else:
            out.info(f"daemon started in the background (data: {paths.data_dir})")
        if no_browser:
            out.info("browser not opened (--no-browser); open the URL above")
        elif out.json_output:
            out.info("browser not opened (--json); open the URL above")
        elif not opened:
            out.warning(f"could not open the default browser; open {url} manually")
        elif auto_authenticated:
            out.info("browser opened with a one-time sign-in link")
        elif no_auto_auth:
            out.info("browser opened; paste the token from the Connect screen prompt")
        if auth_warning:
            out.warning(auth_warning)
        if server is not None:
            out.info("Press Ctrl+C to stop the daemon.")

    out.emit(payload, render)

    if server is None:
        return
    interrupted = _wait_for_shutdown(server)
    if interrupted:
        if not out.json_output:
            out.info("daemon stopped")
        return
    message = server.error_message() or "the daemon stopped unexpectedly"
    out.error(redact_text(message))
    raise typer.Exit(EXIT_ENVIRONMENT)


def register(app: typer.Typer) -> None:
    app.command("gui")(command_errors("gui")(gui))


__all__ = ["gui", "register"]
