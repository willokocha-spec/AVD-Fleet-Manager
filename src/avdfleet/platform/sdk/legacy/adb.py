"""Wrapper around the legacy ``adb`` executable.

Covers the commands the runtime and OOBE services need: device discovery,
``wait-for-device``, ``shell getprop``, ``emu`` control, forwards and package
install/uninstall.  The wrapper is stateless apart from an optional default
serial; every mutating call takes the ``adb`` lock implicitly because the adb
server serialises commands.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path

from avdfleet.core.childenv import child_environment

from ..base import (
    DEFAULT_TIMEOUT_S,
    Runner,
    ToolResult,
    failed_command_error,
    require_ok,
    run_tool,
)
from . import parsing
from .parsing import AdbDevice

WAIT_FOR_DEVICE_TIMEOUT_S = 120.0
SHELL_TIMEOUT_S = 60.0
DEVICES_TIMEOUT_S = 30.0


class Adb:
    """Command builder/executor for ``adb``."""

    def __init__(
        self,
        executable: str | Path,
        *,
        serial: str | None = None,
        runner: Runner | None = None,
        environ: Mapping[str, str] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.executable = Path(executable)
        self.serial = serial
        self._runner: Runner = runner or run_tool
        self._environ = dict(environ or {})
        self.timeout_s = float(timeout_s)

    # -- process plumbing ----------------------------------------------
    def select(self, serial: str | None) -> Adb:
        """Return a copy of this wrapper bound to another serial."""
        return Adb(
            self.executable,
            serial=serial,
            runner=self._runner,
            environ=self._environ,
            timeout_s=self.timeout_s,
        )

    def run(
        self,
        *args: str,
        serial: str | None = None,
        stdin: str | None = None,
        timeout_s: float | None = None,
    ) -> ToolResult:
        target = serial or self.serial
        argv: list[str] = []
        if target:
            argv.extend(["-s", target])
        argv.extend(str(arg) for arg in args)
        return self._runner(
            self.executable,
            argv,
            stdin=stdin,
            timeout_s=self.timeout_s if timeout_s is None else float(timeout_s),
            env=child_environment(self._environ),
        )

    # -- server ---------------------------------------------------------
    def version(self, *, timeout_s: float = DEVICES_TIMEOUT_S) -> str:
        result = require_ok(self.run("version", timeout_s=timeout_s), "adb version")
        return parsing.parse_adb_version(result.combined)

    def start_server(self, *, timeout_s: float = DEVICES_TIMEOUT_S) -> ToolResult:
        return self.run("start-server", timeout_s=timeout_s)

    def kill_server(self, *, timeout_s: float = DEVICES_TIMEOUT_S) -> ToolResult:
        return self.run("kill-server", timeout_s=timeout_s)

    # -- discovery ------------------------------------------------------
    def devices(self, *, long: bool = True, timeout_s: float = DEVICES_TIMEOUT_S) -> list[AdbDevice]:
        args = ["devices", "-l"] if long else ["devices"]
        result = require_ok(self.run(*args, timeout_s=timeout_s), "adb devices")
        return parsing.parse_adb_devices(result.combined)

    def serials(self, *, timeout_s: float = DEVICES_TIMEOUT_S) -> list[str]:
        return [device.serial for device in self.devices(timeout_s=timeout_s)]

    def get_state(self, *, serial: str | None = None, timeout_s: float = DEVICES_TIMEOUT_S) -> str:
        result = self.run("get-state", serial=serial, timeout_s=timeout_s)
        return result.stdout.strip() if result.ok else ""

    def wait_for_device(self, *, serial: str | None = None, timeout_s: float = WAIT_FOR_DEVICE_TIMEOUT_S) -> str:
        """Block until the device is available, then return its state."""
        result = require_ok(
            self.run("wait-for-device", serial=serial, timeout_s=timeout_s + 5.0),
            f"adb wait-for-device ({serial or 'default device'})",
        )
        state = result.stdout.strip() or self.get_state(serial=serial)
        return state or "device"

    # -- shell ----------------------------------------------------------
    def shell_result(
        self,
        command: Sequence[str] | str,
        *,
        serial: str | None = None,
        timeout_s: float = SHELL_TIMEOUT_S,
    ) -> ToolResult:
        args = _shell_args(command)
        return self.run("shell", *args, serial=serial, timeout_s=timeout_s)

    def shell(
        self,
        command: Sequence[str] | str,
        *,
        serial: str | None = None,
        timeout_s: float = SHELL_TIMEOUT_S,
    ) -> str:
        """Run an adb shell command and return stdout (raises on failure)."""
        result = require_ok(
            self.shell_result(command, serial=serial, timeout_s=timeout_s),
            f"adb shell {' '.join(_shell_args(command))}",
        )
        return parsing.adb_shell_output(result)

    def getprop(self, prop: str, *, serial: str | None = None, timeout_s: float = SHELL_TIMEOUT_S) -> str:
        return self.shell(["getprop", prop], serial=serial, timeout_s=timeout_s).strip()

    def boot_completed(self, *, serial: str | None = None, timeout_s: float = SHELL_TIMEOUT_S) -> bool:
        return self.getprop("sys.boot_completed", serial=serial, timeout_s=timeout_s) == "1"

    # -- emulator control ----------------------------------------------
    def emu(self, *command: str, serial: str | None = None, timeout_s: float = SHELL_TIMEOUT_S) -> str:
        result = self.run("emu", *command, serial=serial, timeout_s=timeout_s)
        if not result.ok:
            raise failed_command_error(result, f"adb emu {' '.join(command)}")
        return result.stdout.strip()

    def avd_name(self, *, serial: str | None = None, timeout_s: float = SHELL_TIMEOUT_S) -> str:
        return self.emu("avd", "name", serial=serial, timeout_s=timeout_s)

    def kill_emulator(self, *, serial: str | None = None, timeout_s: float = SHELL_TIMEOUT_S) -> None:
        self.emu("kill", serial=serial, timeout_s=timeout_s)

    # -- packages / port forwarding ------------------------------------
    def install(
        self,
        apk_path: str | Path,
        *,
        serial: str | None = None,
        reinstall: bool = True,
        timeout_s: float = 300.0,
    ) -> str:
        args = ["install"]
        if reinstall:
            args.append("-r")
        args.append(str(apk_path))
        result = require_ok(self.run(*args, serial=serial, timeout_s=timeout_s), "adb install")
        return result.stdout.strip()

    def uninstall(self, package: str, *, serial: str | None = None, timeout_s: float = 120.0) -> str:
        result = require_ok(
            self.run("uninstall", package, serial=serial, timeout_s=timeout_s), "adb uninstall"
        )
        return result.stdout.strip()

    def forward(self, local: str, remote: str, *, serial: str | None = None) -> str:
        result = require_ok(
            self.run("forward", local, remote, serial=serial), "adb forward"
        )
        return result.stdout.strip()

    def forward_remove_all(self, *, serial: str | None = None) -> ToolResult:
        return self.run("forward", "--remove-all", serial=serial)

    def reverse(self, remote: str, local: str, *, serial: str | None = None) -> str:
        result = require_ok(self.run("reverse", remote, local, serial=serial), "adb reverse")
        return result.stdout.strip()


def _shell_args(command: Sequence[str] | str) -> list[str]:
    if isinstance(command, str):
        return shlex.split(command, posix=True)
    return [str(part) for part in command]


__all__ = [
    "DEVICES_TIMEOUT_S",
    "SHELL_TIMEOUT_S",
    "WAIT_FOR_DEVICE_TIMEOUT_S",
    "Adb",
    "AdbDevice",
]
