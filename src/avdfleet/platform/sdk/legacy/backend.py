"""Legacy Android SDK backend: ``sdkmanager``/``avdmanager``/``adb``/``emulator``.

This is the reference implementation of
:class:`avdfleet.core.interfaces.SdkBackend`.  The new ``android`` CLI backend
implements exactly the same surface so A1/N1 can switch by configuration only.

Failure policy (kept identical in both backends):

* environment problems (tool missing, OS error) raise
  :class:`~avdfleet.platform.sdk.base.ToolNotFoundError`/``SdkError``;
* "expected" command failures for methods returning ``OperationResult`` are
  reported as ``ok=False`` with a redacted message (install/uninstall/licenses);
* data-returning methods raise a redacted ``CommandError``;
* AVD name conflicts raise ``ConflictError`` and deleting a missing AVD raises
  ``NotFoundError`` unless ``force=True`` makes deletion idempotent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from avdfleet.core.config import Config
from avdfleet.core.errors import CommandError, ConflictError, NotFoundError, ToolError
from avdfleet.core.logging import redact
from avdfleet.core.models import (
    DeviceProfile,
    OperationResult,
    SdkPackage,
    SdkPaths,
    SystemImage,
)

from ..base import (
    INSTALL_TIMEOUT_S,
    LICENSE_TIMEOUT_S,
    BaseSdkBackend,
    Runner,
    ToolNotFoundError,
    failed_command_error,
    images_from_packages,
    update_ini_file,
)
from . import parsing
from .adb import Adb
from .avdmanager import AvdManager
from .emulator import Emulator
from .sdkmanager import SdkManager


class LegacySdkBackend(BaseSdkBackend):
    """The classic Android SDK command-line toolchain."""

    name = "legacy"

    def __init__(
        self,
        config: Config | None = None,
        sdk_paths: SdkPaths | None = None,
        *,
        host: Any = None,
        environ: Mapping[str, str] | None = None,
        runner: Runner | None = None,
    ) -> None:
        super().__init__(config, sdk_paths, host=host, environ=environ, runner=runner)

    # -- tool clients ---------------------------------------------------
    def sdkmanager(self) -> SdkManager:
        return SdkManager(
            self.executable("sdkmanager"),
            self.sdk_paths.sdk_root or None,
            runner=self._runner,
            environ=self.process_env(),
        )

    def avdmanager(self) -> AvdManager:
        return AvdManager(
            self.executable("avdmanager"),
            runner=self._runner,
            environ=self.process_env(),
        )

    def adb(self, serial: str | None = None) -> Adb:
        return Adb(
            self.adb_executable(),
            serial=serial,
            runner=self._runner,
            environ=self.process_env(),
        )

    def emulator(self) -> Emulator:
        return Emulator(
            self.emulator_executable(),
            avd_home=self.avd_home,
            runner=self._runner,
            environ=self.process_env(),
        )

    # -- version checks -------------------------------------------------
    def tool_versions(self) -> dict[str, str]:
        """Best-effort versions of every resolvable tool (never raises)."""
        versions: dict[str, str] = {}
        probes = (
            ("sdkmanager", lambda: self.sdkmanager().version()),
            ("avdmanager", lambda: self.avdmanager().version()),
            ("adb", lambda: self.adb().version()),
            ("emulator", lambda: self.emulator().version()),
        )
        for tool, probe in probes:
            try:
                version = probe()
            except (ToolError, OSError, ValueError):
                continue
            if version:
                versions[tool] = version
        return versions

    def sdkmanager_version(self) -> str:
        return self.sdkmanager().version()

    def emulator_version(self) -> str:
        return self.emulator().version()

    # -- packages -------------------------------------------------------
    def list_packages(self) -> list[SdkPackage]:
        installed, available = self.sdkmanager().list()
        merged: dict[str, SdkPackage] = {package.id: package for package in available}
        for package in installed:
            merged[package.id] = package.model_copy(update={"installed": True})
        return sorted(merged.values(), key=lambda package: package.id)

    def list_installed(self) -> list[SdkPackage]:
        try:
            installed, _ = self.sdkmanager().list(installed_only=True)
        except CommandError:
            # Very old sdkmanager builds lack --list_installed.
            installed = [package for package in self.list_packages() if package.installed]
        merged: dict[str, SdkPackage] = {
            package.id: package.model_copy(update={"installed": True}) for package in installed
        }
        return sorted(merged.values(), key=lambda package: package.id)

    def install_packages(
        self,
        package_ids: Sequence[str],
        *,
        accept_licenses: bool = False,
        dry_run: bool = False,
        timeout_s: int = 1800,
    ) -> OperationResult:
        ids = _clean_ids(package_ids)
        if not ids:
            return OperationResult(ok=True, message="No packages to install", data={"package_ids": []})
        if dry_run:
            return OperationResult(
                ok=True,
                message=f"Dry run: would install {len(ids)} package(s)",
                data={
                    "dry_run": True,
                    "package_ids": ids,
                    "accept_licenses": accept_licenses,
                    "argv": self._planned_sdkmanager_argv("--install", *ids),
                },
            )
        result = self.sdkmanager().install(
            ids,
            accept_licenses=accept_licenses,
            timeout_s=float(timeout_s or INSTALL_TIMEOUT_S),
        )
        failed = parsing.install_failed(result)
        data = result.to_dict()
        data.update(
            {
                "package_ids": ids,
                "accept_licenses": accept_licenses,
                "license_required": bool(parsing.license_required(result) and not accept_licenses),
            }
        )
        return OperationResult(
            ok=result.ok and not failed,
            message=redact(parsing.install_message(result)),
            data=data,
        )

    def uninstall_packages(
        self,
        package_ids: Sequence[str],
        *,
        dry_run: bool = False,
    ) -> OperationResult:
        ids = _clean_ids(package_ids)
        if not ids:
            return OperationResult(ok=True, message="No packages to uninstall", data={"package_ids": []})
        if dry_run:
            return OperationResult(
                ok=True,
                message=f"Dry run: would uninstall {len(ids)} package(s)",
                data={
                    "dry_run": True,
                    "package_ids": ids,
                    "argv": self._planned_sdkmanager_argv("--uninstall", *ids),
                },
            )
        result = self.sdkmanager().uninstall(ids)
        failed = parsing.install_failed(result)
        data = result.to_dict()
        data["package_ids"] = ids
        return OperationResult(
            ok=result.ok and not failed,
            message=redact(parsing.install_message(result)),
            data=data,
        )

    def accept_licenses(self, *, timeout_s: int = 300) -> OperationResult:
        result = self.sdkmanager().accept_licenses(timeout_s=float(timeout_s or LICENSE_TIMEOUT_S))
        data = result.to_dict()
        return OperationResult(
            ok=parsing.licenses_accepted(result),
            message=redact(parsing.license_message(result)),
            data=data,
        )

    # -- images ---------------------------------------------------------
    def list_system_images(self) -> list[SystemImage]:
        return images_from_packages(self.list_packages(), self.sdk_paths.sdk_root)

    # -- AVDs -----------------------------------------------------------
    def list_avds(self) -> list[str]:
        avdmanager_error: ToolError | None = None
        try:
            names = self.avdmanager().list_avd_names()
        except (ToolError, ValueError) as exc:
            avdmanager_error = exc if isinstance(exc, ToolError) else None
        else:
            return sorted(set(names))
        # Older installs or unused AVD homes: ask the emulator itself.
        try:
            names = self.emulator().list_avds()
        except (ToolError, ValueError):
            if avdmanager_error is not None:
                raise avdmanager_error from None
            raise
        return sorted(set(names))

    def create_avd(
        self,
        name: str,
        image_package: str,
        device_id: str = "pixel_7",
        *,
        config: dict[str, str] | None = None,
        force: bool = False,
    ) -> str:
        avd_name = _validate_avd_name(name)
        package = _validate_image_package(image_package)
        result = self.avdmanager().create(
            avd_name,
            package,
            device=device_id or None,
            force=force,
        )
        if not result.ok:
            if parsing.duplicate_avd(result):
                raise ConflictError(
                    f"AVD {avd_name!r} already exists",
                    hint="Pass force=True to recreate it, or delete it first.",
                )
            raise failed_command_error(result, f"creating AVD {avd_name!r}")
        reported = parsing.created_avd_path(result.combined)
        created = Path(reported) if reported else self.avd_dir(avd_name)
        if config:
            self._apply_avd_config(created, config)
        return str(created)

    def delete_avd(self, name: str, *, force: bool = False) -> OperationResult:
        avd_name = _validate_avd_name(name)
        result = self.avdmanager().delete(avd_name)
        if result.ok:
            return OperationResult(
                ok=True,
                message=f"AVD {avd_name!r} deleted",
                data=result.to_dict(),
            )
        if parsing.missing_avd(result):
            if force:
                return OperationResult(
                    ok=True,
                    message=f"AVD {avd_name!r} did not exist (nothing to delete)",
                    data=result.to_dict(),
                )
            raise NotFoundError(
                f"AVD {avd_name!r} does not exist",
                hint="List AVDs with `avdmanager list avd`.",
            )
        raise failed_command_error(result, f"deleting AVD {avd_name!r}")

    def list_device_profiles(self) -> list[DeviceProfile]:
        """Device profiles from ``avdmanager list device`` (best effort).

        The shared base implementation already prefers ``avdmanager`` and
        falls back to the built-in catalogue, so the legacy backend inherits it
        rather than duplicating the probe.
        """
        return super().list_device_profiles()

    # -- extras used by A1/N1 ------------------------------------------
    def avd_entries(self) -> list[parsing.AvdEntry]:
        """Structured AVD list including target/tag/ABI (legacy only)."""
        return self.avdmanager().list_avds()

    def emulator_list_avds(self) -> list[str]:
        return sorted(set(self.emulator().list_avds()))

    def adb_devices(self) -> list[parsing.AdbDevice]:  # type: ignore[override]
        return self.adb().devices()

    # -- internal helpers ----------------------------------------------
    def _apply_avd_config(self, avd_dir: Path, overrides: Mapping[str, Any]) -> Path:
        return update_ini_file(Path(avd_dir) / "config.ini", overrides)

    def _planned_sdkmanager_argv(self, *args: str) -> list[str]:
        """Argv a dry-run would execute, resolved when possible."""
        try:
            argv = self.sdkmanager().argv_for(*args)
        except ToolNotFoundError:
            argv = ["sdkmanager"]
            if self.sdk_paths.sdk_root:
                argv.append(f"--sdk_root={self.sdk_paths.sdk_root}")
            argv.extend(args)
        return [str(redact(part)) for part in argv]


def _clean_ids(package_ids: Sequence[str]) -> list[str]:
    return [str(package_id).strip() for package_id in package_ids if str(package_id).strip()]


def _validate_avd_name(name: str) -> str:
    candidate = (name or "").strip()
    if not candidate:
        raise ValueError("AVD name must not be empty")
    if any(character in candidate for character in ("/", "\\", "\n", "\r", "\x00")):
        raise ValueError(f"AVD name contains invalid characters: {name!r}")
    return candidate


def _validate_image_package(package: str) -> str:
    candidate = (package or "").strip()
    if not candidate:
        raise ValueError("system image package must not be empty")
    if any(character in candidate for character in ("\n", "\r", "\x00")):
        raise ValueError("system image package must not contain control characters")
    return candidate


__all__ = ["LegacySdkBackend"]
