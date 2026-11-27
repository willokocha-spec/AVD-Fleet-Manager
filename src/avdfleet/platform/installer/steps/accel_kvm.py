"""Step: configure Linux KVM acceleration (udev rule + kvm group).

Installs ``/etc/udev/rules.d/70-avdfleet-kvm.rules``, reloads udev and adds the
operator to the ``kvm`` group.  A re-login (not a reboot) is needed for group
membership; the step reports that clearly.  Nested virtualisation is reported
but cannot be fixed by the installer.
"""

from __future__ import annotations

from pathlib import Path

from avdfleet.core.models import AcceleratorKind, HostOS, OperationResult

from .base import BaseStep


class KvmStep(BaseStep):
    id = "accel.kvm"
    title = "Configure Linux KVM acceleration"
    description = (
        "Install the 70-avdfleet-kvm udev rule, load the KVM module and add the user to the "
        "kvm group so the emulator can use /dev/kvm."
    )
    phase = "acceleration"
    critical = True
    requires_reboot = False
    depends_on = ("accel.detect",)

    def check(self, context) -> bool:
        if context.provider.platform is not HostOS.LINUX:
            return True
        plan = context.provider.virtualization_plan()
        if plan.kind is not AcceleratorKind.KVM:
            return True
        return bool(plan.enabled)

    def run(self, context) -> OperationResult:
        provider = context.provider
        if provider.platform is not HostOS.LINUX:
            return self.skipped("KVM is only applicable on Linux")
        plan = provider.virtualization_plan()
        if plan.kind is not AcceleratorKind.KVM:
            return self.skipped(f"host accelerator is {plan.kind.value}; KVM not required")
        if plan.enabled:
            return self.done("KVM acceleration is already configured")

        nested = plan.nested if plan.nested is not None else provider.nested_virtualization()
        notes = list(plan.notes)
        if nested:
            message = (
                "nested virtualisation detected: /dev/kvm must be exposed by the outer hypervisor; "
                "the emulator cannot accelerate without it"
            )
            context.warning(message)
            notes.append(message)
        if not plan.available:
            return self.failed(
                "KVM acceleration is not available: /dev/kvm is missing",
                hint=(
                    "enable VT-x/AMD-V in firmware (or nested virtualisation in the outer VM); "
                    "then re-run 'avdfleet install --repair --only accel.kvm'"
                ),
                notes=notes,
                udev_rule_path=plan.udev_rule_path,
                udev_rule_content=plan.udev_rule_content,
            )

        failure = self.check_admin(context, "configuring KVM (udev rule and kvm group)")
        if failure is not None:
            return failure
        commands = list(plan.commands)
        if not commands:
            return self.failed(
                "KVM is not configured but no commands are defined",
                hint="install the udev rule and add your user to the kvm group manually",
            )

        if context.dry_run:
            return self.done(
                "would configure KVM (udev rule + kvm group)",
                commands=[list(command.argv) for command in commands],
                udev_rule_path=plan.udev_rule_path,
                udev_rule_content=plan.udev_rule_content,
                groups=list(plan.groups),
                re_login_required=True,
                notes=notes,
            )

        ok, message, results = self.run_commands(context, commands, what="configuring KVM")
        if not ok:
            return self.failed(
                message,
                hint="re-run 'avdfleet install --repair --only accel.kvm' with sudo",
                commands=self.command_lines(results),
            )
        if not context.simulated and plan.udev_rule_path and not Path(plan.udev_rule_path).is_file():
            context.warning(f"expected KVM udev rule at {plan.udev_rule_path} but it was not found")
        return self.done(
            "KVM configured; log out and back in (or reboot) for kvm group membership",
            commands=self.command_lines(results),
            udev_rule_path=plan.udev_rule_path,
            udev_rule_content=plan.udev_rule_content,
            groups=list(plan.groups),
            re_login_required=True,
            notes=notes,
        )

    def uninstall(self, context) -> OperationResult:
        return self.done(
            "the KVM udev rule and kvm group membership are host configuration and are kept"
        )


__all__ = ["KvmStep"]
