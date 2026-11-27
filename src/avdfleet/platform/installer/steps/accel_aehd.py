"""Step: migrate from AEHD to WHPX (Windows only).

The legacy Android Emulator Hypervisor Driver (AEHD) cannot coexist with WHPX.
When AEHD is detected, its service and driver package are removed first; the
subsequent ``accel.whpx`` step enables WHPX and requests the single reboot.
This step is non-critical: if removal fails, WHPX enablement will surface the
conflict with a clear error.
"""

from __future__ import annotations

from avdfleet.core.models import AcceleratorKind, HostOS, OperationResult

from .base import BaseStep


class AehdStep(BaseStep):
    id = "accel.aehd"
    title = "Migrate AEHD to WHPX"
    description = (
        "Detect the Android Emulator Hypervisor Driver and remove it before WHPX is "
        "enabled, avoiding a hypervisor conflict (Windows only)."
    )
    phase = "acceleration"
    critical = False
    requires_reboot = False
    depends_on = ("accel.detect",)

    def check(self, context) -> bool:
        if context.provider.platform is not HostOS.WINDOWS:
            return True
        if context.facts.get("aehd_migrated"):
            return True
        if not context.provider.aehd_installed():
            return True
        plan = context.provider.virtualization_plan()
        # WHPX already enabled means migration is not required; AEHD is dormant.
        return plan.kind is AcceleratorKind.WHPX and plan.enabled

    def run(self, context) -> OperationResult:
        provider = context.provider
        if provider.platform is not HostOS.WINDOWS:
            return self.skipped("AEHD migration is only applicable on Windows")
        if not provider.aehd_installed():
            context.facts["aehd_installed"] = False
            return self.done("AEHD is not installed; no migration needed")

        plan = provider.aehd_migration_plan()
        nested = provider.nested_virtualization()
        if nested:
            context.warning(
                "nested virtualisation detected: WHPX may be unavailable inside this VM even after AEHD is removed"
            )
        failure = self.check_admin(context, "removing the Android Emulator Hypervisor Driver")
        if failure is not None:
            return failure

        commands = list(plan.commands)
        if not commands:
            return self.failed(
                "AEHD is installed but no removal commands are defined",
                hint="remove AEHD from 'Add or remove programs' (Android Emulator Hypervisor Driver), then repair",
            )
        if context.dry_run:
            return self.done(
                "would remove AEHD before enabling WHPX",
                commands=[list(command.argv) for command in commands],
                notes=list(plan.notes),
            )
        ok, message, results = self.run_commands(context, commands, what="removing AEHD")
        if not ok:
            return self.failed(
                message,
                hint=(
                    "stop/delete the AEHD service and uninstall its driver, then re-run "
                    "'avdfleet install --repair --only accel.aehd'"
                ),
                commands=self.command_lines(results),
            )
        context.facts["aehd_migrated"] = True
        return self.done(
            "AEHD removed; WHPX is enabled by the next acceleration step (single reboot)",
            commands=self.command_lines(results),
            notes=list(plan.notes),
        )

    def uninstall(self, context) -> OperationResult:
        return self.done("AEHD is not reinstalled by avdfleet uninstall")


__all__ = ["AehdStep"]
