"""avdfleet serve - run the FastAPI daemon (REST + WebSocket + UI).

The application factory (:func:`avdfleet.api.app.create_app`) and uvicorn are
imported lazily so the rest of the CLI stays fast.  The command prints the URL
and the token **path** (never the token itself), opens the GUI when running on
an interactive terminal, and exits cleanly on SIGINT/Ctrl+C.

Security: the daemon binds loopback by default (BRIEF section2).  Binding a
non-loopback address requires ``--allow-remote``, prints a warning and accepts
``--tls-cert``/``--tls-key`` so uvicorn can terminate HTTPS.  The helpers in
this module are reused by :mod:`avdfleet.cli.commands.gui`, so ``avdfleet gui``
starts exactly the same daemon as ``avdfleet serve``.
"""

import copy
import ipaddress
import logging
import re
import threading
import webbrowser
from pathlib import Path
from typing import Any

import typer

from avdfleet.core.config import APP_VERSION, Config
from avdfleet.core.logging import RedactingFilter

from ..context import get_context
from ..errors import CliUsageError, command_errors
from ..output import json_flag, jsonable

#: Names accepted as loopback in addition to the loopback IP ranges.
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}

#: Bind addresses that mean "every interface" (never loopback themselves).
_WILDCARD_HOSTS = {"0.0.0.0", "::", "[::]"}


def is_loopback_host(host: str) -> bool:
    """Return True when *host* names a loopback interface only."""
    candidate = (host or "").strip().strip("[]")
    if not candidate:
        return True
    if candidate.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def resolve_binding(host: str | None, port: int | None, config: Any) -> tuple[str, int]:
    """Return (bind_host, bind_port) from CLI overrides and the configuration."""
    bind_host = str(host or config.api_host)
    try:
        bind_port = int(port if port else config.api_port)
    except (TypeError, ValueError) as exc:
        raise CliUsageError("--port must be an integer between 1 and 65535") from exc
    if not 1 <= bind_port <= 65535:
        raise CliUsageError("--port must be between 1 and 65535")
    return bind_host, bind_port


def display_url(bind_host: str, bind_port: int, *, tls: bool = False) -> str:
    """Return the URL clients use to reach the daemon bound to *bind_host*."""
    scheme = "https" if tls else "http"
    display_host = "127.0.0.1" if bind_host in _WILDCARD_HOSTS else bind_host
    return f"{scheme}://{display_host}:{bind_port}"


def _url_host(host: str) -> str:
    """Format a bind host for use in an Origin/URL (IPv6 needs brackets)."""
    value = str(host).strip()
    if value.startswith("[") and value.endswith("]"):
        return value
    if ":" in value:
        return f"[{value}]"
    return value


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def resolve_security_allowlists(
    config: Config,
    bind_host: str,
    bind_port: int,
    *,
    tls: bool = False,
    trusted_hosts: list[str] | None = None,
    trusted_origins: list[str] | None = None,
) -> Config:
    """Return a config whose Host/Origin allowlists match the actual bind.

    A concrete bind host is trusted automatically and gets its exact origin, so
    ``--allow-remote`` works without a second manual allowlist.  Wildcard binds
    cannot be trusted automatically (that would trust every Host header), so at
    least one explicit non-loopback ``--trusted-host`` is required.  The same
    derivation fixes loopback on a non-default port, where the browser Origin
    otherwise never matched the defaults.
    """
    hosts = _dedupe([str(item) for item in (config.trusted_hosts or [])])
    hosts.extend(str(item) for item in (trusted_hosts or []) if str(item).strip())
    hosts = _dedupe(hosts)
    origins = _dedupe([str(item) for item in (config.allow_origins or [])])
    origins.extend(str(item) for item in (trusted_origins or []) if str(item).strip())

    wildcard = bind_host in _WILDCARD_HOSTS
    if wildcard:
        explicit_non_loopback = [host for host in hosts if not is_loopback_host(host)]
        if not explicit_non_loopback:
            raise CliUsageError(
                "refusing to bind to a wildcard address without an explicit trusted host",
                hint="pass --trusted-host <name-or-ip> (repeatable) so Host-header "
                "validation and the browser Origin allowlist are explicit",
            )
        # A wildcard bind is usually reached through loopback in a browser on the
        # same machine; without this the GUI WebSocket gets 403 on a non-default port.
        origin_hosts = [*explicit_non_loopback, "127.0.0.1", "localhost"]
    else:
        hosts.append(bind_host)
        origin_hosts = [bind_host]
        if is_loopback_host(bind_host):
            origin_hosts.extend(["127.0.0.1", "localhost"])

    scheme = "https" if tls else "http"
    for host in origin_hosts:
        if not host:
            continue
        origins.append(f"{scheme}://{_url_host(host)}:{bind_port}")
    origins = _dedupe(origins)
    return config.model_copy(update={"trusted_hosts": hosts, "allow_origins": origins})


def _resolve_tls(tls_cert: Path | None, tls_key: Path | None) -> tuple[Path | None, Path | None]:
    """Validate the --tls-cert/--tls-key pair and return normalised paths."""
    if tls_cert is None and tls_key is None:
        return None, None
    if tls_cert is None or tls_key is None:
        raise CliUsageError(
            "--tls-cert and --tls-key must be passed together",
            hint="uvicorn needs both the certificate and its private key for HTTPS",
        )
    cert = Path(tls_cert)
    key = Path(tls_key)
    for option, path in (("--tls-cert", cert), ("--tls-key", key)):
        if not path.is_file():
            raise CliUsageError(
                f"{option} file not found: {path}",
                hint="pass the path to an existing PEM file",
            )
    return cert, key


def create_daemon_app(config: Config, container: Any, paths: Any) -> Any:
    """Build the FastAPI application for the daemon (lazy import)."""
    from avdfleet.api.app import create_app  # noqa: PLC0415 - lazy import keeps the CLI light

    return create_app(config=config, container=container, paths=paths)


#: Query-string credentials that must never reach a log sink.  The shared
#: ``RedactingFilter`` already masks ``?token=``; uvicorn also logs the raw
#: WebSocket request line, including the single-use ``?ticket=`` used by the GUI.
_WS_QUERY_SECRET = re.compile(r"(?i)([?&](?:ticket|token)=)[^&\s\"']+")


class UvicornRedactionFilter(logging.Filter):
    """Scrub daemon credentials from uvicorn's own log output.

    uvicorn writes the WebSocket request line at INFO on ``uvicorn.error`` even
    with ``access_log=False``, and that line carries the full query string.  This
    filter wraps the project's :class:`~avdfleet.core.logging.RedactingFilter`
    (tokens, passwords, authorization headers, …) and additionally masks
    ``?ticket=``/``?token=`` values.
    """

    def __init__(self, name: str = "") -> None:
        super().__init__(name)
        self._shared = RedactingFilter()

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._shared.filter(record):
            return False
        try:
            message = record.getMessage()
            scrubbed = _WS_QUERY_SECRET.sub(r"\1***", message)
            if scrubbed != message:
                record.msg = scrubbed
                record.args = None
        except Exception:  # pragma: no cover - logging must never raise
            record.msg = "<log message redaction failed>"
            record.args = None
        return True


def uvicorn_log_config() -> dict[str, Any]:
    """Return uvicorn's logging config with the daemon redaction filter attached.

    The filter travels inside the config uvicorn applies while starting the
    server, so it is active before the first log line is written and is never
    installed at import time.
    """
    from uvicorn.config import LOGGING_CONFIG  # noqa: PLC0415 - imported when the server runs

    config: dict[str, Any] = copy.deepcopy(LOGGING_CONFIG)
    filters = config.setdefault("filters", {})
    filters["avdfleet_redact"] = {
        "()": "avdfleet.cli.commands.serve.UvicornRedactionFilter",
    }
    for handler in config.get("handlers", {}).values():
        handler.setdefault("filters", []).append("avdfleet_redact")
    return config


def run_uvicorn(
    app: Any,
    *,
    host: str,
    port: int,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
    access_log: bool = False,
) -> None:
    """Run uvicorn with the daemon defaults plus optional TLS files."""
    import uvicorn  # noqa: PLC0415 - imported only when the server runs

    kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "log_level": "info",
        "access_log": access_log,
        # Redact credentials (token/ticket) before anything reaches a handler,
        # including uvicorn.error's WebSocket request line.
        "log_config": uvicorn_log_config(),
    }
    if tls_cert is not None:
        kwargs["ssl_certfile"] = str(tls_cert)
    if tls_key is not None:
        kwargs["ssl_keyfile"] = str(tls_key)
    uvicorn.run(app, **kwargs)


def _open_browser_later(url: str, delay_s: float = 1.0) -> None:
    timer = threading.Timer(delay_s, lambda: _try_open(url))
    timer.daemon = True
    timer.start()


def _try_open(url: str) -> None:  # pragma: no cover - opens a real browser
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _portable_config(config: Config, from_context: object) -> Config:
    """Return a portable edition config for serve --portable."""
    if config.is_portable:
        return config
    root = Path.cwd()
    try:
        from avdfleet.core.paths import PORTABLE_MARKER  # noqa: PLC0415

        marker = root / PORTABLE_MARKER
        if not marker.exists() and getattr(from_context, "portable", False) is False:
            raise CliUsageError(
                "serve --portable requires a portable bundle root",
                hint="run the launcher from the bundle (AVDFLEET_PORTABLE_ROOT) or set portable_root in config",
            )
    except ImportError:  # pragma: no cover - core always has the constant
        pass
    merged = config.model_dump()
    merged["edition"] = "portable"
    merged.setdefault("portable_root", str(root))
    return Config.model_validate(merged)


def serve(
    ctx: typer.Context,
    host: str = typer.Option(None, "--host", help="Bind address (default: configuration api_host, loopback)."),
    port: int = typer.Option(None, "--port", help="Port (default: configuration api_port)."),
    no_open: bool = typer.Option(False, "--no-open", help="Do not open the web GUI in a browser."),
    portable: bool = typer.Option(False, "--portable", help="Run in portable-bundle mode."),
    allow_remote: bool = typer.Option(
        False,
        "--allow-remote",
        help="Allow binding a non-loopback address (prints a warning; pair with --tls-cert/--tls-key).",
    ),
    tls_cert: Path = typer.Option(None, "--tls-cert", help="TLS certificate (PEM) for HTTPS; requires --tls-key."),
    tls_key: Path = typer.Option(None, "--tls-key", help="TLS private key (PEM) for HTTPS; requires --tls-cert."),
    trusted_host: list[str] = typer.Option(
        None,
        "--trusted-host",
        help="Additional allowed Host header (repeatable). Required for --host 0.0.0.0/::.",
    ),
    trusted_origin: list[str] = typer.Option(
        None,
        "--trusted-origin",
        help="Additional allowed browser Origin (repeatable), e.g. behind a reverse proxy.",
    ),
    json_output: bool = json_flag(),
) -> None:
    """Run the AVD Fleet daemon serving the REST API, WebSocket and web GUI."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    config = context.config
    paths = context.paths
    if portable:
        config = _portable_config(config, paths)
        paths = config.resolved_paths().ensure()
    bind_host, bind_port = resolve_binding(host, port, config)
    cert, key = _resolve_tls(tls_cert, tls_key)
    remote = not is_loopback_host(bind_host)
    if remote:
        if not allow_remote:
            raise CliUsageError(
                f"refusing to bind the daemon to the non-loopback address {bind_host}",
                hint="pass --allow-remote to bind remotely (prefer --tls-cert/--tls-key and a firewall)",
            )
        if not bool(getattr(config, "auth_required", True)):
            raise CliUsageError(
                "refusing to expose the daemon remotely with authentication disabled",
                hint="enable auth_required, bind loopback and use an SSH tunnel, or put an "
                "authenticating reverse proxy in front of the daemon",
            )
        out.warning(
            f"binding the daemon to the non-loopback address {bind_host}; "
            "the bearer token is still required but the network may expose the API"
        )
        if cert is None:
            out.warning("traffic is not encrypted: pass --tls-cert/--tls-key for HTTPS on untrusted networks")
    config = resolve_security_allowlists(
        config,
        bind_host,
        bind_port,
        tls=cert is not None,
        trusted_hosts=trusted_host,
        trusted_origins=trusted_origin,
    )

    container = context.container()
    app = create_daemon_app(config, container, paths)

    url = display_url(bind_host, bind_port, tls=cert is not None)
    token_path = Path(paths.token_path)
    payload = {
        "url": url,
        "host": bind_host,
        "port": bind_port,
        "tls": cert is not None,
        "remote": remote,
        "edition": str(getattr(config, "edition", "installed")),
        "data_dir": str(paths.data_dir),
        "token_path": str(token_path),
        "docs_url": f"{url}/docs",
        "api_prefix": "/api/v1",
        "config": jsonable(config.public_dict()),
    }

    def render() -> None:
        out.success(f"AVD Fleet {APP_VERSION} listening on {url}")
        out.info(f"API      {url}/api/v1")
        out.info(f"Docs     {url}/docs")
        out.info(f"Data     {paths.data_dir}")
        out.info(f"Token    {token_path} (owner-only; the value is never printed)")
        if str(getattr(config, "edition", "installed")) == "portable":
            out.info("Mode     portable bundle")
        if cert is not None:
            out.info("TLS      enabled (HTTPS)")
        out.info("Press Ctrl+C to stop.")

    out.emit(payload, render)
    if not no_open and not out.json_output and out.is_tty:
        _open_browser_later(url)

    try:
        run_uvicorn(
            app,
            host=bind_host,
            port=bind_port,
            tls_cert=cert,
            tls_key=key,
            access_log=bool(out.verbose),
        )
    except KeyboardInterrupt:  # pragma: no cover - uvicorn handles SIGINT itself
        pass
    finally:
        if not out.json_output:
            out.info("server stopped")


def register(app: typer.Typer) -> None:
    app.command("serve")(command_errors("serve")(serve))


__all__ = [
    "UvicornRedactionFilter",
    "create_daemon_app",
    "display_url",
    "is_loopback_host",
    "register",
    "resolve_binding",
    "resolve_security_allowlists",
    "run_uvicorn",
    "serve",
    "uvicorn_log_config",
]
