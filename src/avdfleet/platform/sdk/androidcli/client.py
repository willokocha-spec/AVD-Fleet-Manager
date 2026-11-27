"""Client for the new ``android`` command-line interface.

Supported commands (``contracts/fakes-spec.md`` §4.5)::

    android --version
    android sdk list [--installed] [--json]
    android sdk install <id>...
    android sdk licenses
    android avd list [--json]
    android avd create --name NAME --package PACKAGE --device DEVICE [--force]
    android avd delete --name NAME [--force]
    android emulator list [--json]

The client prefers JSON output because it is unambiguous; when a build does
not support a JSON flag it transparently retries with the table form.  It is
also the component that decides whether the toolchain is *available* at all
(missing binary / unknown version), which drives ``get_backend`` fallback to
the legacy toolchain.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from avdfleet.core.logging import redact

from ..base import (
    CUSTOM_HARDWARE_REPLY,
    DEFAULT_TIMEOUT_S,
    LICENSE_PROMPT_REPLY,
    LICENSE_TIMEOUT_S,
    LIST_TIMEOUT_S,
    MIN_ANDROIDCLI_VERSION,
    VERSION_TIMEOUT_S,
    AvdEntry,
    Runner,
    SdkError,
    SdkPackage,
    ToolNotFoundError,
    ToolResult,
    failed_command_error,
    require_ok,
    run_tool,
    version_at_least,
)
from . import parsing

CREATE_AVD_TIMEOUT_S = 300.0


class AndroidCliClient:
    """Command builder/executor for the new ``android`` CLI."""

    def __init__(
        self,
        executable: str | Path | None,
        *,
        runner: Runner | None = None,
        environ: Mapping[str, str] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.executable = Path(executable) if executable else None
        self._runner: Runner = runner or run_tool
        self._environ = dict(environ or {})
        self.timeout_s = float(timeout_s)
        self._probed = False
        self._version: str | None = None
        self._unavailable_reason = ""
        self._json_sdk_list: bool | None = None

    # -- availability / version ----------------------------------------
    def probe(self, *, refresh: bool = False) -> str | None:
        """Return the CLI version, or ``None`` when unavailable/unsupported."""
        if self._probed and not refresh:
            return self._version
        self._probed = True
        self._version = None
        self._unavailable_reason = ""
        if self.executable is None:
            self._unavailable_reason = "android CLI executable not found"
            return None
        try:
            result = self.run("--version", timeout_s=VERSION_TIMEOUT_S)
        except (ToolNotFoundError, SdkError, OSError) as exc:
            self._unavailable_reason = redact(str(exc))
            return None
        if not result.ok:
            self._unavailable_reason = f"android --version failed with exit code {result.returncode}"
            return None
        version = parsing.parse_android_version(result.combined)
        if not version:
            self._unavailable_reason = "android --version output was not recognised"
            return None
        if not version_at_least(version, MIN_ANDROIDCLI_VERSION):
            minimum = ".".join(str(part) for part in MIN_ANDROIDCLI_VERSION)
            self._unavailable_reason = f"android CLI {version} is older than the supported {minimum}"
            return None
        self._version = version
        return version

    def available(self, *, refresh: bool = False) -> bool:
        return self.probe(refresh=refresh) is not None

    @property
    def unavailable_reason(self) -> str:
        if not self._probed:
            self.probe()
        return self._unavailable_reason

    def version(self, *, refresh: bool = False) -> str:
        version = self.probe(refresh=refresh)
        if version is None:
            raise ToolNotFoundError(
                "android",
                hint=self._unavailable_reason or "The android CLI is not available.",
            )
        return version

    def invalidate(self) -> None:
        """Forget the cached probe (e.g. after an installer installed the CLI)."""
        self._probed = False
        self._version = None
        self._unavailable_reason = ""

    # -- process plumbing ----------------------------------------------
    def run(
        self,
        *args: str,
        stdin: str | None = None,
        timeout_s: float | None = None,
    ) -> ToolResult:
        if self.executable is None:
            raise ToolNotFoundError("android", hint="Install the Android CLI or use the legacy backend.")
        return self._runner(
            self.executable,
            [str(arg) for arg in args],
            stdin=stdin,
            timeout_s=self.timeout_s if timeout_s is None else float(timeout_s),
            env=self._environ or None,
        )

    # -- sdk ------------------------------------------------------------
    def sdk_list(self, *, installed_only: bool = False) -> ToolResult:
        """Return ``android sdk list`` output, preferring JSON when supported."""
        base_args = ["sdk", "list"]
        if installed_only:
            base_args.append("--installed")
        if self._json_sdk_list is not False:
            result = self.run(*base_args, "--json", timeout_s=LIST_TIMEOUT_S)
            if result.ok and parsing.extract_json(result.stdout) is not None:
                self._json_sdk_list = True
                return result
            self._json_sdk_list = False
        return self.run(*base_args, timeout_s=LIST_TIMEOUT_S)

    def sdk_list_packages(self, *, installed_only: bool = False) -> tuple[list[SdkPackage], list[SdkPackage]]:
        """Return ``(installed, available)`` packages from ``android sdk list``."""
        result = self.sdk_list(installed_only=installed_only)
        require_ok(result, "android sdk list")
        installed, available = parsing.parse_sdk_list_output(result.combined)
        if installed_only:
            flagged = [package for package in available if package.installed]
            for package in flagged:
                if not any(existing.id == package.id for existing in installed):
                    installed.append(package)
            return [
                package.model_copy(update={"installed": True}) for package in installed
            ], []
        return installed, available

    def sdk_install(
        self,
        package_ids: Sequence[str],
        *,
        accept_licenses: bool = False,
        timeout_s: float = 1800.0,
    ) -> ToolResult:
        stdin = LICENSE_PROMPT_REPLY if accept_licenses else None
        return self.run("sdk", "install", *package_ids, stdin=stdin, timeout_s=timeout_s)

    def sdk_uninstall(self, package_ids: Sequence[str]) -> ToolResult:
        """Remove packages; the released CLI calls the command ``sdk remove``."""
        result = self.run("sdk", "remove", *package_ids, timeout_s=1800.0)
        if result.ok or result.returncode != 2:
            return result
        # Tolerate builds/spec variants that spell it ``uninstall``.
        return self.run("sdk", "uninstall", *package_ids, timeout_s=1800.0)

    def sdk_licenses(self, *, timeout_s: float = LICENSE_TIMEOUT_S) -> ToolResult:
        return self.run("sdk", "licenses", stdin=LICENSE_PROMPT_REPLY, timeout_s=timeout_s)

    # -- avd ------------------------------------------------------------
    def avd_list(self) -> ToolResult:
        """Return the AVD list, preferring JSON output when supported."""
        result = self.run("avd", "list", "--json", timeout_s=LIST_TIMEOUT_S)
        if result.ok:
            return result
        return self.run("avd", "list", timeout_s=LIST_TIMEOUT_S)

    def avd_entries(self) -> list[AvdEntry]:
        result = require_ok(self.avd_list(), "android avd list")
        return parsing.parse_avd_list_output(result.combined)

    def avd_create(
        self,
        name: str,
        image_package: str,
        device: str | None = None,
        *,
        force: bool = False,
    ) -> ToolResult:
        args = ["avd", "create", "--name", name, "--package", image_package]
        if device:
            args.extend(["--device", device])
        if force:
            args.append("--force")
        return self.run(*args, stdin=CUSTOM_HARDWARE_REPLY, timeout_s=CREATE_AVD_TIMEOUT_S)

    def avd_delete(self, name: str, *, force: bool = False) -> ToolResult:
        if force:
            result = self.run("avd", "delete", "--name", name, "--force")
            if result.ok or result.returncode != 2:
                return result
            # Older builds treat --force as a usage error; fall back to plain delete.
            return self.run("avd", "delete", "--name", name)
        return self.run("avd", "delete", "--name", name)

    # -- emulator -------------------------------------------------------
    def emulator_list(self) -> list[dict[str, Any]]:
        result = self.run("emulator", "list", timeout_s=LIST_TIMEOUT_S)
        if result.ok:
            emulators = parsing.parse_emulator_list_output(result.combined)
            if emulators:
                return emulators
        result = self.run("emulator", "list", "--json", timeout_s=LIST_TIMEOUT_S)
        if not result.ok:
            raise failed_command_error(result, "android emulator list")
        return parsing.parse_emulator_list_output(result.combined)


__all__ = ["CREATE_AVD_TIMEOUT_S", "AndroidCliClient"]
