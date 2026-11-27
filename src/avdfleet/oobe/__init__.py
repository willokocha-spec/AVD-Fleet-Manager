"""OOBE detection, reset strategies, golden snapshots and the image matrix.

Public surface
--------------
:class:`OobeManager`
    Concrete ``avdfleet.core.interfaces.OobeManager``; injected with the runtime,
    AVD manager, image catalogue and an adb runner.
:class:`OobeDetector` / :func:`extract_top_activity` / :func:`classify_top_activity`
    ``dumpsys``-based detection primitives (also useful to other services).
:class:`ResetEngine`
    The four reset strategies with injectable clock and state enforcement.
:class:`OobeMatrixRegistry`
    Evidence-enforcing access to ``oobe_matrix.json``.
"""

from .detection import (
    PNG_SIGNATURE,
    ActivityKind,
    Detection,
    OobeDetector,
    RunOutput,
    SubprocessRunner,
    TopActivity,
    call_runner,
    classify_top_activity,
    extract_top_activity,
    is_png,
    repair_shell_png,
)
from .manager import DEFAULT_DEVICE_ID, OobeManager
from .matrix import GOLDEN_SNAPSHOT, OobeMatrixRegistry
from .reset import (
    PACKAGE_CLEANUP_COMMANDS,
    ResetEngine,
    is_active,
    parse_reset_strategy,
)

__all__ = [
    "DEFAULT_DEVICE_ID",
    "GOLDEN_SNAPSHOT",
    "PACKAGE_CLEANUP_COMMANDS",
    "PNG_SIGNATURE",
    "ActivityKind",
    "Detection",
    "OobeDetector",
    "OobeManager",
    "OobeMatrixRegistry",
    "ResetEngine",
    "RunOutput",
    "SubprocessRunner",
    "TopActivity",
    "call_runner",
    "classify_top_activity",
    "extract_top_activity",
    "is_active",
    "is_png",
    "parse_reset_strategy",
    "repair_shell_png",
]
