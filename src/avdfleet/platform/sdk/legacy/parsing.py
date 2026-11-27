"""Output parsers for the legacy Android command-line tools.

The tools print human-readable text; these parsers are the only place that
knows their dialects.  The canonical ``sdkmanager`` package table and the
``avdmanager`` ``Key: value`` blocks are shared with the new Android CLI and
therefore live in :mod:`avdfleet.platform.sdk.base`; this module re-exports
them under legacy-oriented names and adds the legacy-only formats
(install/license results, AVD creation/duplication, ``adb devices`` and
emulator versions).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..base import (  # noqa: F401 - re-exported for the legacy backend/tests
    AvdEntry,
    ToolResult,
    parse_avd_entries,
    parse_based_on,
    parse_device_profiles,
    parse_key_value_blocks,
    parse_package_table,
    parse_version,
)

_FAILED_PACKAGE_RE = re.compile(r"(?i)failed to find package|failed to install|could not (?:find|install)")
_LICENSE_PROMPT_RE = re.compile(r"(?i)accept\?|accept the license|license (?:for|agreement)|"
                                r"review licenses|licenses accepted|license not accepted")
_LICENSE_ACCEPTED_RE = re.compile(r"(?i)all sdk package licenses accepted|licenses accepted")
_DUPLICATE_AVD_RE = re.compile(
    r"(?i)already exists|duplicate(?: avd)?|same name|cannot be created because an AVD"
)
_MISSING_AVD_RE = re.compile(r"(?i)does not exist|not found|unknown avd|no avd (?:named|called)")
_MISSING_PACKAGE_RE = re.compile(r"(?i)failed to find package|package path is not valid|unknown package")
_CREATED_AVD_RE = re.compile(r"(?i)created successfully|AVD .* was created|created AVD")
_AVD_PATH_RE = re.compile(r"(?i)(?:^|\s)(?:path|created at|created in)[:=]\s*(.+)$")
_EMULATOR_LOG_PREFIXES = ("INFO", "WARNING", "WARN", "ERROR", "PANIC", "emulator:", "emulator ")

ADB_DEVICE_KEYS = ("product", "model", "device", "transport_id")


@dataclass(frozen=True)
class AdbDevice:
    """One line from ``adb devices -l``."""

    serial: str
    state: str
    product: str = ""
    model: str = ""
    device: str = ""
    transport_id: str = ""

    @property
    def console_port(self) -> int | None:
        """Console port for ``emulator-5554``-style serials."""
        match = re.match(r"^emulator-(\d+)(?::\d+)?$", self.serial)
        return int(match.group(1)) if match else None

    @property
    def adb_port(self) -> int | None:
        port = self.console_port
        return port + 1 if port is not None else None

    @property
    def is_online(self) -> bool:
        return self.state == "device"


# ---------------------------------------------------------------------------
# sdkmanager
# ---------------------------------------------------------------------------


def parse_sdkmanager_list(text: str) -> tuple[list, list]:
    """Return ``(installed, available)`` :class:`SdkPackage` lists."""
    return parse_package_table(text)


def parse_sdkmanager_version(text: str) -> str:
    """Extract the sdkmanager version (``11.0.0``)."""
    return parse_version(text)


def parse_avdmanager_list(text: str) -> list[AvdEntry]:
    """Parse ``avdmanager list avd`` into :class:`AvdEntry` records."""
    return parse_avd_entries(text)


def parse_avdmanager_devices(text: str) -> list[dict[str, str]]:
    """Parse ``avdmanager list device`` into ``{"id", "name", ...}`` dictionaries."""
    return parse_device_profiles(text)


def install_failed(result: ToolResult) -> bool:
    """Whether an ``sdkmanager --install`` run failed some package."""
    if not result.ok:
        return True
    return bool(_FAILED_PACKAGE_RE.search(result.combined))


def unknown_package(result: ToolResult) -> bool:
    return bool(_MISSING_PACKAGE_RE.search(result.combined))


def license_required(result: ToolResult) -> bool:
    """Whether the output shows unaccepted licenses blocking the operation."""
    text = result.combined
    if _LICENSE_ACCEPTED_RE.search(text):
        return False
    return bool(_LICENSE_PROMPT_RE.search(text))


def licenses_accepted(result: ToolResult) -> bool:
    """Best-effort success detection for ``sdkmanager --licenses``."""
    if not result.ok:
        return False
    text = result.combined
    if _LICENSE_ACCEPTED_RE.search(text):
        return True
    if _FAILED_PACKAGE_RE.search(text) or re.search(r"(?i)license not accepted", text):
        return False
    # A successful run that accepted at least one prompt, or a second run with
    # every license already accepted ("already accepted").
    return bool(
        re.search(r"(?i)accepted|already accepted|licenses? accepted", text)
    ) or bool(_LICENSE_PROMPT_RE.search(text))


def install_message(result: ToolResult) -> str:
    """One-line, redaction-safe summary of an install/uninstall result."""
    if result.timed_out:
        return f"tool timed out after {result.duration_s:.1f}s"
    if result.ok and not install_failed(result):
        return "packages processed successfully"
    if unknown_package(result):
        return "one or more package ids are not available in the configured SDK channel"
    if license_required(result):
        return "the Android SDK licenses have not been accepted"
    error_line = _first_error_line(result)
    if error_line:
        return error_line[:400]
    return f"tool failed with exit code {result.returncode}"


def license_message(result: ToolResult) -> str:
    if result.timed_out:
        return f"sdkmanager --licenses timed out after {result.duration_s:.1f}s"
    if result.ok and licenses_accepted(result):
        return "Android SDK licenses accepted"
    if result.ok:
        return "sdkmanager --licenses finished"
    if license_required(result):
        return "Android SDK licenses were not accepted"
    error_line = _first_error_line(result)
    return error_line[:400] if error_line else f"sdkmanager --licenses failed with exit code {result.returncode}"


def _first_error_line(result: ToolResult) -> str:
    for line in reversed(result.combined.splitlines()):
        stripped = line.strip()
        if stripped and not stripped.startswith("["):
            return stripped
    return ""


# ---------------------------------------------------------------------------
# avdmanager
# ---------------------------------------------------------------------------


def avd_created(result: ToolResult) -> bool:
    """Whether ``avdmanager create avd`` reported success."""
    return result.ok and bool(_CREATED_AVD_RE.search(result.combined))


def created_avd_path(text: str) -> str | None:
    """Extract a created AVD directory from avdmanager output when reported."""
    for line in (text or "").splitlines():
        match = _AVD_PATH_RE.search(line.strip())
        if match:
            candidate = match.group(1).strip().strip("'\"")
            if candidate and ("/" in candidate or "\\" in candidate):
                return candidate
    return None


def duplicate_avd(result: ToolResult) -> bool:
    """Whether creation failed because the AVD already exists."""
    return bool(_DUPLICATE_AVD_RE.search(result.combined))


def missing_avd(result: ToolResult) -> bool:
    """Whether deletion failed because the AVD does not exist."""
    return bool(_MISSING_AVD_RE.search(result.combined))


def create_avd_message(result: ToolResult) -> str:
    if result.timed_out:
        return f"avdmanager create avd timed out after {result.duration_s:.1f}s"
    if duplicate_avd(result):
        return "an AVD with this name already exists"
    if unknown_package(result):
        return "the system image is not installed in the configured SDK"
    line = _first_error_line(result)
    return line[:400] if line else f"avdmanager create avd failed with exit code {result.returncode}"


# ---------------------------------------------------------------------------
# emulator
# ---------------------------------------------------------------------------


def parse_emulator_version(text: str) -> str:
    """``Android emulator version 35.1.4.0 (build_id ...)`` -> ``35.1.4``."""
    match = re.search(r"version\s+(\d+(?:\.\d+)+)", text or "", re.IGNORECASE)
    if match:
        return match.group(1)
    return parse_version(text)


def parse_emulator_avd_list(text: str) -> list[str]:
    """Parse ``emulator -list-avds`` output (one AVD name per line)."""
    names: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(_EMULATOR_LOG_PREFIXES):
            continue
        names.append(line)
    return names


# ---------------------------------------------------------------------------
# adb
# ---------------------------------------------------------------------------


def parse_adb_version(text: str) -> str:
    """Extract the ADB version (``1.0.41``)."""
    return parse_version(text)


def parse_adb_devices(text: str) -> list[AdbDevice]:
    """Parse ``adb devices -l`` output into :class:`AdbDevice` records."""
    devices: list[AdbDevice] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        details: dict[str, str] = {}
        for token in parts[2:]:
            if ":" not in token:
                continue
            key, _, value = token.partition(":")
            if key in ADB_DEVICE_KEYS:
                details[key] = value
        devices.append(
            AdbDevice(
                serial=serial,
                state=state,
                product=details.get("product", ""),
                model=details.get("model", ""),
                device=details.get("device", ""),
                transport_id=details.get("transport_id", ""),
            )
        )
    return devices


def adb_shell_output(result: ToolResult) -> str:
    """Return ``adb shell`` stdout without the trailing newline."""
    return result.stdout.rstrip("\r\n")


__all__ = [
    "ADB_DEVICE_KEYS",
    "AdbDevice",
    "adb_shell_output",
    "avd_created",
    "create_avd_message",
    "created_avd_path",
    "duplicate_avd",
    "install_failed",
    "install_message",
    "license_message",
    "license_required",
    "licenses_accepted",
    "missing_avd",
    "parse_adb_devices",
    "parse_adb_version",
    "parse_avdmanager_devices",
    "parse_avdmanager_list",
    "parse_emulator_avd_list",
    "parse_emulator_version",
    "parse_sdkmanager_list",
    "parse_sdkmanager_version",
    "unknown_package",
]
