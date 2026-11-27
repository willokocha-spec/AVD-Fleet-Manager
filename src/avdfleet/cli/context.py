"""Lazy execution context shared by every CLI command.

Design
------
* :class:`CliContext` loads :class:`~avdfleet.core.config.Config` and
  :class:`~avdfleet.core.paths.Paths` only when a command needs them, and builds
  the production service container (:func:`avdfleet.container.build_container`)
  **only** when a command actually calls a service.  avdfleet --help,
  --version and every --dry-run-style help path therefore import no
  concrete subsystem.
* Tests inject a stub container in either of the two documented ways:

  1. runner.invoke(app, args, obj=CliContext(container=stub)) - direct
     object injection (also accepting a plain {"container": ...} mapping);
  2. the AVDFLEET_CONTAINER=module:factory environment variable - a factory
     returning a container, called with (config, paths) when it accepts two
     arguments and with no arguments otherwise.

* Services are resolved through the same duck-typed protocol the API uses:
  a getter method (images()) or a plain attribute, so both the production
  container and lightweight test doubles work unchanged.

Keep this module free of imports from concrete services: only avdfleet.core
and the CLI's own error/output modules are imported eagerly.
"""

from __future__ import annotations

import importlib
import inspect
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from avdfleet.core.config import Config
from avdfleet.core.errors import AvdFleetError
from avdfleet.core.paths import Paths

from .errors import CliEnvironmentError, CliServiceError, CliUsageError
from .output import Output

#: Environment variable naming a module:factory callable used to inject a
#: service container (tests, embedding, alternate wiring).
CONTAINER_ENV = "AVDFLEET_CONTAINER"

#: Environment variable naming an explicit configuration file.
CONFIG_ENV = "AVDFLEET_CONFIG_FILE"

#: Duck-typed service getters the CLI understands (container protocol + extras).
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
    "verifier",
    "host",
    "repository",
    "store",
)


def configure_stdio(*streams: Any) -> None:
    """Make writes to legacy Windows consoles (cp1252) lossless instead of fatal.

    Redirected/piped output on Windows still uses the ANSI code page, so a
    single non-ASCII glyph in help text, a device name or an error message
    would otherwise raise UnicodeEncodeError.  Setting errors="replace"
    keeps every command (including --help) working; PYTHONIOENCODING and
    the console's own encoding are left untouched.
    """
    for stream in streams or (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(errors="replace")
        except Exception:  # pragma: no cover - exotic/closed stream
            continue


# ---------------------------------------------------------------------------
# Introspection helpers
# ---------------------------------------------------------------------------


def invoke_callable(function: Any, *args: Any, **kwargs: Any) -> Any:
    """Call *function*, dropping keyword arguments it does not accept.

    Services evolve with optional keyword arguments (timeout_s,
    dry_run, ...).  The CLI passes the documented ones and this helper
    adapts to the actual signature, which also keeps simple test doubles usable.
    """
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):  # pragma: no cover - builtins/C callables
        return function(*args, **kwargs)
    parameters = list(signature.parameters.values())
    variadic_positional = any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters
    )
    if not variadic_positional:
        max_positional = sum(
            1
            for parameter in parameters
            if parameter.kind
            in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        )
        args = tuple(args[:max_positional])
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return function(*args, **kwargs)
    accepted = {
        parameter.name
        for parameter in parameters
        if parameter.kind
        in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
    }
    filtered = {key: value for key, value in kwargs.items() if key in accepted}
    return function(*args, **filtered)


def call_method(target: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    """Call target.name(*args, **kwargs) with a clear CLI error when missing."""
    method = getattr(target, name, None)
    if not callable(method):
        service = type(target).__name__
        raise CliServiceError(
            f"{service} does not support {name!r}",
            hint="this capability is not available in the current build",
        )
    return invoke_callable(method, *args, **kwargs)


def _call_container_factory(factory: Any, config: Config, paths: Paths) -> Any:
    """Call a container factory with the arity it declares (0, 1 or 2 arguments)."""
    try:
        parameters = list(inspect.signature(factory).parameters.values())
    except (TypeError, ValueError):  # pragma: no cover - builtins/C callables
        return factory()
    if any(
        parameter.kind
        in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
        for parameter in parameters
    ):
        return factory(config, paths)
    accepted = [
        parameter
        for parameter in parameters
        if parameter.kind
        in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    if len(accepted) >= 2:
        return factory(config, paths)
    if len(accepted) == 1:
        return factory(config)
    return factory()


def load_container_factory(spec: str) -> Any:
    """Resolve module:attr (or module.attr) into a callable."""
    candidate = spec.strip()
    if not candidate:
        raise CliEnvironmentError(f"{CONTAINER_ENV} is empty")
    module_name, separator, attribute = candidate.replace(":", "|", 1).partition("|")
    if not separator:
        module_name, _, attribute = candidate.rpartition(".")
    if not module_name or not attribute:
        raise CliEnvironmentError(
            f"{CONTAINER_ENV} must look like 'module:factory'",
            hint=f"got {spec!r}",
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise CliEnvironmentError(
            f"could not import the container factory module {module_name!r}",
            hint=f"{CONTAINER_ENV}={spec}",
        ) from exc
    target = getattr(module, attribute, None)
    if target is None:
        raise CliEnvironmentError(
            f"container factory {attribute!r} was not found in {module_name!r}",
            hint=f"{CONTAINER_ENV}={spec}",
        )
    if not callable(target):
        raise CliEnvironmentError(
            f"container factory {spec!r} is not callable",
            hint=f"{CONTAINER_ENV}={spec}",
        )
    return target


# ---------------------------------------------------------------------------
# CliContext
# ---------------------------------------------------------------------------


class CliContext:
    """Per-invocation state: flags, config, paths, container and output."""

    def __init__(
        self,
        *,
        config_path: str | Path | None = None,
        json_output: bool = False,
        verbose: bool = False,
        quiet: bool = False,
        container: Any | None = None,
        config: Config | None = None,
        paths: Paths | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        configure_stdio()
        self.config_path: str | Path | None = config_path
        self.json_output = bool(json_output)
        self.verbose = bool(verbose)
        self.quiet = bool(quiet)
        self.environ: dict[str, str] = dict(os.environ if environ is None else environ)
        self._config = config
        self._paths = paths
        self._container = container
        self._owns_container = False
        self._output: Output | None = None
        self._closed = False

    # -- configuration ---------------------------------------------------
    def configure(
        self,
        *,
        config_path: str | Path | None = None,
        json_output: bool | None = None,
        verbose: bool | None = None,
        quiet: bool | None = None,
    ) -> CliContext:
        """Merge global flags into this context (idempotent)."""
        if config_path is not None:
            self.config_path = config_path
        if json_output is not None:
            self.json_output = bool(json_output) or self.json_output
        if verbose is not None:
            self.verbose = bool(verbose) or self.verbose
        if quiet is not None:
            self.quiet = bool(quiet) or self.quiet
        if self.quiet:
            self.verbose = False
        return self

    @property
    def has_injected_container(self) -> bool:
        """True when a test/embedding container is injected (object or env factory)."""
        if self._container is not None:
            return True
        return bool((self.environ.get(CONTAINER_ENV) or "").strip())

    def set_container(self, container: Any, *, owned: bool = False) -> CliContext:
        self._container = container
        self._owns_container = bool(owned)
        return self

    def set_config(self, config: Config) -> CliContext:
        self._config = config
        return self

    def set_paths(self, paths: Paths) -> CliContext:
        self._paths = paths
        return self

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> CliContext:
        """Accept the documented mapping form of ctx.obj."""
        context = cls(
            config_path=data.get("config_path") or data.get("config"),
            json_output=bool(data.get("json_output", data.get("json", False))),
            verbose=bool(data.get("verbose", False)),
            quiet=bool(data.get("quiet", False)),
        )
        container = data.get("container")
        if container is not None:
            context.set_container(container)
        if isinstance(data.get("config_obj"), Config):
            context.set_config(data["config_obj"])
        if isinstance(data.get("paths"), Paths):
            context.set_paths(data["paths"])
        return context

    # -- lazy resources --------------------------------------------------
    @property
    def config(self) -> Config:
        if self._config is None:
            container = self._container
            if container is not None:
                candidate = getattr(container, "config", None)
                if isinstance(candidate, Config):
                    self._config = candidate
        if self._config is None:
            configured = self.config_path
            if configured is None:
                configured = self.environ.get(CONFIG_ENV) or None
            try:
                if self.environ:
                    self._config = Config.load(configured, environ=self.environ)
                else:
                    self._config = Config.load(configured)
            except FileNotFoundError as exc:
                raise CliUsageError(
                    f"configuration file not found: {configured}",
                    hint="pass --config PATH pointing at an existing config.toml",
                ) from exc
            except (OSError, ValueError) as exc:
                raise CliUsageError(f"could not load the configuration: {exc}") from exc
        return self._config

    @property
    def paths(self) -> Paths:
        if self._paths is None:
            container = self._container
            if container is not None:
                candidate = getattr(container, "paths", None)
                if isinstance(candidate, Paths):
                    self._paths = candidate
        if self._paths is None:
            self._paths = self.config.resolved_paths()
        return self._paths

    def output(self, *, json_output: bool = False) -> Output:
        if self._output is None:
            self._output = Output(
                json_output=self.json_output or bool(json_output),
                quiet=self.quiet,
                verbose=self.verbose,
            )
        elif json_output:
            self._output.set_json(True)
        return self._output

    # -- container / services -------------------------------------------
    def container(self) -> Any:
        """Return the service container, building it on first use."""
        if self._container is not None:
            return self._container
        factory_spec = (self.environ.get(CONTAINER_ENV) or "").strip()
        if factory_spec:
            factory = load_container_factory(factory_spec)
            try:
                container = _call_container_factory(factory, self.config, self.paths)
            except AvdFleetError:
                raise
            except Exception as exc:  # noqa: BLE001 - factory failures are environment errors
                raise CliEnvironmentError(
                    "the injected service container could not be built",
                    hint=f"{CONTAINER_ENV}={factory_spec}",
                ) from exc
            if container is None:
                raise CliEnvironmentError(
                    "the injected service container factory returned None",
                    hint=f"{CONTAINER_ENV}={factory_spec}",
                )
            # Injected containers are owned by the caller (tests/embedding).
            self._container = container
            self._owns_container = False
            return container
        try:
            from avdfleet.container import build_container  # noqa: PLC0415 - deliberately lazy
        except ImportError as exc:  # pragma: no cover - full install always has it
            raise CliEnvironmentError(
                "the AVD Fleet service container is not installed",
                hint="reinstall avdfleet (the 'avdfleet.container' module is missing)",
            ) from exc
        try:
            # A one-shot CLI invocation exits seconds after spawning an emulator,
            # so the per-instance proxy bridge must live in a detached worker; an
            # in-process bridge would die here, the emulator would give up on its
            # proxy and the guest would silently egress directly (G1 failure).
            container = invoke_callable(
                build_container, self.config, self.paths, detached_bridge=True
            )
        except AvdFleetError:
            raise
        except Exception as exc:  # noqa: BLE001 - startup failures are environment errors
            raise CliEnvironmentError(
                "the AVD Fleet services could not be initialised",
                hint="run 'avdfleet doctor' and check the log for details",
            ) from exc
        self._container = container
        self._owns_container = True
        return container

    def service(self, name: str) -> Any:
        """Resolve one service from the container (getter method or attribute)."""
        container = self.container()
        try:
            target = getattr(container, name)
        except AttributeError as exc:
            raise CliServiceError(
                f"the {name} service is not available in this build",
                service=name,
                hint="run 'avdfleet doctor' or reinstall with the full feature set",
            ) from exc
        if target is None:
            raise CliServiceError(
                f"the {name} service is not available in this build",
                service=name,
                hint="run 'avdfleet doctor' or reinstall with the full feature set",
            )
        if callable(target) and not isinstance(target, type):
            try:
                resolved = target()
            except AvdFleetError:
                raise
            except Exception as exc:  # noqa: BLE001 - service startup failure
                raise CliServiceError(
                    f"the {name} service failed to initialise",
                    service=name,
                    hint="check the log or run 'avdfleet doctor'",
                ) from exc
        else:
            resolved = target
        if resolved is None:
            raise CliServiceError(
                f"the {name} service is not available in this build",
                service=name,
                hint="run 'avdfleet doctor' or reinstall with the full feature set",
            )
        return resolved

    def optional_service(self, name: str) -> Any | None:
        """Like :meth:`service` but returns None instead of raising."""
        try:
            return self.service(name)
        except (CliServiceError, AvdFleetError):
            return None

    def host_info(self) -> Any | None:
        """Return the detected host information when the build can provide it."""
        container = self.container()
        candidate = getattr(container, "host_info", None)
        if candidate is not None:
            return candidate
        host = self.optional_service("host")
        if host is not None:
            detect = getattr(host, "detect", None)
            if callable(detect):
                return invoke_callable(detect)
        return None

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._owns_container or self._container is None:
            return
        closer = getattr(self._container, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # pragma: no cover - shutdown must not fail the CLI
                pass


# ---------------------------------------------------------------------------
# Context helpers used by command modules
# ---------------------------------------------------------------------------


def is_click_context(value: Any) -> bool:
    """Duck-test for a click/Typer Context.

    Typer vendors its own click build, so the runtime class is
    typer._click.core.Context and is **not** an instance of the
    annotation-only typer.Context alias.  Matching on the class name and
    click module keeps this working across Typer/click versions.
    """
    cls = type(value)
    if cls.__name__ != "Context":
        return False
    return "click" in str(getattr(cls, "__module__", ""))


def get_context(ctx: Any) -> CliContext:
    """Normalise a Typer context / mapping / CliContext into a :class:`CliContext`."""
    if isinstance(ctx, CliContext):
        return ctx
    if is_click_context(ctx):
        obj = ctx.obj
        if isinstance(obj, CliContext):
            return obj
        context = CliContext.from_mapping(obj) if isinstance(obj, Mapping) else CliContext()
        ctx.obj = context
        return context
    if isinstance(ctx, Mapping):
        return CliContext.from_mapping(ctx)
    return CliContext()


def context_from(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> CliContext:
    """Find and normalise the Typer context inside a command's arguments."""
    candidate = kwargs.get("ctx")
    if candidate is not None:
        return get_context(candidate)
    for value in args:
        if isinstance(value, CliContext) or is_click_context(value):
            return get_context(value)
    return CliContext()


def service(ctx: Any, name: str) -> Any:
    return get_context(ctx).service(name)


def optional_service(ctx: Any, name: str) -> Any | None:
    return get_context(ctx).optional_service(name)


def output(ctx: Any, *, json_output: bool = False) -> Output:
    return get_context(ctx).output(json_output=json_output)


__all__ = [
    "CONFIG_ENV",
    "CONTAINER_ENV",
    "SERVICE_GETTERS",
    "CliContext",
    "call_method",
    "configure_stdio",
    "context_from",
    "get_context",
    "invoke_callable",
    "is_click_context",
    "load_container_factory",
    "optional_service",
    "output",
    "service",
]
