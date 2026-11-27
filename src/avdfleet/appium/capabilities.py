"""UiAutomator2 capability construction for Appium 4.

Capabilities are built from an :class:`~avdfleet.core.models.AvdInfo` that must
already be running: avdfleet's runtime manager owns emulator launch, and letting
the UiAutomator2 driver start an AVD would bypass fleet bookkeeping (ports,
proxy wiring, snapshots).  ``appium:udid`` is always the real adb serial so
parallel sessions route deterministically, and the explicit
``systemPort``/``mjpegServerPort`` values come from the central port planner.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..core.errors import AppiumError, InstanceError
from ..core.models import AvdInfo, InstanceState, Template
from .ports import AppiumPorts

__all__ = [
    "DEFAULT_ADB_EXEC_TIMEOUT_MS",
    "DEFAULT_NEW_COMMAND_TIMEOUT_S",
    "build_capabilities",
    "build_uiautomator2_capabilities",
    "serial_for",
]

DEFAULT_NEW_COMMAND_TIMEOUT_S = 120
DEFAULT_ADB_EXEC_TIMEOUT_MS = 20_000


def serial_for(avd: AvdInfo) -> str:
    """Return the adb serial for *avd* or raise :class:`InstanceError`."""
    serial = avd.adb_serial
    if not serial and avd.ports is not None and avd.ports.console:
        serial = f"emulator-{avd.ports.console}"
    if not serial:
        raise InstanceError(
            f"instance {avd.name!r} has no adb serial",
            hint="start the instance so the runtime assigns a console/adb port",
        )
    return serial


def build_uiautomator2_capabilities(
    avd: AvdInfo,
    *,
    template: Template | None = None,
    ports: AppiumPorts | None = None,
    no_reset: bool = False,
    new_command_timeout: int = DEFAULT_NEW_COMMAND_TIMEOUT_S,
    adb_exec_timeout: int = DEFAULT_ADB_EXEC_TIMEOUT_MS,
    auto_grant_permissions: bool = True,
    chromedriver_executable: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the UiAutomator2 W3C capabilities for a running instance.

    ``no_reset=False`` (the default) is the fleet-correct value: avdfleet resets
    app state through its own OOBE/snapshot flows, not through Appium session
    side effects.  Pass ``no_reset=True`` for a session that must keep the app's
    data untouched.
    """
    if not isinstance(avd, AvdInfo):
        raise InstanceError("build_capabilities requires an AvdInfo record")
    if avd.state is not InstanceState.RUNNING:
        raise InstanceError(
            f"instance {avd.name!r} is not running (state: {avd.state.value})",
            hint="start the instance, wait for boot completion, then create the Appium session",
        )
    serial = serial_for(avd)

    if ports is None:
        runtime_ports = avd.ports
        if (
            runtime_ports is None
            or not runtime_ports.appium_server
            or not runtime_ports.system_port
            or not runtime_ports.mjpeg
        ):
            raise AppiumError(
                f"instance {avd.name!r} has no Appium ports allocated",
                hint="plan ports first (AppiumManager.plan_ports) so systemPort/MJPEG are unique",
            )
        ports = AppiumPorts(
            server=int(runtime_ports.appium_server),
            system_port=int(runtime_ports.system_port),
            mjpeg_port=int(runtime_ports.mjpeg),
        )
    ports.validate()

    device_name = avd.name
    if template is not None and not device_name:  # pragma: no cover - name is required
        device_name = template.device_id or template.id
    if not device_name:
        device_name = serial

    capabilities: dict[str, Any] = {
        "platformName": "Android",
        "appium:automationName": "UiAutomator2",
        "appium:deviceName": device_name,
        "appium:udid": serial,
        "appium:systemPort": int(ports.system_port),
        "appium:mjpegServerPort": int(ports.mjpeg_port),
        "appium:noReset": bool(no_reset),
        "appium:autoGrantPermissions": bool(auto_grant_permissions),
        "appium:newCommandTimeout": int(new_command_timeout),
        "appium:adbExecTimeout": int(adb_exec_timeout),
        "appium:suppressKillServer": True,
    }
    if chromedriver_executable:
        capabilities["appium:chromedriverExecutable"] = str(chromedriver_executable)
    if extra:
        for key, value in extra.items():
            if value is not None:
                capabilities[str(key)] = value
    return capabilities


#: Backwards-friendly alias used by documentation and integration callers.
build_capabilities = build_uiautomator2_capabilities
