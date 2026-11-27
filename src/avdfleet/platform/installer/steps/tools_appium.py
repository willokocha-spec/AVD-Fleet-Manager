"""Step: Appium 4, the UiAutomator2 driver and the Appium Inspector.

The server/driver install is critical; the Inspector is optional and a failure
only produces a warning.  Driver commands run through the resolved ``appium``
binary when we can find it (npm global installs often land outside PATH until a
new shell).
"""

from __future__ import annotations

from avdfleet.core.models import OperationResult

from ..providers.base import Command
from .base import BaseStep

APPIUM_MAJOR = 4


def _with_binary(commands: list[Command], binary: str) -> list[Command]:
    """Rewrite leading ``appium`` argv entries to the resolved binary path."""
    resolved: list[Command] = []
    for command in commands:
        if command.argv and command.argv[0] == "appium":
            resolved.append(
                Command(
                    [binary, *command.argv[1:]],
                    description=command.description,
                    admin=command.admin,
                    timeout_s=command.timeout_s,
                    stdin=command.stdin,
                    env=dict(command.env),
                )
            )
        else:
            resolved.append(command)
    return resolved


class AppiumStep(BaseStep):
    id = "tools.appium"
    title = "Install Appium 4, UiAutomator2 driver and Inspector"
    description = (
        "npm install appium@4 globally, install the uiautomator2 driver and "
        "(best effort) the Appium Inspector."
    )
    phase = "tools"
    critical = True
    depends_on = ("tools.node",)

    def check(self, context) -> bool:
        return context.has("appium")

    def run(self, context) -> OperationResult:
        failure = self.bootstrap_package_manager(context)
        if failure is not None:
            return failure

        appium_commands = context.tool_commands("appium")
        if not appium_commands:
            return self.failed(
                f"no Appium install command is defined for {context.provider.id}",
                hint="run 'npm install -g appium@4' manually and re-run 'avdfleet install --repair'",
            )
        if any(command.admin for command in appium_commands):
            failure = self.check_admin(context, "installing Appium")
            if failure is not None:
                return failure
        ok, message, results = self.run_commands(context, appium_commands, what="installing Appium 4")
        if not ok:
            return self.failed(
                message,
                hint="check npm/network access; 'npm install -g appium@4' can be run manually",
                commands=self.command_lines(results),
            )

        appium_bin = context.which("appium") or "appium"
        driver_commands = context.tool_commands(
            "appium_driver",
            fallback=[
                Command(
                    ["appium", "driver", "install", "uiautomator2"],
                    description="install the Appium UiAutomator2 driver",
                    timeout_s=900,
                )
            ],
        )
        driver_commands = _with_binary(driver_commands, appium_bin)
        ok, message, driver_results = self.run_commands(
            context,
            driver_commands,
            what="installing the Appium UiAutomator2 driver",
        )
        if not ok:
            return self.failed(
                message,
                hint="run 'appium driver install uiautomator2' manually, then 'avdfleet install --repair'",
                commands=self.command_lines(results + driver_results),
            )

        inspector_commands = context.tool_commands("inspector")
        inspector_results = context.run_commands(inspector_commands) if inspector_commands else []
        inspector_ok = all(result.ok for result in inspector_results)
        if inspector_commands and not inspector_ok:
            context.warning("Appium Inspector install failed (optional); continuing")

        version = ""
        if not context.dry_run and not context.simulated and context.which("appium"):
            probe = context.run([appium_bin, "--version"], timeout_s=60)
            version = self.first_line(probe.stdout or probe.stderr)
            major = self.version_major(version)
            if major is not None and major != APPIUM_MAJOR:
                context.warning(f"Appium version {version} is not {APPIUM_MAJOR}.x")
        return self.done(
            f"Appium {APPIUM_MAJOR} installed with the UiAutomator2 driver",
            appium=appium_bin,
            appium_version=version,
            inspector_installed=inspector_ok if inspector_commands else False,
            commands=self.command_lines(results + driver_results + inspector_results),
        )

    def uninstall(self, context) -> OperationResult:
        return self.done(
            "Appium is left installed; remove it with 'npm uninstall -g appium' if desired"
        )


__all__ = ["APPIUM_MAJOR", "AppiumStep"]
