"""Built-in instrumentation profiles.

The four profiles from the feature brief are shipped as read-only starting
points; operators clone them into editable copies. "Custom" is deliberately
minimal (a root verification) because it exists to be edited.
"""

from __future__ import annotations

from ..core.models import utcnow
from .models import InstrumentationProfile, ProfileStep, StepAction
from .repository import InstrumentationRepository


def _root() -> InstrumentationProfile:
    return InstrumentationProfile(
        id="prof_builtin_root",
        name="Root",
        description="Verify that the AVD has usable root (Magisk or userdebug adb root).",
        builtin=True,
        steps=[
            ProfileStep(id="adb-root", action=StepAction.ENABLE_ADB_ROOT),
            ProfileStep(id="root-verify", action=StepAction.VERIFY_ROOT),
        ],
    )


def _magisk_zygisk() -> InstrumentationProfile:
    return InstrumentationProfile(
        id="prof_builtin_magisk_zygisk",
        name="Magisk + Zygisk",
        description=(
            "Install Magisk with the configured installer, enable Zygisk and verify it became"
            " operational after the reboot."
        ),
        builtin=True,
        steps=[
            ProfileStep(id="magisk-install", action=StepAction.INSTALL_MAGISK),
            ProfileStep(id="magisk-setup", action=StepAction.COMPLETE_MAGISK_SETUP),
            ProfileStep(id="zygisk-enable", action=StepAction.ENABLE_ZYGISK),
            ProfileStep(id="zygisk-reboot", action=StepAction.REBOOT),
            ProfileStep(
                id="zygisk-verify",
                action=StepAction.VERIFY,
                params={"expect": {"magisk": True, "zygisk_active": True}},
            ),
        ],
    )


def _lsposed() -> InstrumentationProfile:
    return InstrumentationProfile(
        id="prof_builtin_lsposed",
        name="LSPosed",
        description=(
            "Magisk + Zygisk, then the LSPosed framework module from the library, then a"
            " framework health check."
        ),
        builtin=True,
        steps=[
            ProfileStep(id="magisk-install", action=StepAction.INSTALL_MAGISK),
            ProfileStep(id="magisk-setup", action=StepAction.COMPLETE_MAGISK_SETUP),
            ProfileStep(id="zygisk-enable", action=StepAction.ENABLE_ZYGISK),
            ProfileStep(id="lsposed-install", action=StepAction.INSTALL_LSPOSED),
            ProfileStep(id="lsposed-reboot", action=StepAction.REBOOT),
            ProfileStep(id="lsposed-manager", action=StepAction.INSTALL_LSPOSED_MANAGER),
            ProfileStep(
                id="lsposed-verify",
                action=StepAction.VERIFY,
                params={"expect": {"magisk": True, "lsposed_active": True}},
            ),
        ],
    )


def _xposed_instrumentation() -> InstrumentationProfile:
    return InstrumentationProfile(
        id="prof_builtin_xposed",
        name="Xposed Instrumentation",
        description=(
            "Full stack: Magisk, Zygisk, LSPosed and an Xposed APK from the library. Module"
            " activation and per-app scope still require the LSPosed manager (guided step)."
        ),
        builtin=True,
        steps=[
            ProfileStep(id="magisk-install", action=StepAction.INSTALL_MAGISK),
            ProfileStep(id="magisk-setup", action=StepAction.COMPLETE_MAGISK_SETUP),
            ProfileStep(id="zygisk-enable", action=StepAction.ENABLE_ZYGISK),
            ProfileStep(id="lsposed-install", action=StepAction.INSTALL_LSPOSED),
            ProfileStep(id="lsposed-reboot", action=StepAction.REBOOT),
            ProfileStep(id="lsposed-manager", action=StepAction.INSTALL_LSPOSED_MANAGER),
            ProfileStep(id="xposed-apk-install", action=StepAction.INSTALL_MODULE),
            ProfileStep(
                id="framework-verify",
                action=StepAction.VERIFY,
                params={"expect": {"magisk": True, "lsposed_active": True}},
            ),
        ],
    )


def _custom() -> InstrumentationProfile:
    return InstrumentationProfile(
        id="prof_builtin_custom",
        name="Custom",
        description="Empty starting point: add the steps and modules you need.",
        builtin=True,
        steps=[ProfileStep(id="root-verify", action=StepAction.VERIFY_ROOT)],
    )


#: Every built-in profile, in sidebar order.
BUILTIN_PROFILES: tuple[InstrumentationProfile, ...] = (
    _root(),
    _magisk_zygisk(),
    _lsposed(),
    _xposed_instrumentation(),
    _custom(),
)


def ensure_builtin_profiles(repository: InstrumentationRepository) -> int:
    """Insert any missing built-in profile; returns how many were added.

    Idempotent and safe to call on every startup: existing rows are left alone so
    an operator's edits (a cloned profile) are never overwritten.
    """
    added = 0
    for template in BUILTIN_PROFILES:
        stamp = utcnow()
        current = repository.get_profile(template.id) or repository.get_profile_by_name(
            template.name
        )
        if current is None:
            repository.add_profile(
                template.model_copy(update={"created_at": stamp, "updated_at": stamp})
            )
            added += 1
            continue
        if current.builtin:
            # Built-ins cannot be edited through the API, so refreshing them is safe
            # and is what keeps an existing installation's steps current after an
            # upgrade adds a step.
            repository.save_profile(
                template.model_copy(
                    update={
                        "created_at": current.created_at,
                        "updated_at": stamp,
                        "revision": current.revision,
                    }
                )
            )
    return added


__all__ = ["BUILTIN_PROFILES", "ensure_builtin_profiles"]
