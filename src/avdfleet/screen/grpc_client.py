"""Emulator gRPC discovery and an optional screen transport.

The Android emulator advertises its controller (gRPC) endpoint in small ini
files.  Two layouts are supported:

* the AOSP layout ``<tmp>/avd/running/pid_<pid>.ini`` (keys ``grpc.port``,
  ``grpc.address``, ``grpc.token``, ``port.serial``, ``pid``);
* the legacy/tooling layout ``<tmp>/android-<user>/emu-grpc/<console>.ini`` and
  per-AVD ``<avd_home>/<name>.avd/grpc.ini`` (used by the repo fakes).

``grpcio`` is an **optional** dependency: this module imports it lazily and
never fails to import when it is absent.  The transport is a Protocol so tests
and advanced integrations can inject a fake/alternative implementation; the
built-in :class:`GrpcEmulatorTransport` needs an injected ``stub_factory``
because the EmulatorController protobuf stubs are not generated in this repo.
"""

from __future__ import annotations

import asyncio
import getpass
import inspect
import tempfile
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..core.errors import ScreenError
from ..core.logging import get_logger

try:  # pragma: no cover - exercised only when grpcio is installed
    import grpc as _grpc
except Exception:  # the optional dependency is genuinely absent (or broken) on most installs
    _grpc = None  # type: ignore[assignment]

grpc = _grpc
"""The ``grpc`` module when installed, otherwise ``None``."""

logger = get_logger("avdfleet.screen.grpc")

DEFAULT_GRPC_PORT = 8554


# ---------------------------------------------------------------------------
# Discovery endpoints
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GrpcEndpoint:
    """One discovered emulator gRPC endpoint.

    ``token`` is excluded from ``repr`` so structural logging can never leak the
    bearer token; use :meth:`as_dict` when a serialisable form is needed.
    """

    address: str = "localhost"
    port: int = DEFAULT_GRPC_PORT
    token: str | None = field(default=None, repr=False)
    serial: str | None = None
    pid: int | None = None
    source: str = ""

    @property
    def host_port(self) -> str:
        host = self.address or "localhost"
        return f"{host}:{self.port}"

    @property
    def is_loopback(self) -> bool:
        host = (self.address or "localhost").strip().lower()
        return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

    def metadata(self) -> tuple[tuple[str, str], ...]:
        if not self.token:
            return ()
        return (("authorization", f"Bearer {self.token}"),)

    def as_dict(self, *, include_token: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "address": self.address,
            "port": self.port,
            "host_port": self.host_port,
            "serial": self.serial,
            "pid": self.pid,
            "source": self.source,
        }
        if include_token and self.token:
            payload["token"] = self.token
        return payload


def _parse_ini_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"')
        if key and value:
            values[key] = value
    return values


def parse_discovery_file(path: str | Path) -> GrpcEndpoint | None:
    """Parse one discovery ini file; return ``None`` when it has no usable port."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    values = _parse_ini_text(text)
    address = values.get("grpc.address") or ""
    port_raw = values.get("grpc.port") or ""
    if not port_raw and ":" in address:
        address, _, port_raw = address.rpartition(":")
    if not port_raw:
        return None
    try:
        port = int(port_raw)
    except ValueError:
        return None
    if not 1 <= port <= 65535:
        return None
    if not address:
        address = "localhost"
    pid: int | None = None
    if values.get("pid"):
        try:
            pid = int(values["pid"])
        except ValueError:
            pid = None
    return GrpcEndpoint(
        address=address,
        port=port,
        token=values.get("grpc.token") or values.get("grpc.jwt") or None,
        serial=values.get("port.serial") or values.get("grpc.serial") or None,
        pid=pid,
        source=str(source),
    )


def default_discovery_dirs(
    *,
    avd_home: str | Path | None = None,
    temp_root: str | Path | None = None,
    user: str | None = None,
) -> list[Path]:
    """Return the well-known gRPC discovery directories for this host."""
    temp = Path(temp_root) if temp_root is not None else Path(tempfile.gettempdir())
    try:
        account = user or getpass.getuser()
    except Exception:  # pragma: no cover - getpass can fail in stripped environments
        account = "user"
    candidates = [
        temp / f"android-{account}" / "emu-grpc",
        temp / "avd" / "running",
        temp / f"android-{account}" / "avd" / "running",
    ]
    if avd_home is not None:
        candidates.append(Path(avd_home))
    home = Path.home()
    candidates.extend(
        [
            home / ".android" / "avd" / "running",
            home / ".android" / "avd",
        ]
    )
    seen: list[Path] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.append(candidate)
    return seen


def _discovery_files(directory: Path, *, avd_name: str | None) -> list[Path]:
    if not directory.is_dir():
        return []
    files: list[Path] = []

    def _add(candidates: Iterable[Path]) -> None:
        for candidate in sorted(candidates):
            if candidate.is_file() and candidate not in files:
                files.append(candidate)

    _add(directory.glob("*.ini"))
    if avd_name:
        _add((directory / f"{avd_name}.avd").glob("*.ini"))
        avd_ini = directory / f"{avd_name}.avd" / "grpc.ini"
        if avd_ini.is_file() and avd_ini not in files:
            files.append(avd_ini)
    return files


def _serial_matches(endpoint: GrpcEndpoint, serial: str | None) -> bool:
    if not serial:
        return False
    requested = str(serial).strip()
    if not requested:
        return False
    digits = "".join(character for character in requested if character.isdigit())
    candidates = {requested, requested.lower(), requested.removeprefix("emulator-")}
    if digits:
        candidates.add(digits)
        candidates.add(f"emulator-{digits}")
    source_stem = Path(endpoint.source).stem.lower()
    if source_stem in candidates or (digits and source_stem.endswith(digits)):
        return True
    value = (endpoint.serial or "").strip()
    if not value:
        return False
    if value.lower() in candidates:
        return True
    value_digits = "".join(character for character in value if character.isdigit())
    return bool(digits) and value_digits == digits


def discover_endpoints(
    *,
    dirs: Iterable[str | Path] | None = None,
    avd_home: str | Path | None = None,
    avd_name: str | None = None,
    serial: str | None = None,
    pid: int | None = None,
    temp_root: str | Path | None = None,
    include_defaults: bool = True,
) -> list[GrpcEndpoint]:
    """Discover candidate gRPC endpoints, best match first."""
    search_dirs: list[Path] = []
    for directory in dirs or ():
        candidate = Path(directory)
        if candidate.is_dir() and candidate not in search_dirs:
            search_dirs.append(candidate)
    if avd_home is not None:
        candidate = Path(avd_home)
        if candidate.is_dir() and candidate not in search_dirs:
            search_dirs.append(candidate)
    if include_defaults:
        for directory in default_discovery_dirs(avd_home=avd_home, temp_root=temp_root):
            if directory.is_dir() and directory not in search_dirs:
                search_dirs.append(directory)

    endpoints: list[GrpcEndpoint] = []
    for directory in search_dirs:
        for file_path in _discovery_files(directory, avd_name=avd_name):
            endpoint = parse_discovery_file(file_path)
            if endpoint is None or endpoint in endpoints:
                continue
            endpoints.append(endpoint)
    if not endpoints:
        return []

    def _rank(endpoint: GrpcEndpoint) -> tuple[int, int, str]:
        serial_rank = 0 if _serial_matches(endpoint, serial) else 1
        pid_rank = 0 if (pid is not None and endpoint.pid == pid) else 1
        return (serial_rank, pid_rank, endpoint.source)

    if serial is not None or pid is not None:
        matches = [
            endpoint
            for endpoint in endpoints
            if _serial_matches(endpoint, serial) or (pid is not None and endpoint.pid == pid)
        ]
        if matches:
            return sorted(matches, key=_rank)
    return sorted(endpoints, key=_rank)


def find_endpoint(**kwargs: Any) -> GrpcEndpoint | None:
    """Return the single best discovered endpoint, if any."""
    endpoints = discover_endpoints(**kwargs)
    return endpoints[0] if endpoints else None


# ---------------------------------------------------------------------------
# Transport protocol (injectable for tests / alternative clients)
# ---------------------------------------------------------------------------


@runtime_checkable
class ScreenTransport(Protocol):
    """Low-level screen transport fed to :class:`avdfleet.screen.service.ScreenService`."""

    def available(self) -> bool: ...

    def capture(self) -> bytes: ...

    def stream(self, *, fps: float = 5.0) -> AsyncIterator[bytes]: ...


def _extract_frame(response: Any) -> bytes:
    """Pull PNG bytes out of a generated protobuf response (duck-typed)."""
    if response is None:
        raise ScreenError("the emulator gRPC screenshot call returned no response")
    if isinstance(response, (bytes, bytearray, memoryview)):
        return bytes(response)
    for attribute in ("image", "png", "data", "bytes", "screenshot"):
        value = getattr(response, attribute, None)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value)
    raise ScreenError(
        "could not extract PNG bytes from the emulator gRPC response",
        hint="inject a stub_factory that returns an object with a bytes 'image' field",
    )


class GrpcEmulatorTransport:
    """Minimal gRPC screen transport with an optional ``grpcio`` dependency.

    Parameters
    ----------
    endpoint:
        Discovered :class:`GrpcEndpoint` (port + bearer token).
    grpc_module:
        Override for the optional ``grpc`` import (tests).
    stub_factory:
        Callable ``(channel, metadata) -> controller stub``.  The repository does
        not vendor the EmulatorController protobuf stubs, so a real gRPC path
        needs an injected factory (or a subclass implementing ``_call_capture``).
    timeout_s:
        RPC timeout.
    """

    def __init__(
        self,
        endpoint: GrpcEndpoint | None = None,
        *,
        grpc_module: Any | None = None,
        stub_factory: Callable[..., Any] | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.endpoint = endpoint
        self.timeout_s = float(timeout_s)
        self._grpc = _grpc if grpc_module is None else grpc_module
        self._stub_factory = stub_factory
        self._channel: Any | None = None
        self._stub: Any | None = None

    # -- capability ------------------------------------------------------
    @property
    def grpc_available(self) -> bool:
        return self._grpc is not None

    def available(self) -> bool:
        """True only when grpcio, an injected stub factory and an endpoint exist."""
        return bool(
            self.endpoint is not None
            and self.endpoint.port
            and self.grpc_available
            and callable(self._stub_factory)
        )

    # -- internals -------------------------------------------------------
    def _open(self) -> Any:
        if self._stub is not None:
            return self._stub
        if not self.grpc_available:
            raise ScreenError(
                "the optional 'grpcio' dependency is not installed",
                hint="pip install avdfleet[screen] or use the adb screencap fallback",
            )
        if self.endpoint is None:
            raise ScreenError("no emulator gRPC endpoint was discovered")
        if not callable(self._stub_factory):
            raise ScreenError(
                "no emulator gRPC stub factory is configured",
                hint="inject stub_factory or use the adb screencap fallback",
            )
        grpc_module = self._grpc
        channel = grpc_module.insecure_channel(self.endpoint.host_port)
        metadata = self.endpoint.metadata()
        self._channel = channel
        try:
            parameters = inspect.signature(self._stub_factory).parameters
            if len(parameters) >= 2:
                self._stub = self._stub_factory(channel, metadata)
            else:  # pragma: no cover - single-argument factories
                self._stub = self._stub_factory(channel)
        except ScreenError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface as a screen error
            raise ScreenError(f"failed to open the emulator gRPC channel: {exc}") from exc
        return self._stub

    def _call_capture(self) -> bytes:
        stub = self._open()
        methods: list[tuple[str, Any]] = []
        for name in (
            "get_screenshot",
            "getScreenshot",
            "capture_screenshot",
            "captureScreenshot",
            "screenshot",
        ):
            candidate = getattr(stub, name, None)
            if callable(candidate):
                methods.append((name, candidate))
        if not methods:
            raise ScreenError(
                "the gRPC stub has no screenshot method",
                hint="expected get_screenshot()/getScreenshot() on the controller stub",
            )
        last_error: Exception | None = None
        for name, method in methods:
            try:
                try:
                    response = method(timeout=self.timeout_s)
                except TypeError:
                    response = method()
                return _extract_frame(response)
            except ScreenError:
                raise
            except Exception as exc:  # noqa: BLE001 - try the next spelling
                last_error = exc
                logger.debug("gRPC screenshot method %s failed: %s", name, exc)
        raise ScreenError(
            f"emulator gRPC screenshot failed: {last_error}",
            hint="falling back to adb screencap",
        )

    # -- ScreenTransport -------------------------------------------------
    def capture(self) -> bytes:
        return self._call_capture()

    async def stream(self, *, fps: float = 5.0) -> AsyncIterator[bytes]:
        interval = 1.0 / max(1.0, float(fps))
        while True:
            frame = await asyncio.to_thread(self._call_capture)
            yield frame
            await asyncio.sleep(interval)

    def close(self) -> None:
        channel, self._channel = self._channel, None
        self._stub = None
        if channel is None:
            return
        closer = getattr(channel, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # pragma: no cover - best effort
                logger.debug("failed to close the gRPC channel", exc_info=True)


__all__ = [
    "DEFAULT_GRPC_PORT",
    "GrpcEmulatorTransport",
    "GrpcEndpoint",
    "ScreenTransport",
    "default_discovery_dirs",
    "discover_endpoints",
    "find_endpoint",
    "grpc",
    "parse_discovery_file",
]
