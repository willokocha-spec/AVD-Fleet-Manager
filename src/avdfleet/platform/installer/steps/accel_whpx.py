"""Step: enable Windows Hypervisor Platform (WHPX) and pause for the reboot.

WHPX is a Windows optional feature.  Enabling it requires admin rights and one
reboot; the engine converts :class:`RebootRequiredError` into a persisted
``reboot_required`` checkpoint (exit code 3010 at the CLI) and never reboots
the host itself.
"""

from __future__ import annotations

from avdfleet.core.models import AcceleratorKind, HostOS, OperationResult

from .base import BaseStep


class WhpxStep(BaseStep):
    id = "accel.whpx"
    title = "Enable Windows Hypervisor Platform (WHPX)"
    description = (
        "Enable the HypervisorPlatform Windows feature and configure the hypervisor to launch "
        "at boot. Requires administrator rights and one reboot; the install resumes afterwards."
    )
    phase = "acceleration"
    critical = True
    requires_reboot = True
    depends_on = ("accel.detect", "accel.aehd")

    def check(self, context) -> bool:
        if context.provider.platform is not HostOS.WINDOWS:
            return True
        plan = context.provider.virtualization_plan()
        if plan.kind is not AcceleratorKind.WHPX:
            return True
        return bool(plan.enabled)

    def run(self, context) -> OperationResult:
        provider = context.provider
        if provider.platform is not HostOS.WINDOWS:
            return self.skipped("WHPX is not applicable on this host")

        plan = provider.virtualization_plan()
        if plan.kind is not AcceleratorKind.WHPX:
            return self.skipped(f"host accelerator is {plan.kind.value}; WHPX not required")
        if plan.enabled:
            context.facts["whpx_enabled"] = True
            return self.done("Windows Hypervisor Platform is already enabled")
        if not plan.available:
            return self.failed(
                "Windows Hypervisor Platform is unavailable on this host",
                hint=(
                    "enable VT-x/AMD-V in firmware (or nested virtualisation in the outer VM); "
                    "WHPX cannot work without hardware virtualisation"
                ),
                notes=list(plan.notes),
            )

        failure = self.check_admin(context, "enabling the Windows Hypervisor Platform")
        if failure is not None:
            return failure
        commands = list(plan.commands)
        if not commands:
            return self.failed(
                "no WHPX enable commands are defined for this host",
                hint="enable the 'Windows Hypervisor Platform' feature manually, then repair",
            )

        if context.dry_run:
            return self.done(
                "would enable Windows Hypervisor Platform; a reboot is required afterwards",
                commands=[list(command.argv) for command in commands],
                reboot_required=True,
                notes=list(plan.notes),
            )

        ok, message, results = self.run_commands(context, commands, what="enabling Windows Hypervisor Platform")
        if not ok:
            return self.failed(
                message,
                hint=(
                    "run 'Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -All' "
                    "in an elevated PowerShell, then repair"
                ),
                commands=self.command_lines(results),
            )
        context.facts["whpx_enable_commands_done"] = True
        raise self.reboot_required(
            "Windows Hypervisor Platform was enabled; reboot the host and run "
            "'avdfleet install --resume' to continue",
            step_id=self.id,
        )

    def uninstall(self, context) -> OperationResult:
        return self.done(
            "WHPX is a Windows feature and is not disabled by avdfleet uninstall"
        )


__all__ = ["WhpxStep"]
