"""Output parsers for the new ``android`` command-line interface.

The new CLI can emit JSON; where it cannot (older builds, table-only
subcommands) we fall back to the canonical text formats that
``contracts/fakes-spec.md`` §4.5 defines.  Table parsing itself is shared with
the legacy backend through :mod:`avdfleet.platform.sdk.base`.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..base import (
    AvdEntry,
    SdkPackage,
    parse_avd_entries,
    parse_image_package_id,
    parse_package_table,
    parse_version,
    target_api_level,
)

_ANDROID_VERSION_RE = re.compile(r"(?i)\bandroid\b[^\d]{0,12}(\d+\.\d+(?:\.\d+)?)")
_JSON_LEADER_RE = re.compile(r"[\[{]")


# ---------------------------------------------------------------------------
# Version / JSON
# ---------------------------------------------------------------------------


def parse_android_version(text: str) -> str:
    """``android 1.0.0 (fake)`` -> ``1.0.0``."""
    match = _ANDROID_VERSION_RE.search(text or "")
    if match:
        return match.group(1)
    return parse_version(text)


def extract_json(text: str) -> Any | None:
    """Return the first JSON value embedded in *text* (log lines tolerated)."""
    if not text:
        return None
    decoder = json.JSONDecoder()
    for match in _JSON_LEADER_RE.finditer(text):
        start = match.start()
        try:
            value, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        return value
    return None


# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------


def _package_from_object(obj: Any, *, installed: bool | None = None) -> SdkPackage | None:
    if isinstance(obj, str):
        identifier = obj.strip()
        if not identifier:
            return None
        return SdkPackage(id=identifier, installed=bool(installed))
    if not isinstance(obj, dict):
        return None
    identifier_obj = (
        obj.get("id")
        or obj.get("package")
        or obj.get("packageId")
        or obj.get("package_id")
        or obj.get("path")
        or obj.get("name")
    )
    if not identifier_obj:
        return None
    location = obj.get("location") or obj.get("locationPath") or obj.get("installedPath")
    flag = installed if installed is not None else bool(obj.get("installed", False))
    return SdkPackage(
        id=str(identifier_obj),
        version=str(obj.get("version") or ""),
        description=str(obj.get("description") or obj.get("displayName") or ""),
        installed=flag,
        path=str(location) if location else None,
        optional=bool(obj.get("optional", False)),
    )


def _objects_from(payload: Any, *keys: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def parse_packages_json(payload: Any) -> tuple[list[SdkPackage], list[SdkPackage]]:
    """Parse a JSON package catalogue into ``(installed, available)`` lists."""
    installed: list[SdkPackage] = []
    available: list[SdkPackage] = []
    if isinstance(payload, dict) and any(
        key in payload for key in ("installed", "available", "packages", "updates")
    ):
        for key in ("installed",):
            for obj in _objects_from(payload, key):
                package = _package_from_object(obj, installed=True)
                if package is not None:
                    installed.append(package)
        for key in ("available", "packages", "updates"):
            for obj in _objects_from(payload, key):
                package = _package_from_object(obj, installed=None)
                if package is not None:
                    available.append(package)
        if installed or available:
            return installed, available
    for obj in _objects_from(payload, "packages", "items", "results"):
        package = _package_from_object(obj)
        if package is None:
            continue
        (installed if package.installed else available).append(package)
    return installed, available


def parse_sdk_list_output(text: str) -> tuple[list[SdkPackage], list[SdkPackage]]:
    """Parse ``android sdk list`` output: JSON when present, canonical table otherwise."""
    payload = extract_json(text)
    if payload is not None:
        installed, available = parse_packages_json(payload)
        if installed or available:
            return installed, available
    return parse_package_table(text)


# ---------------------------------------------------------------------------
# AVDs
# ---------------------------------------------------------------------------


def _avd_from_object(obj: Any) -> AvdEntry | None:
    if isinstance(obj, str):
        name = obj.strip()
        return AvdEntry(name=name) if name else None
    if not isinstance(obj, dict):
        return None
    name_obj = (
        obj.get("name")
        or obj.get("displayName")
        or obj.get("display_name")
        or obj.get("id")
    )
    if not name_obj:
        return None
    package = (
        obj.get("package")
        or obj.get("image")
        or obj.get("imagePackage")
        or obj.get("image_package")
        or obj.get("systemImage")
        or ""
    )
    package = str(package)
    parsed = parse_image_package_id(package) if package.startswith("system-images;") else None
    package_api, package_tag, package_abi = parsed if parsed else ("", "", "")
    raw_api = obj.get("api") or obj.get("apiLevel") or obj.get("api_level")
    if raw_api is None and package_api:
        raw_api = target_api_level(package_api)
    try:
        api_level = int(raw_api) if raw_api is not None else 0
    except (TypeError, ValueError):
        api_level = 0
    tag = str(obj.get("tag") or package_tag or "")
    abi = str(obj.get("abi") or obj.get("abiType") or package_abi or "")
    target = str(obj.get("target") or package or "")
    based_on = str(obj.get("basedOn") or obj.get("based_on") or "")
    if not based_on and api_level and tag and abi:
        based_on = f"Android API {api_level} Tag/ABI: {tag}/{abi}"
    return AvdEntry(
        name=str(name_obj),
        path=str(obj.get("path") or obj.get("dir") or ""),
        target=target,
        based_on=based_on,
        api_level=api_level,
        tag=tag,
        abi=abi,
    )


def parse_avd_list_json(payload: Any) -> list[AvdEntry]:
    """Parse a JSON AVD list into :class:`AvdEntry` records."""
    entries: list[AvdEntry] = []
    for obj in _objects_from(payload, "avds", "instances", "items", "results"):
        entry = _avd_from_object(obj)
        if entry is not None:
            entries.append(entry)
    if not entries and isinstance(payload, list):
        for obj in payload:
            entry = _avd_from_object(obj)
            if entry is not None:
                entries.append(entry)
    return entries


def parse_avd_list_table(text: str) -> list[AvdEntry]:
    """Fallback table parser for ``android avd list`` without JSON support."""
    entries = parse_avd_entries(text)
    if entries:
        return entries
    # A plain column table ("Name  Device  Target") is also possible.
    fallback: list[AvdEntry] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith(("name", "avd", "---", "available")):
            continue
        cells = re.split(r"\s{2,}|\t+", line)
        if not cells or not cells[0]:
            continue
        fallback.append(AvdEntry(name=cells[0]))
    return fallback


def parse_avd_list_output(text: str) -> list[AvdEntry]:
    """Parse ``android avd list`` output: JSON when present, table otherwise."""
    payload = extract_json(text)
    if payload is not None:
        entries = parse_avd_list_json(payload)
        if entries:
            return entries
    return parse_avd_list_table(text)


# ---------------------------------------------------------------------------
# Emulator list / licenses
# ---------------------------------------------------------------------------


def parse_emulator_list_json(payload: Any) -> list[dict[str, Any]]:
    """Normalise ``android emulator list`` JSON into a list of dictionaries."""
    emulators: list[dict[str, Any]] = []
    for obj in _objects_from(payload, "emulators", "instances", "items", "results", "avds"):
        if isinstance(obj, str):
            emulators.append({"name": obj})
        elif isinstance(obj, dict):
            emulators.append(dict(obj))
    if not emulators and isinstance(payload, list):
        for obj in payload:
            if isinstance(obj, str):
                emulators.append({"name": obj})
            elif isinstance(obj, dict):
                emulators.append(dict(obj))
    return emulators


def parse_emulator_list_output(text: str) -> list[dict[str, Any]]:
    """Parse ``android emulator list`` output (JSON first, then line list)."""
    payload = extract_json(text)
    if payload is not None:
        emulators = parse_emulator_list_json(payload)
        if emulators:
            return emulators
    emulators = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("INFO", "WARNING", "ERROR")):
            continue
        cells = re.split(r"\s{2,}|\t+", line)
        emulators.append({"name": cells[0], "raw": line})
    return emulators


__all__ = [
    "extract_json",
    "parse_android_version",
    "parse_avd_list_json",
    "parse_avd_list_output",
    "parse_avd_list_table",
    "parse_emulator_list_json",
    "parse_emulator_list_output",
    "parse_packages_json",
    "parse_sdk_list_output",
]
