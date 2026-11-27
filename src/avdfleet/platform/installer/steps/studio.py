"""Step: optional Android Studio configuration.

Android Studio stores its per-user configuration under the provider's
``studio_config_dir``.  Rather than editing Google's own XML files (fragile and
version-specific) avdfleet writes a small managed properties file next to them
that records the SDK/AVD locations; the portable edition reuses the same file
via ``STUDIO_PROPERTIES``.  The step is optional: if Studio is not installed it
reports success with ``skipped=True`` instead of failing the install.
"""

from __future__ import annotations

from pathlib import Path

from avdfleet.core.models import OperationResult

from .base import BaseStep

STUDIO_PROPERTIES_FILENAME = "avdfleet.properties"


def properties_path(context) -> Path:
    return Path(context.studio_config_dir) / STUDIO_PROPERTIES_FILENAME


def properties_content(context) -> str:
    return "\n".join(
        [
            "# Managed by avdfleet; rewritten by 'avdfleet install --repair'.",
            f"android.sdk.path={context.sdk_root}",
            f"android.avd.home={context.avd_home}",
            f"android.jdk.home={context.facts.get('java_home') or context.get_env('JAVA_HOME') or ''}",
            "",
        ]
    )


def studio_installed(context) -> bool:
    if Path(context.studio_config_dir).is_dir():
        return True
    if context.provider.studio_jbr_home() is not None:
        return True
    return context.has("studio") or context.has("studio.exe") or context.has("studio64.exe")


class StudioConfigStep(BaseStep):
    id = "studio.config"
    title = "Configure Android Studio"
    description = (
        "Write an avdfleet-managed properties file into Android Studio's user config "
        "directory pointing at the SDK/AVD/JDK locations (optional)."
    )
    phase = "config"
    critical = False
    depends_on = ("sdk.env",)

    def check(self, context) -> bool:
        target = properties_path(context)
        if not target.is_file():
            return False
        try:
            return target.read_text(encoding="utf-8").strip() == properties_content(context).strip()
        except OSError:
            return False

    def run(self, context) -> OperationResult:
        target = properties_path(context)
        if not studio_installed(context) and not target.parent.is_dir():
            return self.skipped(
                "Android Studio not detected; configuration step skipped",
                path=str(target),
            )
        if context.dry_run:
            return self.done(
                "would write the Android Studio configuration file",
                path=str(target),
                content=properties_content(context),
            )
        context.write_text(target, properties_content(context))
        return self.done(
            "Android Studio configuration written",
            path=str(target),
        )

    def uninstall(self, context) -> OperationResult:
        target = properties_path(context)
        removed = context.remove_file(target)
        return self.done(
            "Android Studio configuration removed" if removed else "no Android Studio configuration to remove",
            path=str(target),
            removed=removed,
        )


__all__ = [
    "STUDIO_PROPERTIES_FILENAME",
    "StudioConfigStep",
    "properties_content",
    "properties_path",
    "studio_installed",
]
