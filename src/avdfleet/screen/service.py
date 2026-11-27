"""Concrete screen service: gRPC capture/streaming with adb fallbacks + input.

Implements :class:`avdfleet.core.interfaces.ScreenClient`:

* ``available()`` — tells the API/CLI whether capture can work for an instance;
* ``capture()`` — gRPC screenshot when an endpoint + transport are usable, else
  ``adb exec-out screencap -p`` (then ``adb shell screencap -p``), always
  validated as a PNG and cached under ``Paths.screenshots_dir/live``;
* ``stream()`` — async frame iterator at a configurable FPS using the gRPC
  transport when available and a periodic ``screencap`` poll otherwise;
  disconnects fall back to the adb path and cancellation closes the generator;
* ``tap``/``swipe``/``text``/``key`` — safe ``adb shell input`` injection that
  refuses to run when the instance is not running.

The service never imports ``grpcio`` itself: the optional dependency lives in
:mod:`avdfleet.screen.grpc_client` and a bare installation degrades to adb.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

from ..core.config import Config
from ..core.errors import AvdFleetError, ScreenError
from ..core.logging import get_logger, redact
from ..core.models import AvdInfo, OperationResult
from ..core.paths import Paths
from .grpc_client import GrpcEmulatorTransport, GrpcEndpoint, find_endpoint
from .input import InputController
from .scrcpy import ScrcpyConfig, ScrcpyLauncher
from .screencap import AdbScreencap, FrameCache, SubprocessRunner, capture_async, is_png, validate_png

logger = get_logger("avdfleet.screen")

DEFAULT_FPS = 5.0
MAX_ADB_STREAM_FAILURES = 3


def _paths_for_root(root: str | Path) -> Paths:
    root_path = Path(root).expanduser()
    return Paths(
        root=root_path,
        data_dir=root_path / "data",
        config_dir=root_path / "config",
        cache_dir=root_path / "cache",
        log_dir=root_path / "logs",
        sdk_root=root_path / "sdk",
        avd_home=root_path / "avd",
    )


def _is_running(info: AvdInfo | None) -> bool:
    if info is None:
        return False
    state = getattr(info, "state", None)
    active = getattr(state, "active", None)
    if isinstance(active, bool):
        return active
    raw = getattr(state, "value", state)
    return str(raw or "").strip().lower() in {"starting", "booting", "running"}


class ScreenService:
    """Screen capture, streaming and input for AVD instances."""

    def __init__(
        self,
        runtime: Any,
        paths: Paths | str | Path,
        *,
        adb: Any | None = None,
        avds: Any | None = None,
        config: Config | None = None,
        adb_executable: str = "adb",
        transport: Any | None = None,
        transport_factory: Callable[[AvdInfo, GrpcEndpoint | None], Any | None] | None = None,
        discovery_dirs: Sequence[str | Path] | None = None,
        discovery_include_defaults: bool = True,
        fps: float = DEFAULT_FPS,
        cache_enabled: bool = True,
        input_controller: InputController | None = None,
        scrcpy: ScrcpyLauncher | None = None,
        event_bus: Any | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        if runtime is None:
            raise ScreenError("an instance runtime is required for the screen service")
        self._runtime = runtime
        self._avds = avds
        self._paths = paths if isinstance(paths, Paths) else _paths_for_root(paths)
        self._config = config
        self._adb_executable = adb_executable
        self._logger = logger_ or logger
        self._warnings: list[str] = []
        self._fps = max(0.2, float(fps or DEFAULT_FPS))
        self._discovery_dirs = [Path(item) for item in (discovery_dirs or ())]
        self._discovery_include_defaults = bool(discovery_include_defaults)
        request_timeout = float(getattr(config, "request_timeout_s", 30.0) or 30.0)
        self._request_timeout_s = request_timeout
        # The daemon container wires only an adb executable path; default to a
        # subprocess runner so capture/input keep working without an injection.
        self._adb_runner = adb if adb is not None else SubprocessRunner(
            adb_executable,
            timeout_s=request_timeout,
        )

        self._screencap = AdbScreencap(self._adb_runner, adb=adb_executable, timeout_s=request_timeout)
        self._cache = FrameCache(Path(self._paths.screenshots_dir) / "live", enabled=cache_enabled)
        self._transport_override = transport
        self._transport_factory = transport_factory
        self._transports: dict[str, Any] = {}
        self._endpoints: dict[str, GrpcEndpoint | None] = {}
        self._scrcpy = scrcpy or ScrcpyLauncher()
        self._events = event_bus
        self._input = input_controller or InputController(
            self._adb_runner,
            serial_for=self._serial_for_name,
            is_running=self._is_running_name,
            adb=adb_executable,
            timeout_s=request_timeout,
        )

    # -- introspection ---------------------------------------------------
    @property
    def fps(self) -> float:
        return self._fps

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    @property
    def frame_cache(self) -> FrameCache:
        return self._cache

    # -- availability ----------------------------------------------------
    def available(self, avd_name: str) -> bool:
        """True when the running instance can be captured (gRPC or adb)."""
        try:
            info = self._instance(avd_name)
        except ScreenError:
            return False
        if not _is_running(info):
            return False
        transport = self._transport_for(info, avd_name)
        if transport is not None and self._transport_usable(transport):
            return True
        if self._screencap.runner is None:
            return False
        try:
            self._serial(info)
        except ScreenError:
            return False
        return True

    def diagnostics(self, avd_name: str) -> dict[str, Any]:
        """Non-throwing snapshot of the screen path for one instance."""
        try:
            info = self._instance(avd_name)
        except ScreenError as exc:
            return {"available": False, "reason": str(exc)}
        running = _is_running(info)
        serial = ""
        if running:
            try:
                serial = self._serial(info)
            except ScreenError:
                serial = ""
        transport = self._transport_for(info, avd_name) if running else None
        endpoint = self._endpoints.get(avd_name)
        return {
            "available": running and bool(serial),
            "running": running,
            "serial": serial,
            "state": str(getattr(getattr(info, "state", ""), "value", getattr(info, "state", "")) or ""),
            "grpc": bool(transport is not None and self._transport_usable(transport)),
            "grpc_endpoint": endpoint.host_port if endpoint is not None else "",
            "adb_fallback": bool(serial),
            "cached_frame": self._cache.get_path(avd_name) is not None,
            "cache_path": str(self._cache.get_path(avd_name)) if self._cache.get_path(avd_name) else "",
            "scrcpy": self._scrcpy.available(),
        }

    # -- capture ---------------------------------------------------------
    def capture(self, avd_name: str) -> bytes:
        """Capture one PNG frame through the fallback chain."""
        info = self._instance(avd_name)
        if not _is_running(info):
            raise ScreenError(
                f"instance {avd_name!r} is not running",
                hint="start the instance before requesting a screenshot",
                details={"avd": avd_name},
            )
        errors: list[str] = []
        transport = self._transport_for(info, avd_name)
        if transport is not None and self._transport_usable(transport):
            try:
                data = transport.capture()  # type: ignore[union-attr]
                frame = validate_png(data, context="gRPC screenshot")
                self._cache.store(avd_name, frame)
                return frame
            except ScreenError as exc:
                errors.append(f"gRPC: {exc}")
                self._warn(
                    f"gRPC screenshot capture for {avd_name} failed ({redact(str(exc))});"
                    " falling back to adb screencap"
                )
            except Exception as exc:  # noqa: BLE001 - connection problems fall back
                errors.append(f"gRPC: {type(exc).__name__}: {redact(str(exc))}")
                self._warn(
                    f"gRPC screenshot capture for {avd_name} failed"
                    f" ({type(exc).__name__}: {redact(str(exc))}); falling back to adb screencap"
                )
        serial = self._serial(info)
        try:
            data = self._screencap.capture(serial, timeout_s=self._request_timeout_s)
        except ScreenError as exc:
            detail = f" ({'; '.join(errors)})" if errors else ""
            raise ScreenError(
                f"screen capture failed for instance {avd_name!r}{detail}",
                hint="check that the instance is running and gRPC/screencap is available",
                details={"avd": avd_name, "serial": serial, "attempts": [*errors, str(exc)]},
            ) from exc
        self._cache.store(avd_name, data)
        return data

    def cache_path(self, avd_name: str) -> Path | None:
        """Path of the last cached frame for *avd_name* (if any this session)."""
        return self._cache.get_path(avd_name)

    # -- streaming -------------------------------------------------------
    async def stream(self, avd_name: str, *, fps: float | None = None) -> AsyncIterator[bytes]:
        """Yield PNG frames at *fps* until the consumer stops iterating."""
        interval = 1.0 / max(0.2, float(fps or self._fps))
        info = self._instance(avd_name)
        if not _is_running(info):
            raise ScreenError(
                f"instance {avd_name!r} is not running",
                hint="start the instance before opening the screen stream",
                details={"avd": avd_name},
            )
        transport = self._transport_for(info, avd_name)
        if transport is not None and self._transport_usable(transport) and hasattr(transport, "stream"):
            try:
                async for raw_frame in transport.stream(fps=1.0 / interval):  # type: ignore[union-attr]
                    if not is_png(raw_frame):
                        self._warn(f"gRPC stream delivered a non-PNG frame for {avd_name}; skipping it")
                        continue
                    self._cache.store(avd_name, bytes(raw_frame))
                    yield bytes(raw_frame)
                return
            except asyncio.CancelledError:
                raise
            except ScreenError as exc:
                self._warn(f"gRPC screen stream for {avd_name} disconnected: {redact(str(exc))}")
            except Exception as exc:  # noqa: BLE001 - fall back to periodic screencap
                self._warn(
                    f"gRPC screen stream for {avd_name} failed"
                    f" ({type(exc).__name__}: {redact(str(exc))}); falling back to adb"
                )
            self._logger.info("screen stream for %s is falling back to adb screencap", avd_name)
        failures = 0
        while True:
            try:
                frame = await capture_async(lambda: self._capture_adb_frame(avd_name))
            except ScreenError as exc:
                failures += 1
                if failures >= MAX_ADB_STREAM_FAILURES:
                    raise ScreenError(
                        f"screen stream for instance {avd_name!r} failed: {redact(str(exc))}",
                        hint="check the instance state and adb connectivity",
                        details={"avd": avd_name, "failures": failures},
                    ) from exc
                self._warn(f"screen capture for {avd_name} failed ({exc}); retrying")
                await asyncio.sleep(interval)
                continue
            failures = 0
            yield frame
            await asyncio.sleep(interval)

    def _capture_adb_frame(self, avd_name: str) -> bytes:
        info = self._instance(avd_name)
        if not _is_running(info):
            raise ScreenError(f"instance {avd_name!r} stopped during streaming")
        serial = self._serial(info)
        data = self._screencap.capture(serial, timeout_s=self._request_timeout_s)
        self._cache.store(avd_name, data)
        return data

    # -- input -----------------------------------------------------------
    def tap(self, avd_name: str, x: int, y: int) -> OperationResult:
        return self._input.tap(avd_name, x, y)

    def swipe(
        self,
        avd_name: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
    ) -> OperationResult:
        return self._input.swipe(avd_name, x1, y1, x2, y2, duration_ms=duration_ms)

    def text(self, avd_name: str, value: str) -> OperationResult:
        return self._input.text(avd_name, value)

    def key(self, avd_name: str, keycode: str) -> OperationResult:
        return self._input.key(avd_name, keycode)

    # -- scrcpy fallback (local display) ---------------------------------
    def scrcpy_available(self) -> bool:
        return self._scrcpy.available()

    def scrcpy_command(self, avd_name: str, *, config: ScrcpyConfig | None = None) -> list[str]:
        info = self._instance(avd_name)
        if not _is_running(info):
            raise ScreenError(
                f"instance {avd_name!r} is not running",
                hint="start the instance before launching scrcpy",
            )
        return self._scrcpy.command(self._serial(info), avd_name=avd_name, config=config)

    def launch_scrcpy(
        self,
        avd_name: str,
        *,
        config: ScrcpyConfig | None = None,
        spawn: bool = False,
    ) -> OperationResult:
        info = self._instance(avd_name)
        if not _is_running(info):
            return OperationResult(
                ok=False,
                message=f"instance {avd_name!r} is not running; scrcpy was not launched",
                data={"avd": avd_name},
            )
        return self._scrcpy.launch(self._serial(info), avd_name=avd_name, config=config, spawn=spawn)

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        for transport in self._transports.values():
            closer = getattr(transport, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # pragma: no cover - best effort
                    self._logger.debug("failed to close a screen transport", exc_info=True)
        self._transports.clear()
        self._endpoints.clear()
        self._cache.clear()

    # -- instance resolution ---------------------------------------------
    def _instance(self, avd_name: str) -> AvdInfo:
        name = str(avd_name or "").strip()
        if not name:
            raise ScreenError("an AVD name is required")
        last_error: BaseException | None = None
        status = getattr(self._runtime, "status", None)
        if callable(status):
            try:
                info = status(name)
                if info is not None:
                    return info
            except (KeyError, IndexError) as exc:
                last_error = exc
            except AvdFleetError as exc:
                last_error = exc
        getter = getattr(self._avds, "get_instance", None)
        if callable(getter):
            try:
                info = getter(name)
            except (KeyError, IndexError, AvdFleetError) as exc:
                last_error = exc
            else:
                if info is not None:
                    return info
        message = f"instance {name!r} was not found"
        if last_error is not None and str(last_error):
            message = f"instance {name!r} was not found: {redact(str(last_error))}"
        raise ScreenError(message, hint="check `avdfleet instances list`") from last_error

    def _is_running_name(self, avd_name: str) -> bool:
        try:
            return _is_running(self._instance(avd_name))
        except ScreenError:
            return False

    def _serial_for_name(self, avd_name: str) -> str:
        return self._serial(self._instance(avd_name))

    @staticmethod
    def _serial(info: AvdInfo) -> str:
        serial = getattr(info, "adb_serial", None)
        if serial:
            return str(serial)
        ports = getattr(info, "ports", None)
        console = getattr(ports, "console", None) if ports is not None else None
        if console:
            return f"emulator-{console}"
        raise ScreenError(
            f"cannot determine the adb serial for instance {getattr(info, 'name', '?')!r}",
            hint="the instance must be running with an assigned adb port",
        )

    # -- transports ------------------------------------------------------
    def _transport_usable(self, transport: Any) -> bool:
        probe = getattr(transport, "available", None)
        if not callable(probe):
            return True
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 - a broken transport is unavailable
            return False

    def _transport_for(self, info: AvdInfo, avd_name: str) -> Any | None:
        cached = self._transports.get(avd_name)
        if cached is not None:
            return cached
        endpoint = self._endpoint_for(info, avd_name)
        transport: Any | None
        if self._transport_factory is not None:
            try:
                transport = self._transport_factory(info, endpoint)
            except Exception as exc:  # noqa: BLE001 - factory failures are non-fatal
                self._warn(f"could not build a screen transport for {avd_name}: {redact(str(exc))}")
                transport = None
        elif self._transport_override is not None:
            transport = self._transport_override
        else:
            transport = GrpcEmulatorTransport(endpoint)
        if transport is not None:
            self._transports[avd_name] = transport
        return transport

    def _endpoint_for(self, info: AvdInfo, avd_name: str) -> GrpcEndpoint | None:
        if avd_name in self._endpoints:
            return self._endpoints[avd_name]
        pid = getattr(info, "pid", None)
        serial = getattr(info, "adb_serial", None)
        if not serial:
            ports = getattr(info, "ports", None)
            console = getattr(ports, "console", None) if ports is not None else None
            serial = f"emulator-{console}" if console else None
        endpoint = getattr(info, "grpc_endpoint", None)
        discovered: GrpcEndpoint | None = None
        if endpoint:
            host, _, port_text = str(endpoint).rpartition(":")
            if port_text.isdigit():
                discovered = GrpcEndpoint(address=host or "localhost", port=int(port_text), serial=serial)
        if discovered is None:
            try:
                discovered = find_endpoint(
                    dirs=self._discovery_dirs or None,
                    avd_home=self._paths.avd_home,
                    avd_name=avd_name,
                    serial=serial,
                    pid=pid if isinstance(pid, int) else None,
                    include_defaults=self._discovery_include_defaults,
                )
            except Exception:  # noqa: BLE001 - discovery is best effort
                self._logger.debug("gRPC discovery failed for %s", avd_name, exc_info=True)
                discovered = None
        self._endpoints[avd_name] = discovered
        return discovered

    def _warn(self, message: str) -> None:
        if message not in self._warnings:
            self._warnings.append(message)
        self._logger.warning("%s", message)


__all__ = ["DEFAULT_FPS", "ScreenService"]
