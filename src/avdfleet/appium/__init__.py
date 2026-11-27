"""Appium subsystem: server lifecycle, port planning and UiAutomator2 capabilities.

Public surface::

    from avdfleet.appium import AppiumManager, AppiumPortPlanner, build_capabilities

``AppiumManager`` implements :class:`avdfleet.core.interfaces.AppiumManager` and
receives its executable resolver, adb runner, port allocator/planner and event
bus via constructor injection, so it can be exercised with a fake Appium server
and a fake adb without either being installed.
"""

from .capabilities import build_capabilities, build_uiautomator2_capabilities, serial_for
from .manager import AppiumManager
from .ports import AppiumPortPlanner, AppiumPorts

__all__ = [
    "AppiumManager",
    "AppiumPortPlanner",
    "AppiumPorts",
    "build_capabilities",
    "build_uiautomator2_capabilities",
    "serial_for",
]
