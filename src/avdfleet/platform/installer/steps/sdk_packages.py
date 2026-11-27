"""Step: install the required Android SDK packages via ``sdkmanager``.

When an :class:`avdfleet.core.interfaces.SdkBackend` is injected (the
container/CLI wires whichever backend is configured) the step delegates to it;
otherwise it runs ``sdkmanager --install`` itself.  Required package ids can be
overridden with ``AVDFLEET_SDK_PACKAGES`` (comma separated).
"""

from __future__ import annotations

import os
from pathlib import Path

from avdfleet.core.models import OperationResult

from ..providers.base import Command
from .base import BaseStep

#: Minimum set needed to create/boot AVDs and run adb; system images are
#: downloaded by the images subsystem when an AVD template needs one.
DEFAULT_SDK_PACKAGES: tuple[str, ...] = (
    "platform-tools",
    "emulator",
    "build-tools;35.0.0",
    "platforms;android-35",
)
SDK_PACKAGES_ENV = "AVDFLEET_SDK_PACKAGES"


def required_packages(environ: dict[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    override = (env.get(SDK_PACKAGES_ENV) or "").strip()
    if not override:
        return list(DEFAULT_SDK_PACKAGES)
    return [item.strip() for item in override.replace(";", ",").split(",") if item.strip()]


def package_path(sdk_root: str | Path, package_id: str) -> Path:
    """Map an SDK package id to its on-disk directory under *sdk_root*."""
    root = Path(sdk_root)
    parts = [part for part in package_id.split(";") if part]
    if not parts:
        return root
    if parts[0] == "system-images" and len(parts) >= 4:
        return root.joinpath("system-images", parts[1], parts[2], parts[3])
    if len(parts) == 1:
        return root / parts[0]
    return root.joinpath(parts[0], parts[1])


class SdkPackagesStep(BaseStep):
    id = "sdk.packages"
    title = "Install required Android SDK packages"
    description = (
        "Install platform-tools, emulator, build-tools and the platform package "
        "through sdkmanager (or the injected SdkBackend)."
    )
    phase = "sdk"
    critical = True
    depends_on = ("sdk.licenses",)

    # -- helpers ---------------------------------------------------------
    def packages(self, context) -> list[str]:
        provider = context.provider
        override = provider.env_value(SDK_PACKAGES_ENV) or os.environ.get(SDK_PACKAGES_ENV, "")
        if override.strip():
            return required_packages({SDK_PACKAGES_ENV: override})
        return list(DEFAULT_SDK_PACKAGES)

    def missing_packages(self, context) -> list[str]:
        missing: list[str] = []
        for package_id in self.packages(context):
            if not package_path(context.sdk_root, package_id).is_dir():
                missing.append(package_id)
        return missing

    def _backend_installed(self, context) -> set[str] | None:
        backend = context.sdk_backend
        if backend is None:
            return None
        try:
            return {package.id for package in backend.list_installed()}
        except Exception:
            return None

    # -- contract --------------------------------------------------------
    def check(self, context) -> bool:
        if context.facts.get("sdk_packages"):
            return True
        packages = self.packages(context)
        installed = self._backend_installed(context)
        if installed is not None:
            return set(packages).issubset(installed)
        return not self.missing_packages(context)

    def run(self, context) -> OperationResult:
        packages = self.packages(context)
        missing = self.missing_packages(context)
        if not missing and context.sdk_backend is None:
            context.facts["sdk_packages"] = packages
            return self.done("required SDK packages already installed", packages=packages)

        if not context.options.accept_android_sdk_licenses and not context.dry_run:
            from .sdk_licenses import licenses_accepted

            if not licenses_accepted(context.sdk_root):
                return self.failed(
                    "refusing to install SDK packages while the SDK licenses are not accepted",
                    hint="re-run with --accept-android-sdk-licenses",
                )

        if context.sdk_backend is not None:
            if context.dry_run:
                return self.done(
                    "would install SDK packages through the SDK backend",
                    packages=packages,
                    backend=context.sdk_backend.name,
                )
            outcome = context.sdk_backend.install_packages(
                packages,
                accept_licenses=context.options.accept_android_sdk_licenses,
                dry_run=False,
                timeout_s=1800,
            )
            if not outcome.ok:
                return self.failed(
                    f"SDK backend could not install packages: {outcome.message}",
                    packages=packages,
                    backend=context.sdk_backend.name,
                )
            context.facts["sdk_packages"] = packages
            return self.done(
                f"installed {len(packages)} SDK package(s) through {context.sdk_backend.name}",
                packages=packages,
                backend=context.sdk_backend.name,
            )

        sdkmanager = context.sdkmanager_path()
        if sdkmanager is None and not context.simulated and not context.dry_run:
            return self.failed(
                "sdkmanager was not found; cannot install SDK packages",
                hint="run the sdk.cmdline_tools step first ('avdfleet install --only sdk.cmdline_tools')",
                packages=packages,
            )
        stdin = "y\n" * (len(packages) * 2) if context.options.accept_android_sdk_licenses else None
        command = Command(
            [sdkmanager or "sdkmanager", f"--sdk_root={context.sdk_root}", "--install", *packages],
            description=f"install SDK packages: {', '.join(packages)}",
            stdin=stdin,
            timeout_s=1800,
        )
        ok, message, results = self.run_commands(
            context,
            [command],
            what="installing Android SDK packages",
        )
        if not ok:
            return self.failed(
                message,
                hint="check network access to dl.google.com; re-run 'avdfleet install --repair'",
                packages=packages,
                commands=self.command_lines(results),
            )

        still_missing = self.missing_packages(context) if not context.simulated and not context.dry_run else []
        if still_missing:
            return self.failed(
                f"sdkmanager did not install: {', '.join(still_missing)}",
                hint="run 'sdkmanager --list_installed' to inspect the SDK",
                packages=still_missing,
                commands=self.command_lines(results),
            )
        context.facts["sdk_packages"] = packages
        return self.done(
            f"installed {len(packages)} SDK package(s)",
            packages=packages,
            commands=self.command_lines(results),
        )

    def uninstall(self, context) -> OperationResult:
        return self.done("SDK packages are removed with the managed SDK directory")


__all__ = [
    "DEFAULT_SDK_PACKAGES",
    "SDK_PACKAGES_ENV",
    "SdkPackagesStep",
    "package_path",
    "required_packages",
]
