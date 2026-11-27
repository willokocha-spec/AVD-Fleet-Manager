"""Step: persist the Android environment variables and PATH entries.

Sets ``ANDROID_SDK_ROOT``/``ANDROID_HOME``/``ANDROID_AVD_HOME``/``JAVA_HOME``
and prepends the SDK/JDK tool directories to ``PATH`` using the provider's
native persistence mechanism (user environment on Windows, managed shell-profile
block on macOS/Linux).  The same values are injected into the process
environment used for all later steps.
"""

from __future__ import annotations

import os
from pathlib import Path

from avdfleet.core.models import OperationResult

from .base import BaseStep

ENV_NAMES = ("ANDROID_SDK_ROOT", "ANDROID_HOME", "ANDROID_AVD_HOME", "JAVA_HOME")


def _normalise_path_entry(entry: str, platform: str) -> str:
    candidate = entry.strip().strip('"')
    if platform == "windows":
        return os.path.normcase(os.path.normpath(candidate))
    return candidate.rstrip("/") or "/"


class SdkEnvStep(BaseStep):
    id = "sdk.env"
    title = "Configure Android environment variables"
    description = (
        "Persist ANDROID_SDK_ROOT, ANDROID_HOME, ANDROID_AVD_HOME, JAVA_HOME and the "
        "SDK/JDK PATH entries for current and future shells."
    )
    phase = "sdk"
    critical = True
    depends_on = ("sdk.packages",)

    # -- helpers ---------------------------------------------------------
    def expected_env(self, context) -> dict[str, str]:
        mapping = {
            "ANDROID_SDK_ROOT": str(context.sdk_root),
            "ANDROID_HOME": str(context.sdk_root),
            "ANDROID_AVD_HOME": str(context.avd_home),
        }
        java_home = context.facts.get("java_home") or context.get_env("JAVA_HOME")
        if java_home:
            mapping["JAVA_HOME"] = str(java_home)
        return mapping

    def path_entries(self, context) -> list[str]:
        entries = [
            context.sdk_root / "cmdline-tools" / "latest" / "bin",
            context.sdk_root / "platform-tools",
            context.sdk_root / "emulator",
        ]
        java_home = context.facts.get("java_home") or context.get_env("JAVA_HOME")
        if java_home:
            entries.append(Path(str(java_home)) / "bin")
        return [str(entry) for entry in entries]

    @staticmethod
    def _path_contains(current: str, entries: list[str], platform: str) -> bool:
        parts = {
            _normalise_path_entry(part, platform)
            for part in current.split(os.pathsep)
            if part.strip()
        }
        return all(_normalise_path_entry(entry, platform) in parts for entry in entries)

    # -- contract --------------------------------------------------------
    def check(self, context) -> bool:
        platform = context.provider.platform.value
        expected = self.expected_env(context)
        for name, value in expected.items():
            current = context.get_env(name)
            if current is None:
                return False
            if _normalise_path_entry(current, platform) != _normalise_path_entry(value, platform):
                return False
        entries = self.path_entries(context)
        current_path = context.process_env().get("PATH") or context.get_env("PATH") or ""
        if not self._path_contains(current_path, entries, platform):
            return False
        return context.simulated or all(Path(entry).is_dir() for entry in entries)

    def run(self, context) -> OperationResult:
        expected = self.expected_env(context)
        entries = self.path_entries(context)
        commands = []
        applied: dict[str, str] = {}
        for name, value in expected.items():
            commands.extend(context.set_env(name, value))
            applied[name] = value
        commands.extend(context.add_path(entries))
        ok, message, results = self.run_commands(
            context,
            commands,
            what="configuring the Android environment",
        )
        if not ok:
            return self.failed(
                message,
                hint="check permissions for your shell profile / user environment",
                commands=self.command_lines(results),
            )
        context.facts["env"] = applied
        context.facts["path_entries"] = entries
        context.info(f"configured Android environment for SDK root {context.sdk_root}")
        return self.done(
            "Android environment variables configured",
            env=applied,
            path_entries=entries,
            commands=self.command_lines(results),
        )

    def uninstall(self, context) -> OperationResult:
        commands = []
        for name in ENV_NAMES:
            commands.extend(context.provider.remove_env_var(name))
        entries = context.facts.get("path_entries") or self.path_entries(context)
        commands.extend(context.provider.remove_from_path([str(entry) for entry in entries]))
        ok, message, results = self.run_commands(context, commands, what="removing Android environment variables")
        if not ok:
            return self.failed(message, commands=self.command_lines(results))
        return self.done(
            "Android environment variables removed",
            commands=self.command_lines(results),
        )


__all__ = ["ENV_NAMES", "SdkEnvStep"]
