"""Wrapper around the legacy ``emulator`` executable.

The emulator is special: it is a long-running process that N1 supervises, so
this wrapper exposes both a **command builder** (pure, testable, used to log
the exact argv before launch) and a thin ``launch`` that starts the process
detached from our stdin.  Proxy URLs are passed verbatim (the emulator needs
the credentials to authenticate upstream) but are never logged by this module.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from avdfleet.core.childenv import child_environment
from avdfleet.core.models import GpuMode

from ..base import DEFAULT_TIMEOUT_S, Runner, ToolResult, require_ok, run_tool
from . import parsing

VERSION_TIMEOUT_S = 60.0
LIST_AVDS_TIMEOUT_S = 60.0


class Emulator:
    """Command builder/executor for ``emulator``."""

    def __init__(
        self,
        executable: str | Path,
        *,
        avd_home: str | Path | None = None,
        runner: Runner | None = None,
        environ: Mapping[str, str] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.executable = Path(executable)
        self.avd_home = Path(avd_home) if avd_home else None
        self._runner: Runner = runner or run_tool
        self._environ = dict(environ or {})
        self.timeout_s = float(timeout_s)

    # -- process plumbing ----------------------------------------------
    def run(self, *args: str, timeout_s: float | None = None) -> ToolResult:
        return self._runner(
            self.executable,
            [str(arg) for arg in args],
            stdin=None,
            timeout_s=self.timeout_s if timeout_s is None else float(timeout_s),
            env=child_environment(self._environ),
        )

    # -- queries --------------------------------------------------------
    def version(self, *, timeout_s: float = VERSION_TIMEOUT_S) -> str:
        result = require_ok(self.run("-version", timeout_s=timeout_s), "emulator -version")
        version = parsing.parse_emulator_version(result.combined)
        if not version:
            from ..base import failed_command_error

            raise failed_command_error(result, "emulator -version returned no version")
        return version

    def list_avds(self, *, timeout_s: float = LIST_AVDS_TIMEOUT_S) -> list[str]:
        result = require_ok(self.run("-list-avds", timeout_s=timeout_s), "emulator -list-avds")
        return parsing.parse_emulator_avd_list(result.combined)

    def is_running(self) -> bool:  # pragma: no cover - process table check is N1's job
        raise NotImplementedError("use avdfleet.platform.runtime for process supervision")

    # -- launch ---------------------------------------------------------
    def build_launch_argv(
        self,
        avd_name: str,
        *,
        port: int | None = None,
        gpu_mode: GpuMode | str | None = GpuMode.SWIFTSHADER_INDIRECT,
        headless: bool = True,
        proxy_url: str | None = None,
        snapshot: str | None = None,
        wipe_data: bool = False,
        no_snapshot: bool = False,
        no_boot_anim: bool = True,
        no_audio: bool = True,
        read_only: bool = False,
        grpc_port: int | None = None,
        extra_args: Sequence[str] = (),
    ) -> list[str]:
        """Build a deterministic emulator argv (including the executable).

        ``snapshot`` and ``wipe_data`` are mutually exclusive; ``port`` must be
        even because it is the emulator console port (adb is ``port + 1``).
        """
        name = _validate_launch_name(avd_name)
        if snapshot and wipe_data:
            raise ValueError("snapshot and wipe_data cannot be combined")
        argv: list[str] = [str(self.executable), "-avd", name]
        if port is not None:
            if port % 2 != 0:
                raise ValueError(f"emulator console port must be even, got {port}")
            argv.extend(["-port", str(port)])
        if gpu_mode:
            argv.extend(["-gpu", str(getattr(gpu_mode, "value", gpu_mode))])
        if headless:
            argv.append("-no-window")
        if proxy_url:
            argv.extend(["-http-proxy", _validate_proxy_url(proxy_url)])
        if wipe_data:
            argv.append("-wipe-data")
        elif snapshot:
            argv.extend(["-snapshot", _validate_launch_name(snapshot)])
        elif no_snapshot:
            argv.append("-no-snapshot")
        if no_boot_anim:
            argv.append("-no-boot-anim")
        if no_audio:
            argv.append("-no-audio")
        if read_only:
            argv.append("-read-only")
        if grpc_port is not None:
            argv.extend(["-grpc", str(int(grpc_port))])
        argv.extend(str(arg) for arg in extra_args)
        return argv

    def launch(
        self,
        argv: Sequence[str],
        *,
        log_path: str | Path | None = None,
        cwd: str | Path | None = None,
    ) -> subprocess.Popen[bytes]:
        """Start the emulator process; returns the live :class:`subprocess.Popen`."""
        command = [str(item) for item in argv]
        if not command:
            raise ValueError("emulator argv must not be empty")
        stdout: Any = subprocess.DEVNULL
        log_handle = None
        if log_path is not None:
            path = Path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(path, "ab")  # noqa: SIM115 - handed to the child process
            stdout = log_handle
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=subprocess.STDOUT,
                cwd=str(cwd) if cwd is not None else None,
                env=child_environment(self._environ),
            )
        finally:
            if log_handle is not None:
                log_handle.close()
        return process

    def launch_avd(
        self,
        avd_name: str,
        *,
        log_path: str | Path | None = None,
        **kwargs: object,
    ) -> tuple[subprocess.Popen[bytes], list[str]]:
        """Build and launch in one call; returns ``(process, argv)`` for logging."""
        argv = self.build_launch_argv(avd_name, **kwargs)  # type: ignore[arg-type]
        return self.launch(argv, log_path=log_path), argv


def _validate_launch_name(value: str) -> str:
    candidate = (value or "").strip()
    if not candidate:
        raise ValueError("AVD name must not be empty")
    if any(character in candidate for character in ("\n", "\r", "\x00")):
        raise ValueError("AVD name must not contain control characters")
    if "/" in candidate or "\\" in candidate:
        raise ValueError(f"AVD name must not contain path separators: {value!r}")
    return candidate


def _validate_proxy_url(value: str) -> str:
    candidate = (value or "").strip()
    if not candidate:
        raise ValueError("proxy URL must not be empty")
    if any(character in candidate for character in ("\n", "\r", "\x00")):
        raise ValueError("proxy URL must not contain control characters")
    return candidate


__all__ = ["LIST_AVDS_TIMEOUT_S", "VERSION_TIMEOUT_S", "Emulator"]
