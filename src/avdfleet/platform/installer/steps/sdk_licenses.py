"""Step: accept the Android SDK licenses.

Licenses are **never** auto-accepted.  The step only sends ``y`` to
``sdkmanager --licenses`` when the caller passed ``--accept-android-sdk-licenses``
(``InstallOptions.accept_android_sdk_licenses``).  Without the flag the step
fails with an actionable hint so an unattended bootstrap cannot silently
consent to Google's terms.
"""

from __future__ import annotations

from pathlib import Path

from avdfleet.core.models import OperationResult

from ..providers.base import Command
from .base import BaseStep

#: License files sdkmanager creates under ``<sdk>/licenses``.
LICENSE_FILES = ("android-sdk-license", "android-sdk-preview-license")
LICENSE_ACCEPT_STDIN = "y\n" * 20


def licenses_dir(sdk_root: str | Path) -> Path:
    return Path(sdk_root) / "licenses"


def licenses_accepted(sdk_root: str | Path) -> bool:
    folder = licenses_dir(sdk_root)
    return (folder / LICENSE_FILES[0]).is_file()


class SdkLicensesStep(BaseStep):
    id = "sdk.licenses"
    title = "Accept Android SDK licenses"
    description = (
        "Run 'sdkmanager --licenses' and answer 'y' - only when "
        "--accept-android-sdk-licenses was passed. Never auto-accepts."
    )
    phase = "sdk"
    critical = True
    depends_on = ("sdk.cmdline_tools",)

    def check(self, context) -> bool:
        if context.facts.get("licenses_accepted"):
            return True
        return licenses_accepted(context.sdk_root)

    def run(self, context) -> OperationResult:
        if licenses_accepted(context.sdk_root):
            context.facts["licenses_accepted"] = True
            return self.done("Android SDK licenses already accepted")

        if not context.options.accept_android_sdk_licenses:
            return self.failed(
                "Android SDK licenses are not accepted",
                hint="re-run with --accept-android-sdk-licenses (only after reading Google's SDK terms)",
                licenses_dir=str(licenses_dir(context.sdk_root)),
            )

        sdkmanager = context.sdkmanager_path()
        if context.sdk_backend is not None:
            if context.dry_run:
                return self.done("would accept the Android SDK licenses through the SDK backend")
            outcome = context.sdk_backend.accept_licenses(timeout_s=600)
            if not outcome.ok:
                return self.failed(
                    f"SDK backend could not accept licenses: {outcome.message}",
                    hint="run 'sdkmanager --licenses' manually and retry",
                )
        else:
            if sdkmanager is None and not context.simulated and not context.dry_run:
                return self.failed(
                    "sdkmanager was not found; cannot accept the Android SDK licenses",
                    hint="run the sdk.cmdline_tools step first ('avdfleet install --only sdk.cmdline_tools')",
                )
            command = Command(
                [sdkmanager or "sdkmanager", f"--sdk_root={context.sdk_root}", "--licenses"],
                description="accept Android SDK licenses (stdin: y)",
                stdin=LICENSE_ACCEPT_STDIN,
                timeout_s=600,
            )
            ok, message, results = self.run_commands(context, [command], what="accepting SDK licenses")
            if not ok:
                return self.failed(
                    message,
                    hint="check network access to dl.google.com and re-run with --accept-android-sdk-licenses",
                    commands=self.command_lines(results),
                )

        if not context.dry_run and not context.simulated and not licenses_accepted(context.sdk_root):
            return self.failed(
                "sdkmanager reported success but no license file was written",
                hint=f"expected {licenses_dir(context.sdk_root)}/{LICENSE_FILES[0]}",
            )
        context.facts["licenses_accepted"] = True
        return self.done("Android SDK licenses accepted")

    def uninstall(self, context) -> OperationResult:
        return self.done("Android SDK licenses are kept (they are not user data)")


__all__ = ["LICENSE_ACCEPT_STDIN", "LICENSE_FILES", "SdkLicensesStep", "licenses_accepted", "licenses_dir"]
