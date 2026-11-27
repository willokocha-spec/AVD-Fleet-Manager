"""Legacy Android SDK toolchain backend.

Layout::

    sdkmanager.py  -- thin ``sdkmanager`` command wrapper (install/uninstall/licenses)
    avdmanager.py  -- thin ``avdmanager`` command wrapper (AVD CRUD)
    adb.py         -- thin ``adb`` command wrapper (devices/shell/emu/forward)
    emulator.py    -- emulator version/list + launch argv builder
    parsing.py     -- legacy output dialects (packages, AVDs, licenses, adb)
    console.py     -- re-export of the emulator console client
    backend.py     -- :class:`LegacySdkBackend` implementing ``SdkBackend``
"""

from __future__ import annotations

from .adb import Adb, AdbDevice
from .avdmanager import AvdManager
from .backend import LegacySdkBackend
from .console import EmulatorConsole, EmulatorConsoleError
from .emulator import Emulator
from .sdkmanager import SdkManager

__all__ = [
    "Adb",
    "AdbDevice",
    "AvdManager",
    "Emulator",
    "EmulatorConsole",
    "EmulatorConsoleError",
    "LegacySdkBackend",
    "SdkManager",
]
