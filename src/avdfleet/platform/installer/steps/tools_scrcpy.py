"""Step: install scrcpy (optional local screen mirroring fallback).

Success is only reported when ``scrcpy`` is genuinely resolvable after the
install command.  A package manager exiting 0 is not proof that the tool is
usable: winget can report success while the portable shim is not visible to
the already-running process (its ``PATH`` predates the install).  Because the
step is optional, an unresolvable scrcpy ends in a *failed* step result --
the engine records it and continues the rest of the install.
"""

from __future__ import annotations

from avdfleet.core.models import OperationResult

from .base import BaseStep


class ScrcpyStep(BaseStep):
    id = "tools.scrcpy"
    title = "Install scrcpy"
    description = "Install scrcpy as the local-display fallback for the screen service (optional)."
    phase = "tools"
    critical = False
    depends_on = ()

    def check(self, context) -> bool:
        return context.has("scrcpy")

    def run(self, context) -> OperationResult:
        commands = context.tool_commands("scrcpy")
        if not commands:
            return self.skipped(f"scrcpy is not available for {context.provider.id}")
        failure = self.bootstrap_package_manager(context)
        if failure is not None:
            return failure
        if any(command.admin for command in commands):
            failure = self.check_admin(context, "installing scrcpy")
            if failure is not None:
                return failure
        ok, message, results = self.run_commands(context, commands, what="installing scrcpy")
        command_lines = self.command_lines(results)
        if not ok:
            return self.failed(
                message,
                hint=self._hint(context),
                commands=command_lines,
            )

        if context.dry_run or context.simulated:
            # Nothing was really installed: report the plan, never a version.
            return self.done(
                f"would install scrcpy with {context.provider.package_manager}",
                scrcpy=context.which("scrcpy") or "scrcpy",
                version="",
                planned=True,
                commands=command_lines,
            )

        # Re-resolve instead of trusting the exit code: the running process may
        # still have the PATH from before the package manager updated it.
        scrcpy = context.which("scrcpy")
        if scrcpy is None:
            context.provider.refresh_path()
            scrcpy = context.which("scrcpy")
        if scrcpy is None:
            return self.failed(
                "the scrcpy install command succeeded but no 'scrcpy' executable is on PATH",
                hint=self._hint(context),
                commands=command_lines,
            )

        version = ""
        probe = context.run([scrcpy, "--version"], timeout_s=60)
        if probe.ok:
            version = self.first_line(probe.stdout or probe.stderr)
        else:
            context.warning(
                f"scrcpy was found at {scrcpy} but 'scrcpy --version' failed "
                f"(exit {probe.exit_code}); the screen fallback may not start"
            )
        return self.done(
            f"scrcpy installed{f' ({version})' if version else ''}",
            scrcpy=scrcpy,
            version=version,
            commands=command_lines,
        )

    def _hint(self, context) -> str:
        manager = context.provider.package_manager
        return (
            f"scrcpy is optional; install it manually with {manager} and then run "
            "'avdfleet install --repair --only tools.scrcpy'. If it is already "
            "installed, open a new terminal first so PATH includes its install "
            "directory."
        )

    def uninstall(self, context) -> OperationResult:
        return self.done("scrcpy is a system package and is left installed")


__all__ = ["ScrcpyStep"]
