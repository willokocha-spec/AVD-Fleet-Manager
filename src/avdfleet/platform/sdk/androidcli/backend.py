"""New Android CLI backend (``android``), the preferred toolchain.

It implements exactly the same protocol surface as
:class:`~avdfleet.platform.sdk.legacy.backend.LegacySdkBackend`, so A1/N1 can
switch toolchains through :func:`avdfleet.platform.sdk.base.get_backend`
without knowing which one is active.  When the CLI is missing or too old,
``available()`` is ``False`` and the selector falls back to legacy; an
explicit ``prefer="androidcli"`` request records why in ``fallback_reason``.

Where the new CLI has no equivalent command (package uninstall on some builds,
device-profile listing) the backend degrades to the legacy executable or the
built-in catalogue instead of failing the caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from avdfleet.core.config import Config
from avdfleet.core.errors import ConflictError, NotFoundError, ToolError
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
    AvdEntry,
    BaseSdkBackend,
    Runner,
    ToolNotFoundError,
    failed_command_error,
    images_from_packages,
    update_ini_file,
)
from ..legacy import parsing as legacy_parsing
from ..legacy.sdkmanager import SdkManager
from .client import AndroidCliClient

#: Commands/features the ``android`` CLI (>= 1.0.0) provides to avdfleet.
SUPPORTED_FEATURES = frozenset(
    {
        "sdk_list",
        "sdk_install",
        "sdk_licenses",
        "avd_list",
        "avd_create",
        "avd_delete",
        "emulator_list",
        "json",
    }
)


class AndroidCliBackend(BaseSdkBackend):
    """The new ``android`` command-line backend."""

    name = "androidcli"

    def __init__(
        self,
        config: Config | None = None,
        sdk_paths: SdkPaths | None = None,
        *,
        host: Any = None,
        environ: Mapping[str, str] | None = None,
        runner: Runner | None = None,
        client: AndroidCliClient | None = None,
    ) -> None:
        super().__init__(config, sdk_paths, host=host, environ=environ, runner=runner)
        self._client = client

    # -- availability / capabilities -----------------------------------
    def client(self) -> AndroidCliClient:
        if self._client is None:
            self._client = AndroidCliClient(
                self.find_executable("android"),
                runner=self._runner,
                environ=self.process_env(),
            )
        return self._client

    def invalidate(self) -> None:
        """Drop cached availability/probe state (after an install or upgrade)."""
        self._client = None

    def available(self) -> bool:
        try:
            return self.client().available()
        except ToolError:
            return False

    @property
    def unavailable_reason(self) -> str:
        try:
            return self.client().unavailable_reason
        except ToolError as exc:
            return str(exc)

    def supports(self, feature: str) -> bool:
        return self.available() and feature in SUPPORTED_FEATURES

    def version(self) -> str:
        return self.client().version()

    def tool_versions(self) -> dict[str, str]:
        versions: dict[str, str] = {}
        try:
            versions["android"] = self.version()
        except (ToolError, OSError, ValueError):
            pass
        for tool, probe in (("adb", self._adb_version), ("emulator", self.emulator_version)):
            try:
                version = probe()
            except (ToolError, OSError, ValueError):
                continue
            if version:
                versions[tool] = version
        return versions

    def _adb_version(self) -> str:
        from ..legacy.adb import Adb

        return Adb(
            self.adb_executable(),
            runner=self._runner,
            environ=self.process_env(),
        ).version()

    # -- executables ----------------------------------------------------
    def executable(self, tool: str) -> Path:
        if tool in {"android", "cli"}:
            found = self.find_executable("android")
            if found is None:
                raise ToolNotFoundError(
                    "android",
                    hint="Install the new Android CLI or select the legacy backend.",
                )
            return found
        if tool in {"sdkmanager", "avdmanager"}:
            raise ToolNotFoundError(
                tool,
                hint=(
                    "The androidcli backend does not use sdkmanager/avdmanager; "
                    "select the legacy backend (prefer='legacy') for this tool."
                ),
            )
        return super().executable(tool)

    # -- packages -------------------------------------------------------
    def list_packages(self) -> list[SdkPackage]:
        installed, available = self.client().sdk_list_packages()
        merged: dict[str, SdkPackage] = {package.id: package for package in available}
        for package in installed:
            merged[package.id] = package.model_copy(update={"installed": True})
        return sorted(merged.values(), key=lambda package: package.id)

    def list_installed(self) -> list[SdkPackage]:
        installed, _ = self.client().sdk_list_packages(installed_only=True)
        merged = {package.id: package.model_copy(update={"installed": True}) for package in installed}
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
                    "argv": self._planned_argv("sdk", "install", *ids),
                },
            )
        result = self.client().sdk_install(
            ids,
            accept_licenses=accept_licenses,
            timeout_s=float(timeout_s or INSTALL_TIMEOUT_S),
        )
        failed = legacy_parsing.install_failed(result)
        data = result.to_dict()
        data.update(
            {
                "package_ids": ids,
                "accept_licenses": accept_licenses,
                "license_required": bool(legacy_parsing.license_required(result) and not accept_licenses),
            }
        )
        return OperationResult(
            ok=result.ok and not failed,
            message=redact(legacy_parsing.install_message(result)),
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
                data={"dry_run": True, "package_ids": ids, "argv": self._planned_argv("sdk", "uninstall", *ids)},
            )
        result = self.client().sdk_uninstall(ids)
        failed = legacy_parsing.install_failed(result)
        if result.ok and not failed:
            data = result.to_dict()
            data["package_ids"] = ids
            return OperationResult(
                ok=True,
                message=redact(legacy_parsing.install_message(result)),
                data=data,
            )
        fallback = self._uninstall_with_legacy(ids)
        if fallback is not None:
            return fallback
        data = result.to_dict()
        data["package_ids"] = ids
        return OperationResult(
            ok=False,
            message=(
                "the android CLI does not support uninstalling packages and no "
                "sdkmanager executable is installed"
            ),
            data=data,
        )

    def accept_licenses(self, *, timeout_s: int = 300) -> OperationResult:
        result = self.client().sdk_licenses(timeout_s=float(timeout_s or LICENSE_TIMEOUT_S))
        if legacy_parsing.licenses_accepted(result):
            return OperationResult(
                ok=True,
                message=redact(legacy_parsing.license_message(result)),
                data=result.to_dict(),
            )
        fallback = self._accept_licenses_with_legacy(timeout_s=float(timeout_s or LICENSE_TIMEOUT_S))
        if fallback is not None:
            return fallback
        return OperationResult(
            ok=False,
            message=redact(legacy_parsing.license_message(result)),
            data=result.to_dict(),
        )

    # -- images ---------------------------------------------------------
    def list_system_images(self) -> list[SystemImage]:
        return images_from_packages(self.list_packages(), self.sdk_paths.sdk_root)

    # -- AVDs -----------------------------------------------------------
    def avd_entries(self) -> list[AvdEntry]:
        return self.client().avd_entries()

    def list_avds(self) -> list[str]:
        try:
            entries = self.client().avd_entries()
        except (ToolError, ValueError) as exc:
            try:
                return self.emulator_list_avds()
            except (ToolError, ValueError):
                raise exc from None
        return sorted({entry.name for entry in entries if entry.name})

    def emulator_list_avds(self) -> list[str]:
        from ..legacy.emulator import Emulator

        names = Emulator(
            self.emulator_executable(),
            avd_home=self.avd_home,
            runner=self._runner,
            environ=self.process_env(),
        ).list_avds()
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
        result = self.client().avd_create(
            avd_name,
            package,
            device_id or None,
            force=force,
        )
        if not result.ok:
            if legacy_parsing.duplicate_avd(result):
                raise ConflictError(
                    f"AVD {avd_name!r} already exists",
                    hint="Pass force=True to recreate it, or delete it first.",
                )
            raise failed_command_error(result, f"creating AVD {avd_name!r}")
        reported = legacy_parsing.created_avd_path(result.combined)
        created = Path(reported) if reported else self.avd_dir(avd_name)
        if config:
            update_ini_file(created / "config.ini", config)
        return str(created)

    def delete_avd(self, name: str, *, force: bool = False) -> OperationResult:
        avd_name = _validate_avd_name(name)
        result = self.client().avd_delete(avd_name, force=force)
        if result.ok:
            return OperationResult(
                ok=True,
                message=f"AVD {avd_name!r} deleted",
                data=result.to_dict(),
            )
        if legacy_parsing.missing_avd(result):
            if force:
                return OperationResult(
                    ok=True,
                    message=f"AVD {avd_name!r} did not exist (nothing to delete)",
                    data=result.to_dict(),
                )
            raise NotFoundError(
                f"AVD {avd_name!r} does not exist",
                hint="List AVDs with `android avd list`.",
            )
        raise failed_command_error(result, f"deleting AVD {avd_name!r}")

    def emulator_list(self) -> list[dict[str, Any]]:
        """Running emulators as reported by ``android emulator list``."""
        return self.client().emulator_list()

    def list_device_profiles(self) -> list[DeviceProfile]:
        # The CLI has no stable device-profile command yet; prefer a legacy
        # avdmanager when installed, otherwise the built-in catalogue.
        return super().list_device_profiles()

    # -- internal helpers ----------------------------------------------
    def _planned_argv(self, *args: str) -> list[str]:
        executable = self.find_executable("android")
        argv = [str(executable) if executable is not None else "android", *args]
        return [str(redact(part)) for part in argv]

    def _uninstall_with_legacy(self, ids: Sequence[str]) -> OperationResult | None:
        executable = self.find_executable("sdkmanager")
        if executable is None:
            return None
        manager = SdkManager(
            executable,
            self.sdk_paths.sdk_root or None,
            runner=self._runner,
            environ=self.process_env(),
        )
        result = manager.uninstall(ids)
        failed = legacy_parsing.install_failed(result)
        data = result.to_dict()
        data["package_ids"] = list(ids)
        return OperationResult(
            ok=result.ok and not failed,
            message=redact(legacy_parsing.install_message(result)),
            data=data,
        )

    def _accept_licenses_with_legacy(self, *, timeout_s: float) -> OperationResult | None:
        """Fallback for builds whose ``android`` CLI has no ``sdk licenses``."""
        executable = self.find_executable("sdkmanager")
        if executable is None:
            return None
        manager = SdkManager(
            executable,
            self.sdk_paths.sdk_root or None,
            runner=self._runner,
            environ=self.process_env(),
        )
        result = manager.accept_licenses(timeout_s=timeout_s)
        ok = legacy_parsing.licenses_accepted(result)
        return OperationResult(
            ok=ok,
            message=redact(legacy_parsing.license_message(result)),
            data=result.to_dict(),
        )


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


__all__ = ["SUPPORTED_FEATURES", "AndroidCliBackend"]
