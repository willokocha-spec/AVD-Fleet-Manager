"""Shared fakes for the Root & Instrumentation QA suite.

The fakes stop at the same boundary the production code uses: the ``adb``
process call. ``ScriptedAdb`` answers commands from a dictionary, and
``ShellAdb`` additionally splits a command on ``;`` the way the device shell
would, so an unescaped injection is visible as an extra executed command rather
than as a string assertion.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from avdfleet.instrumentation import (
    DeviceProber,
    InstrumentationRepository,
    InstrumentationService,
    ModuleLibrary,
    TargetDevice,
)
from avdfleet.instrumentation.capabilities import CommandResult
from avdfleet.store import Store

MAGISK_PROPS = (
    "[ro.build.type]: [user]\n"
    "[ro.build.version.sdk]: [34]\n"
    "[ro.product.cpu.abi]: [x86_64]\n"
    "[ro.debuggable]: [0]\n"
)
API25_PROPS = (
    "[ro.build.type]: [userdebug]\n"
    "[ro.build.version.sdk]: [25]\n"
    "[ro.product.cpu.abi]: [x86]\n"
    "[ro.debuggable]: [1]\n"
)
LSPOSED_PROP = "id=zygisk_lsposed\nname=LSPosed\nversion=v1.9.2 (7024)\n"


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    """Build an in-memory ZIP/APK archive."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def magisk_zip(**prop: str) -> bytes:
    """Build a Magisk module ZIP whose ``module.prop`` carries *prop*."""
    fields = {"id": "alpha", "name": "Alpha", "version": "1.0", "versionCode": "1"}
    fields.update(prop)
    body = "".join(f"{key}={value}\n" for key, value in fields.items())
    return zip_bytes({"module.prop": body.encode("utf-8")})


class ScriptedAdb:
    """Deterministic stand-in for the ``adb`` process boundary."""

    def __init__(
        self,
        responses: dict[str, tuple[int, str]] | None = None,
        *,
        default: tuple[int, str] = (1, ""),
    ) -> None:
        self.responses = dict(responses or {})
        self.default = default
        self.commands: list[str] = []

    @staticmethod
    def inner(argv: list[str]) -> str:
        """Unwrap ``root_shell``'s ``su -c "..."`` quoting."""
        command = argv[-1]
        prefix = 'su -c "'
        if command.startswith(prefix) and command.endswith('"'):
            return command[len(prefix) : -1].replace('\\"', '"')
        return command

    def __call__(self, argv: list[str]) -> CommandResult:
        command = self.inner(argv)
        self.commands.append(command)
        returncode, stdout = self.responses.get(command, self.default)
        return CommandResult(argv=argv, returncode=returncode, stdout=stdout)


class ShellAdb(ScriptedAdb):
    """``ScriptedAdb`` that also models the device shell's ``;`` separator.

    ``adb shell`` hands the whole string to the device's shell, so a value
    interpolated without validation that contains ``;`` becomes a second
    command. Recording every resulting command makes that observable here.
    """

    def __init__(
        self,
        responses: dict[str, tuple[int, str]] | None = None,
        *,
        default: tuple[int, str] = (0, "yes\n"),
    ) -> None:
        super().__init__(responses, default=default)
        self.executed: list[str] = []

    def __call__(self, argv: list[str]) -> CommandResult:
        command = self.inner(argv)
        self.commands.append(command)
        for part in command.split(";"):
            stripped = part.strip()
            if stripped and not stripped.startswith("#"):
                self.executed.append(stripped)
        returncode, stdout = self.responses.get(command, self.default)
        return CommandResult(argv=argv, returncode=returncode, stdout=stdout)


def magisk_script(*, modules: dict[str, str] | None = None) -> ScriptedAdb:
    """A Magisk-rooted device with a complete environment and Zygisk active."""
    listing = "".join(f"{name}\n" for name in (modules or {}))
    responses: dict[str, tuple[int, str]] = {
        "getprop": (0, MAGISK_PROPS),
        "su -c id": (0, "uid=0(root) gid=0(root) context=u:r:magisk:s0\n"),
        "magisk -V": (0, "27000\n"),
        "magisk -v": (0, "27000\n"),
        "ls /data/adb/magisk": (0, "magisk32\nmagisk64\n"),
        "magisk --sqlite \"select value from settings where key='zygisk'\"": (0, "value=1\n"),
        "getprop ro.zygisk.active": (0, "\n"),
        "ps -A": (0, "USER PID PPID NAME\nroot 1234 1 zygiskd64\n"),
        "ls /data/adb/modules": (0, listing),
        "mount": (0, "/dev/block/by-name/system /system ext4 ro,seclabel 0 0\n"),
    }
    for name, prop in (modules or {}).items():
        base = f"/data/adb/modules/{name}"
        responses[f"cat {base}/module.prop"] = (0, prop)
        responses[f"[ -e {base}/disable ] && echo yes"] = (1, "")
        responses[f"[ -e {base}/remove ] && echo yes"] = (1, "")
        responses[f"ls {base}/zygisk 2>/dev/null"] = (1, "")
    return ScriptedAdb(responses)


def target(api_level: int = 34) -> TargetDevice:
    """One running target, as the service resolver would return it."""
    return TargetDevice(
        instance="avd-01",
        serial="emulator-5554",
        running=True,
        boot_completed=True,
        api_level=api_level,
    )


@contextmanager
def open_service(
    tmp_path: Path,
    runner: Any,
    *,
    target_device: TargetDevice | None = None,
    http_get: Any | None = None,
) -> Iterator[InstrumentationService]:
    """A service wired to an in-memory store, a temp library and *runner*."""
    store = Store(":memory:")
    store.initialize()
    try:
        repository = InstrumentationRepository(store.database)
        library = ModuleLibrary(tmp_path / "modules", http_get=http_get)
        resolved = target_device or target()

        def resolve(name: str) -> TargetDevice:
            return replace(resolved, instance=name)

        yield InstrumentationService(
            repository,
            library,
            DeviceProber(runner=runner),
            resolve,
            settings=store.settings,
        )
    finally:
        store.close()


class FakeClock:
    """Virtual clock so boot polls and reboot budgets cost no real time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
