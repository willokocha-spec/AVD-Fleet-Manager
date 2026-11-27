"""Step: detect the host accelerator and nested-virtualisation constraints.

This is intentionally informational (``critical=False``, ``always_run=True``):
it records what the host can do and warns when nested virtualisation limits the
emulator.  The OS-specific enable steps (``accel.whpx``/``accel.kvm``) are the
critical ones.
"""

from __future__ import annotations

from avdfleet.core.models import AcceleratorKind, OperationResult

from .base import BaseStep


class AccelDetectStep(BaseStep):
    id = "accel.detect"
    title = "Detect hardware acceleration"
    description = (
        "Detect WHPX/KVM/HVF, report nested-virtualisation limits and warn when no "
        "hardware acceleration is usable."
    )
    phase = "acceleration"
    critical = False
    requires_reboot = False
    depends_on = ()
    always_run = True

    def check(self, context) -> bool:
        return context.facts.get("acceleration") is not None

    def run(self, context) -> OperationResult:
        plan = context.provider.virtualization_plan()
        nested = plan.nested if plan.nested is not None else context.provider.nested_virtualization()
        context.facts["acceleration"] = plan
        context.facts["nested_virtualization"] = nested
        context.facts["accelerator_kind"] = plan.kind.value

        notes = list(plan.notes)
        if nested:
            message = (
                "nested virtualisation detected: the Android emulator needs the host hypervisor "
                "exposed to this guest; enable nested virtualisation in the outer VM"
            )
            context.warning(message)
            notes.append(message)

        payload = {
            "kind": plan.kind.value,
            "available": plan.available,
            "enabled": plan.enabled,
            "requires_reboot": plan.requires_reboot,
            "nested": nested,
            "notes": notes,
        }
        if plan.kind is AcceleratorKind.NONE or not plan.available:
            return self.failed(
                f"no usable hardware acceleration detected ({plan.detail}). "
                "The Android emulator cannot boot without WHPX/KVM/HVF (BRIEF section 6).",
                hint=(
                    "enable VT-x/AMD-V (or nested virtualisation) in firmware/VM settings; "
                    "run 'avdfleet doctor' for details"
                ),
                **payload,
            )
        context.info(f"hardware acceleration detected: {plan.detail}")
        return self.done(f"hardware acceleration {plan.detail}", **payload)


__all__ = ["AccelDetectStep"]
