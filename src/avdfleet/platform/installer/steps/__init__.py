"""Installer step registry and selection.

The declared order is the execution order.  Dependencies are explicit so
``--only`` can automatically pull in prerequisite steps (dependency closure) and
the engine can verify that a step never runs before its dependencies.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from avdfleet.core.errors import InstallerError, ValidationError
from avdfleet.core.models import InstallStep

from .accel_aehd import AehdStep
from .accel_detect import AccelDetectStep
from .accel_kvm import KvmStep
from .accel_whpx import WhpxStep
from .base import BaseStep
from .sdk_cmdline import SdkCmdlineToolsStep
from .sdk_env import SdkEnvStep
from .sdk_licenses import SdkLicensesStep
from .sdk_packages import SdkPackagesStep
from .studio import StudioConfigStep
from .tools_appium import AppiumStep
from .tools_jdk import JdkStep
from .tools_node import NodeStep
from .tools_scrcpy import ScrcpyStep

#: Canonical ordered step classes (the execution order of a full install).
STEP_CLASSES: tuple[type[BaseStep], ...] = (
    JdkStep,
    NodeStep,
    AppiumStep,
    ScrcpyStep,
    SdkCmdlineToolsStep,
    SdkLicensesStep,
    SdkPackagesStep,
    SdkEnvStep,
    StudioConfigStep,
    AccelDetectStep,
    AehdStep,
    WhpxStep,
    KvmStep,
)

STEP_IDS: tuple[str, ...] = tuple(step.id for step in STEP_CLASSES)

#: Friendly aliases accepted by ``--only``.
STEP_ALIASES: dict[str, str] = {
    "jdk": "tools.jdk",
    "java": "tools.jdk",
    "node": "tools.node",
    "nodejs": "tools.node",
    "appium": "tools.appium",
    "uiautomator2": "tools.appium",
    "inspector": "tools.appium",
    "scrcpy": "tools.scrcpy",
    "cmdline": "sdk.cmdline_tools",
    "cmdline-tools": "sdk.cmdline_tools",
    "cmdline_tools": "sdk.cmdline_tools",
    "license": "sdk.licenses",
    "licenses": "sdk.licenses",
    "packages": "sdk.packages",
    "sdk-packages": "sdk.packages",
    "env": "sdk.env",
    "environment": "sdk.env",
    "studio": "studio.config",
    "detect": "accel.detect",
    "whpx": "accel.whpx",
    "aehd": "accel.aehd",
    "kvm": "accel.kvm",
}

#: Phases that ``--only <phase>`` selects in full.
PHASES: tuple[str, ...] = ("tools", "sdk", "config", "acceleration", "core")


def _by_id() -> dict[str, type[BaseStep]]:
    return {step.id: step for step in STEP_CLASSES}


def step_ids() -> tuple[str, ...]:
    return STEP_IDS


def get_step_class(step_id: str) -> type[BaseStep]:
    mapping = _by_id()
    if step_id not in mapping:
        raise ValidationError(
            f"unknown installer step: {step_id!r}",
            hint=f"available steps: {', '.join(STEP_IDS)}",
            details={"steps": list(STEP_IDS)},
        )
    return mapping[step_id]


def _matches(token: str, step: type[BaseStep]) -> bool:
    if token == step.id or token == step.phase:
        return True
    if token.endswith("*"):
        return step.id.startswith(token[:-1])
    if token.endswith("."):
        return step.id.startswith(token)
    return False


def resolve_selection(only: Sequence[str]) -> list[str]:
    """Resolve ``--only`` tokens (ids, aliases, phases, prefixes) to step ids."""
    requested: list[str] = []
    unknown: list[str] = []
    for raw in only:
        token = (raw or "").strip().lower()
        if not token:
            continue
        alias = STEP_ALIASES.get(token)
        if alias:
            requested.append(alias)
            continue
        matches = [step.id for step in STEP_CLASSES if _matches(token, step)]
        if not matches and "." in token:
            matches = [step.id for step in STEP_CLASSES if step.id.startswith(token)]
        if matches:
            requested.extend(matches)
        else:
            unknown.append(raw)
    if unknown:
        raise ValidationError(
            f"unknown installer step selection: {', '.join(unknown)}",
            hint=f"available steps: {', '.join(STEP_IDS)}",
            details={"unknown": unknown, "steps": list(STEP_IDS)},
        )
    # Preserve selection order, drop duplicates.
    return list(dict.fromkeys(requested))


def validate_steps(steps: Sequence[BaseStep]) -> None:
    """Verify unique ids and that every dependency exists and comes earlier."""
    seen: set[str] = set()
    for step in steps:
        if not step.id or not step.title:
            raise InstallerError(f"installer step {type(step).__name__} is missing an id/title")
        if step.id in seen:
            raise InstallerError(f"duplicate installer step id: {step.id!r}")
        for dependency in step.depends_on:
            if dependency not in seen:
                raise InstallerError(
                    f"installer step {step.id!r} depends on {dependency!r}, "
                    "which is missing or declared later"
                )
        seen.add(step.id)


def build_steps(
    *,
    only: Iterable[str] = (),
    skip_appium: bool = False,
    skip_studio: bool = False,
    skip_scrcpy: bool = False,
) -> list[BaseStep]:
    """Instantiate the configured step list (dependency closure for ``--only``)."""
    steps: list[BaseStep] = [step_class() for step_class in STEP_CLASSES]
    if skip_appium:
        steps = [step for step in steps if step.id != "tools.appium"]
    if skip_studio:
        steps = [step for step in steps if step.id != "studio.config"]
    if skip_scrcpy:
        steps = [step for step in steps if step.id != "tools.scrcpy"]

    selected = list(only)
    if selected:
        wanted = resolve_selection(selected)
        by_id = {step.id: step for step in steps}
        closure: list[str] = []
        pending = list(reversed(wanted))
        while pending:
            step_id = pending.pop()
            if step_id in closure:
                continue
            step = by_id.get(step_id)
            if step is None:
                raise ValidationError(
                    f"installer step {step_id!r} is not available in this install "
                    "(it may be disabled by --no-appium/--no-studio/--no-scrcpy)",
                    hint=f"available steps: {', '.join(step.id for step in steps)}",
                )
            closure.append(step_id)
            pending.extend(reversed(step.depends_on))
        steps = [step for step in steps if step.id in set(closure)]

    validate_steps(steps)
    return steps


def all_step_specs() -> list[InstallStep]:
    """Declarative metadata for every known step (used by ``plan`` and the API)."""
    return [step_class().spec() for step_class in STEP_CLASSES]


__all__ = [
    "PHASES",
    "STEP_ALIASES",
    "STEP_CLASSES",
    "STEP_IDS",
    "BaseStep",
    "all_step_specs",
    "build_steps",
    "get_step_class",
    "resolve_selection",
    "step_ids",
    "validate_steps",
]
