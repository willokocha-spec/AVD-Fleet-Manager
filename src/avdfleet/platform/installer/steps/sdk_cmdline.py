"""Step: install the Android ``cmdline-tools`` archive.

The archive is downloaded from Google's repository (URL/build overridable via
environment so research/CI can pin it) and laid out as
``<sdk>/cmdline-tools/latest/bin/sdkmanager``.  The step never downloads when
the runner is in dry-run mode and never touches the network in tests because a
recorded/fake runner is injected.
"""

from __future__ import annotations

import os
from pathlib import Path

from avdfleet.core.models import OperationResult

from .base import BaseStep

#: Pinned build used when no override is provided.  R1 will confirm/refresh the
#: version and SHA-256; operators and CI can override all three with env vars.
DEFAULT_CMDLINE_TOOLS_BUILD = "11076708"
CMDLINE_TOOLS_BUILD_ENV = "AVDFLEET_CMDLINE_TOOLS_BUILD"
CMDLINE_TOOLS_URL_ENV = "AVDFLEET_CMDLINE_TOOLS_URL"
CMDLINE_TOOLS_SHA256_ENV = "AVDFLEET_CMDLINE_TOOLS_SHA256"

_ARCHIVE_PLATFORM = {
    "windows": "win",
    "macos": "mac",
    "linux": "linux",
}


def cmdline_tools_filename(platform: str, build: str) -> str:
    suffix = _ARCHIVE_PLATFORM.get(platform, "linux")
    return f"commandlinetools-{suffix}-{build}_latest.zip"


def cmdline_tools_url(
    platform: str,
    *,
    build: str | None = None,
    url: str | None = None,
    sha256: str | None = None,
) -> tuple[str, str | None]:
    """Return ``(download_url, sha256_or_none)`` for the given host platform."""
    resolved_build = build or DEFAULT_CMDLINE_TOOLS_BUILD
    filename = cmdline_tools_filename(platform, resolved_build)
    default_url = f"https://dl.google.com/android/repository/{filename}"
    return (url or default_url, sha256 or None)


class SdkCmdlineToolsStep(BaseStep):
    id = "sdk.cmdline_tools"
    title = "Install Android command-line tools"
    description = (
        "Download the pinned Android command-line tools archive and unpack it to "
        "<sdk>/cmdline-tools/latest so sdkmanager/avdmanager are available."
    )
    phase = "sdk"
    critical = True
    depends_on = ("tools.jdk",)

    # -- helpers ---------------------------------------------------------
    def expected_sdkmanager(self, context) -> Path:
        suffix = "sdkmanager.bat" if context.provider.platform.value == "windows" else "sdkmanager"
        return context.sdk_root / "cmdline-tools" / "latest" / "bin" / suffix

    def _resolve_source(self, context) -> tuple[str, str | None]:
        provider = context.provider
        build = provider.env_value(CMDLINE_TOOLS_BUILD_ENV) or os.environ.get(CMDLINE_TOOLS_BUILD_ENV)
        url = provider.env_value(CMDLINE_TOOLS_URL_ENV) or os.environ.get(CMDLINE_TOOLS_URL_ENV)
        sha = provider.env_value(CMDLINE_TOOLS_SHA256_ENV) or os.environ.get(CMDLINE_TOOLS_SHA256_ENV)
        return cmdline_tools_url(provider.platform.value, build=build, url=url, sha256=sha or None)

    # -- contract --------------------------------------------------------
    def check(self, context) -> bool:
        if self.expected_sdkmanager(context).is_file():
            return True
        backend = context.sdk_backend
        if backend is not None:
            try:
                executable = Path(backend.executable("sdkmanager"))
            except Exception:
                return False
            return executable.is_file()
        return False

    def run(self, context) -> OperationResult:
        sdkmanager = self.expected_sdkmanager(context)
        if sdkmanager.is_file():
            context.facts["sdkmanager"] = str(sdkmanager)
            return self.done("Android command-line tools already installed", sdkmanager=str(sdkmanager))

        if context.sdk_backend is not None:
            try:
                backend_executable = Path(context.sdk_backend.executable("sdkmanager"))
            except Exception:
                backend_executable = None
            if backend_executable is not None and backend_executable.is_file():
                context.facts["sdkmanager"] = str(backend_executable)
                return self.done(
                    "sdkmanager is provided by the injected SDK backend",
                    sdkmanager=str(backend_executable),
                )

        url, sha256 = self._resolve_source(context)
        archive = context.paths.downloads_dir / Path(url).name
        staging = context.sdk_root / ".cmdline-tools-staging"
        latest = context.sdk_root / "cmdline-tools" / "latest"
        commands = list(context.provider.download_file(url, str(archive), sha256=sha256))
        commands.append(context.provider.extract_zip(str(archive), str(staging)))
        commands.append(context.provider.move_path(str(staging / "cmdline-tools"), str(latest)))
        if not context.dry_run:
            context.mkdir(staging)
        ok, message, results = self.run_commands(
            context,
            commands,
            what="installing the Android command-line tools",
        )
        if not ok:
            return self.failed(message, commands=self.command_lines(results), url=url)

        if not context.dry_run and not context.simulated and not sdkmanager.is_file():
            return self.failed(
                "command-line tools were downloaded but sdkmanager was not produced",
                hint=f"inspect {staging}; delete <sdk>/cmdline-tools and re-run 'avdfleet install --repair'",
                url=url,
            )
        context.facts["sdkmanager"] = str(sdkmanager)
        return self.done(
            "Android command-line tools installed",
            sdkmanager=str(sdkmanager),
            sha256=sha256 or "",
            url=url,
            commands=self.command_lines(results),
        )

    def uninstall(self, context) -> OperationResult:
        return self.done("command-line tools are removed with the managed SDK directory")


__all__ = [
    "CMDLINE_TOOLS_BUILD_ENV",
    "CMDLINE_TOOLS_SHA256_ENV",
    "CMDLINE_TOOLS_URL_ENV",
    "DEFAULT_CMDLINE_TOOLS_BUILD",
    "SdkCmdlineToolsStep",
    "cmdline_tools_filename",
    "cmdline_tools_url",
]
