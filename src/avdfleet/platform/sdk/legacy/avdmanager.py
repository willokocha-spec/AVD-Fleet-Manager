"""Wrapper around the legacy ``avdmanager`` executable.

``avdmanager`` is interactive unless told otherwise: it asks
``Do you wish to create a custom hardware profile? [no]`` when no device
profile is given.  Every creation therefore pipes :data:`CUSTOM_HARDWARE_REPLY`
to stdin so fleet deployment can never hang on a prompt.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from avdfleet.core.childenv import child_environment

from ..base import (
    CUSTOM_HARDWARE_REPLY,
    DEFAULT_TIMEOUT_S,
    VERSION_TIMEOUT_S,
    AvdEntry,
    Runner,
    ToolResult,
    require_ok,
    run_tool,
)
from . import parsing

DEFAULT_AVDMANAGER_TIMEOUT_S = DEFAULT_TIMEOUT_S
CREATE_AVD_TIMEOUT_S = 300.0


class AvdManager:
    """Command builder/executor for ``avdmanager``."""

    def __init__(
        self,
        executable: str | Path,
        *,
        runner: Runner | None = None,
        environ: Mapping[str, str] | None = None,
        timeout_s: float = DEFAULT_AVDMANAGER_TIMEOUT_S,
    ) -> None:
        self.executable = Path(executable)
        self._runner: Runner = runner or run_tool
        self._environ = dict(environ or {})
        self.timeout_s = float(timeout_s)

    # -- process plumbing ----------------------------------------------
    def run(
        self,
        *args: str,
        stdin: str | None = None,
        timeout_s: float | None = None,
    ) -> ToolResult:
        return self._runner(
            self.executable,
            [str(arg) for arg in args],
            stdin=stdin,
            timeout_s=self.timeout_s if timeout_s is None else float(timeout_s),
            env=child_environment(self._environ),
        )

    def version(self, *, timeout_s: float = VERSION_TIMEOUT_S) -> str:
        """Return ``avdmanager --version`` output (newer SDKs support it)."""
        result = require_ok(self.run("--version", timeout_s=timeout_s), "avdmanager --version")
        return parsing.parse_version(result.combined)

    # -- queries --------------------------------------------------------
    def list_avds(self, *, timeout_s: float | None = None) -> list[AvdEntry]:
        result = require_ok(
            self.run("list", "avd", timeout_s=timeout_s),
            "avdmanager list avd",
        )
        return parsing.parse_avdmanager_list(result.combined)

    def list_avd_names(self, *, timeout_s: float | None = None) -> list[str]:
        return [entry.name for entry in self.list_avds(timeout_s=timeout_s)]

    def list_devices(self, *, timeout_s: float | None = None) -> list[dict[str, str]]:
        result = require_ok(
            self.run("list", "device", timeout_s=timeout_s),
            "avdmanager list device",
        )
        return parsing.parse_avdmanager_devices(result.combined)

    # -- mutations ------------------------------------------------------
    def create(
        self,
        name: str,
        image_package: str,
        device: str | None = None,
        *,
        force: bool = False,
        path: str | Path | None = None,
        sd_card: str | Path | None = None,
        timeout_s: float = CREATE_AVD_TIMEOUT_S,
    ) -> ToolResult:
        """Create an AVD, answering the custom-hardware prompt with ``no``."""
        args = ["create", "avd", "-n", _validate_name(name), "-k", _validate_package(image_package)]
        if device:
            args.extend(["-d", str(device)])
        if path:
            args.extend(["--path", str(path)])
        if sd_card:
            args.extend(["--sd-card", str(sd_card)])
        if force:
            args.append("--force")
        return self.run(*args, stdin=CUSTOM_HARDWARE_REPLY, timeout_s=timeout_s)

    def delete(self, name: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> ToolResult:
        return self.run("delete", "avd", "-n", _validate_name(name), timeout_s=timeout_s)

    def move(self, name: str, new_path: str | Path, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> ToolResult:
        return self.run(
            "move", "avd", "-n", _validate_name(name), "-p", str(new_path), timeout_s=timeout_s
        )


def _validate_name(name: str) -> str:
    candidate = (name or "").strip()
    if not candidate:
        raise ValueError("AVD name must not be empty")
    if "/" in candidate or "\\" in candidate or "\x00" in candidate:
        raise ValueError(f"AVD name must not contain path separators: {name!r}")
    return candidate


def _validate_package(package: str) -> str:
    candidate = (package or "").strip()
    if not candidate:
        raise ValueError("system image package must not be empty")
    if any(character in candidate for character in ("\n", "\r", "\x00")):
        raise ValueError("system image package must not contain control characters")
    return candidate


__all__ = ["CREATE_AVD_TIMEOUT_S", "DEFAULT_AVDMANAGER_TIMEOUT_S", "AvdManager"]
