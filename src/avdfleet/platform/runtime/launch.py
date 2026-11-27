"""Emulator launch planning: argv construction, environment and process creation.

The command line is part of the security boundary of AVD Fleet: proxy passwords
must never appear in ``ps`` output, log files or persisted state.  Therefore the
only proxy value this module accepts is a *credential-free* upstream URL - either
the local bridge's loopback listener or, only when a direct provider is
explicitly enabled, ``scheme://host:port`` with no userinfo.  :func:`build_launch_spec`
rejects userinfo outright instead of silently redacting it.

Launch shapes:

* headless: ``-no-window -gpu swiftshader_indirect`` (``host``/``auto`` GPU modes
  fall back to SwiftShader when there is no window server);
* visible: the configured GPU mode is passed through unchanged;
* always: ``-avd <name> -port <even console> -no-snapshot-save`` plus the gRPC
  port for the screen service (``-grpc <port>``);
* optional: ``-wipe-data`` (OOBE reset), ``-http-proxy <bridge>``, extra argv.

The process factory is injectable so unit tests never spawn a real emulator; the
default implementation uses :class:`subprocess.Popen` with detached stdio and a
per-instance log file so a noisy emulator can never block the daemon.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from avdfleet.core.errors import InstanceError, ValidationError
from avdfleet.core.logging import get_logger, redact
from avdfleet.core.models import GpuMode, InstancePorts
from avdfleet.core.paths import Paths

logger = get_logger("platform.runtime.launch")

AVD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SNAPSHOT_SAFE_PROXY_SCHEMES = frozenset({"http", "https", "socks5"})
_HEADLESS_GPU_FALLBACKS = frozenset({"auto", "host"})


# ---------------------------------------------------------------------------
# Process protocol / factory
# ---------------------------------------------------------------------------


@runtime_checkable
class ProcessHandle(Protocol):
    """The subset of ``subprocess.Popen`` the runtime manager needs."""

    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def send_signal(self, sig: int) -> None: ...


@runtime_checkable
class ProcessFactory(Protocol):
    """Callable that starts a detached emulator process for one instance."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str | Path | None = None,
        log_path: str | Path | None = None,
    ) -> ProcessHandle: ...


class SubprocessProcessFactory:
    """Default process factory: detached stdio, output appended to ``log_path``."""

    def __init__(self, *, creationflags: int | None = None) -> None:
        self._creationflags = creationflags

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str | Path | None = None,
        log_path: str | Path | None = None,
    ) -> ProcessHandle:
        command = [str(item) for item in argv]
        kwargs: dict[str, Any] = {
            "args": command,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "env": dict(env),
            "cwd": str(cwd) if cwd is not None else None,
            "close_fds": True,
        }
        flags = self._creationflags
        if os.name == "nt":
            if flags is None:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            kwargs["creationflags"] = flags or 0
        else:
            kwargs["start_new_session"] = True
        handle = None
        if log_path is not None:
            target = Path(log_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = target.open("ab")
            kwargs["stdout"] = handle
            kwargs["stderr"] = subprocess.STDOUT
        try:
            process = subprocess.Popen(**kwargs)  # noqa: S603 - argv list, no shell
        except OSError as exc:
            raise InstanceError(
                f"could not start the Android emulator: {exc}",
                hint="Check that the emulator executable exists and is permitted to run.",
            ) from exc
        finally:
            if handle is not None:
                handle.close()
        return process


# ---------------------------------------------------------------------------
# Launch specification
# ---------------------------------------------------------------------------


@dataclass
class LaunchSpec:
    """Everything needed to start one emulator (and to reproduce the attempt)."""

    name: str
    argv: list[str]
    env: dict[str, str]
    cwd: Path | None = None
    log_path: Path | None = None
    headless: bool = True
    gpu_mode: str = GpuMode.SWIFTSHADER_INDIRECT.value
    proxy_upstream: str | None = None
    wipe_data: bool = False

    @property
    def redacted_argv(self) -> list[str]:
        return [str(redact(item)) for item in self.argv]

    @property
    def redacted_command(self) -> str:
        return " ".join(self.redacted_argv)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def validate_avd_name(name: str) -> str:
    candidate = (name or "").strip()
    if not candidate:
        raise ValidationError("AVD name must not be empty")
    if not AVD_NAME_RE.match(candidate):
        raise ValidationError(
            f"invalid AVD name {candidate!r}: use letters, digits, '.', '_' and '-'",
        )
    return candidate


def validate_proxy_upstream(url: str) -> str:
    """Validate a credential-free proxy URL (host:port, optional scheme)."""
    candidate = (url or "").strip()
    if not candidate:
        raise ValidationError("proxy upstream URL must not be empty")
    if "@" in candidate:
        raise InstanceError(
            "refusing to pass proxy credentials on the emulator command line",
            hint="Use the local proxy bridge; credentials must never appear in argv.",
        )
    parts = urlsplit(candidate if "://" in candidate else f"http://{candidate}")
    if parts.scheme.lower() not in _SNAPSHOT_SAFE_PROXY_SCHEMES:
        raise ValidationError(f"unsupported proxy scheme {parts.scheme!r}")
    if not parts.hostname:
        raise ValidationError("proxy upstream URL is missing a host")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValidationError(f"invalid proxy port in {candidate!r}") from exc
    if port is None or not 1 <= port <= 65535:
        raise ValidationError(f"invalid proxy port in {candidate!r}")
    if parts.username or parts.password:
        raise InstanceError(
            "refusing to pass proxy credentials on the emulator command line",
            hint="Use the local proxy bridge; credentials must never appear in argv.",
        )
    return f"{parts.scheme.lower()}://{parts.hostname}:{port}"


def resolve_gpu_mode(headless: bool, configured: str | GpuMode | None = None) -> str:
    """Map the configured GPU mode to a valid emulator value for this launch."""
    value = getattr(configured, "value", configured) or GpuMode.SWIFTSHADER_INDIRECT.value
    text = str(value).strip().lower()
    allowed = {mode.value for mode in GpuMode}
    if text not in allowed:
        raise ValidationError(
            f"invalid GPU mode {value!r}",
            hint=f"valid modes: {', '.join(sorted(allowed))}",
        )
    if headless and text in _HEADLESS_GPU_FALLBACKS:
        # No window server: software rendering is the reliable headless choice.
        return GpuMode.SWIFTSHADER_INDIRECT.value
    return text


def screen_endpoint(ports: InstancePorts | None) -> str | None:
    """The screen-sharing gRPC endpoint for a port lease."""
    if ports is None or ports.grpc is None:
        return None
    return f"127.0.0.1:{int(ports.grpc)}"


def validate_ports(ports: InstancePorts) -> InstancePorts:
    if ports is None:
        raise ValidationError("instance ports are required to launch an emulator")
    if ports.console % 2:
        raise ValidationError(
            f"emulator console port must be even (got {ports.console})",
            hint="The Android emulator reserves console=even / adb=console+1 pairs.",
        )
    for name in ("console", "adb"):
        value = getattr(ports, name)
        if not isinstance(value, int) or not 1 <= value <= 65535:
            raise ValidationError(f"invalid emulator {name} port: {value!r}")
    if ports.grpc is not None and not 1 <= int(ports.grpc) <= 65535:
        raise ValidationError(f"invalid emulator gRPC port: {ports.grpc!r}")
    return ports


# ---------------------------------------------------------------------------
# argv
# ---------------------------------------------------------------------------


def build_emulator_argv(
    emulator: str | Path,
    name: str,
    ports: InstancePorts,
    *,
    headless: bool = True,
    gpu_mode: str | GpuMode | None = None,
    proxy_upstream: str | None = None,
    wipe_data: bool = False,
    no_snapshot_save: bool = True,
    grpc_use_token: bool = True,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Build the emulator argv for one instance (never contains credentials)."""
    avd_name = validate_avd_name(name)
    validate_ports(ports)
    argv: list[str] = [
        str(emulator),
        "-avd",
        avd_name,
        "-port",
        str(ports.console),
    ]
    if ports.grpc is not None:
        argv.extend(["-grpc", str(ports.grpc)])
        if grpc_use_token:
            argv.append("-grpc-use-token")
    resolved_headless = bool(headless)
    argv.extend(["-gpu", resolve_gpu_mode(resolved_headless, gpu_mode)])
    if resolved_headless:
        argv.extend(["-no-window", "-no-audio"])
    if no_snapshot_save:
        argv.append("-no-snapshot-save")
    if wipe_data:
        argv.append("-wipe-data")
    if proxy_upstream:
        argv.extend(["-http-proxy", validate_proxy_upstream(proxy_upstream)])
    for item in extra_args:
        text = str(item)
        if not text or any(character in text for character in "\r\n\x00"):
            raise ValidationError("invalid extra emulator argument")
        argv.append(text)
    return argv


def build_launch_spec(
    emulator: str | Path,
    name: str,
    ports: InstancePorts,
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    log_path: str | Path | None = None,
    headless: bool = True,
    gpu_mode: str | GpuMode | None = None,
    proxy_upstream: str | None = None,
    wipe_data: bool = False,
    no_snapshot_save: bool = True,
    grpc_use_token: bool = True,
    extra_args: Sequence[str] = (),
) -> LaunchSpec:
    """Assemble the full launch specification (argv, env, cwd, log)."""
    argv = build_emulator_argv(
        emulator,
        name,
        ports,
        headless=headless,
        gpu_mode=gpu_mode,
        proxy_upstream=proxy_upstream,
        wipe_data=wipe_data,
        no_snapshot_save=no_snapshot_save,
        grpc_use_token=grpc_use_token,
        extra_args=extra_args,
    )
    resolved_env = {str(key): str(value) for key, value in (env or {}).items()}
    resolved_cwd = Path(cwd) if cwd is not None else None
    resolved_log = Path(log_path) if log_path is not None else None
    if resolved_log is not None:
        resolved_log.parent.mkdir(parents=True, exist_ok=True)
    return LaunchSpec(
        name=name,
        argv=argv,
        env=resolved_env,
        cwd=resolved_cwd,
        log_path=resolved_log,
        headless=bool(headless),
        gpu_mode=resolve_gpu_mode(bool(headless), gpu_mode),
        proxy_upstream=validate_proxy_upstream(proxy_upstream) if proxy_upstream else None,
        wipe_data=bool(wipe_data),
    )


def launch_log_path(paths: Paths | None, name: str) -> Path | None:
    """Per-instance emulator log path under the configured log directory."""
    if paths is None or paths.log_dir is None:
        return None
    safe = _UNSAFE_FILENAME_RE.sub("_", name).strip("._") or "instance"
    return Path(paths.log_dir) / f"{safe}-emulator.log"


__all__ = [
    "AVD_NAME_RE",
    "LaunchSpec",
    "ProcessFactory",
    "ProcessHandle",
    "SubprocessProcessFactory",
    "build_emulator_argv",
    "build_launch_spec",
    "launch_log_path",
    "resolve_gpu_mode",
    "screen_endpoint",
    "validate_avd_name",
    "validate_ports",
    "validate_proxy_upstream",
]
