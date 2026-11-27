"""Step: install scrcpy (optional local screen mirroring fallback)."""

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
        if not ok:
            return self.failed(
                message,
                hint="scrcpy is optional; install it manually or re-run 'avdfleet install --repair --only tools.scrcpy'",
                commands=self.command_lines(results),
            )
        version = ""
        scrcpy = context.which("scrcpy")
        if scrcpy and not context.dry_run and not context.simulated:
            probe = context.run([scrcpy, "--version"], timeout_s=60)
            version = self.first_line(probe.stdout or probe.stderr)
        return self.done(
            f"scrcpy installed{f' ({version})' if version else ''}",
            scrcpy=scrcpy or "scrcpy",
            version=version,
            commands=self.command_lines(results),
        )

    def uninstall(self, context) -> OperationResult:
        return self.done("scrcpy is a system package and is left installed")


__all__ = ["ScrcpyStep"]
