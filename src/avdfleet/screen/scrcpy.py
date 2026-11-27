"""scrcpy detection and launch-command builder for local display fallback.

The daemon normally streams frames over WebSocket; when the operator wants the
real emulator window on the local desktop, scrcpy is the documented fallback
(BRIEF §3.8).  This module only *builds and (optionally) spawns* the command;
it never requires scrcpy to be installed and is inert in tests.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.childenv import child_environment
from ..core.errors import ScreenError
from ..core.logging import get_logger
from ..core.models import OperationResult

logger = get_logger("avdfleet.screen.scrcpy")

DEFAULT_WINDOW_PREFIX = "AVD Fleet"


@dataclass(frozen=True)
class ScrcpyConfig:
    """Options translated into scrcpy flags."""

    window_title: str = ""
    max_size: int | None = None
    bit_rate: str | None = None
    max_fps: int | None = None
    stay_awake: bool = True
    turn_screen_off: bool = False
    no_audio: bool = True
    always_on_top: bool = False
    extra_args: tuple[str, ...] = field(default_factory=tuple)


def find_scrcpy(
    *,
    executable: str = "scrcpy",
    which: Callable[[str], str | None] | None = None,
    search_paths: Sequence[str | os.PathLike[str]] = (),
) -> str | None:
    """Return the scrcpy executable path when available, else ``None``."""
    resolver = which or shutil.which
    try:
        found = resolver(executable)
    except Exception:  # noqa: BLE001 - detection must never raise
        found = None
    if found:
        return found
    for directory in search_paths:
        candidate = os.path.join(str(directory), executable)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def build_scrcpy_command(
    serial: str,
    *,
    executable: str = "scrcpy",
    config: ScrcpyConfig | None = None,
    window_title: str | None = None,
) -> list[str]:
    """Build the scrcpy argv for *serial* (pure function, no spawning)."""
    if not serial or not str(serial).strip():
        raise ScreenError(
            "cannot launch scrcpy without an adb serial",
            hint="start the instance first",
        )
    settings = config or ScrcpyConfig()
    title = window_title or settings.window_title
    argv = [executable, "--serial", str(serial)]
    if title:
        argv.extend(["--window-title", title])
    if settings.max_size:
        argv.extend(["--max-size", str(int(settings.max_size))])
    if settings.bit_rate:
        argv.extend(["--video-bit-rate", str(settings.bit_rate)])
    if settings.max_fps:
        argv.extend(["--max-fps", str(int(settings.max_fps))])
    if settings.stay_awake:
        argv.append("--stay-awake")
    if settings.turn_screen_off:
        argv.append("--turn-screen-off")
    if settings.no_audio:
        argv.append("--no-audio")
    if settings.always_on_top:
        argv.append("--always-on-top")
    argv.extend(str(item) for item in settings.extra_args)
    return argv


class ScrcpyLauncher:
    """Detect and optionally spawn the local-display scrcpy fallback."""

    def __init__(
        self,
        *,
        executable: str = "scrcpy",
        which: Callable[[str], str | None] | None = None,
        spawner: Callable[..., Any] | None = None,
        window_prefix: str = DEFAULT_WINDOW_PREFIX,
    ) -> None:
        self._executable = executable
        self._which = which
        self._spawner = spawner
        self._window_prefix = window_prefix

    def executable(self) -> str | None:
        return find_scrcpy(executable=self._executable, which=self._which)

    def available(self) -> bool:
        return self.executable() is not None

    def command(self, serial: str, *, avd_name: str = "", config: ScrcpyConfig | None = None) -> list[str]:
        executable = self.executable() or self._executable
        title = f"{self._window_prefix} — {avd_name or serial}"
        return build_scrcpy_command(
            serial,
            executable=executable,
            config=config,
            window_title=title,
        )

    def launch(
        self,
        serial: str,
        *,
        avd_name: str = "",
        config: ScrcpyConfig | None = None,
        spawn: bool = True,
    ) -> OperationResult:
        """Return the launch command; spawn it only when *spawn* is true."""
        executable = self.executable()
        if executable is None:
            return OperationResult(
                ok=False,
                message="scrcpy is not installed; the local-display fallback is unavailable",
                data={"available": False},
            )
        argv = self.command(serial, avd_name=avd_name, config=config)
        if not spawn:
            return OperationResult(ok=True, message="scrcpy launch command built", data={"argv": argv})
        spawner = self._spawner
        try:
            process = (
                spawner(argv)
                if spawner is not None
                else subprocess.Popen(  # noqa: S603 - injected/validated argv
                    argv, close_fds=True, env=child_environment()
                )
            )
        except Exception as exc:  # noqa: BLE001 - report as operation failure
            return OperationResult(ok=False, message=f"failed to launch scrcpy: {exc}", data={"argv": argv})
        pid = getattr(process, "pid", None)
        return OperationResult(ok=True, message="scrcpy launched", data={"argv": argv, "pid": pid})


__all__ = [
    "DEFAULT_WINDOW_PREFIX",
    "ScrcpyConfig",
    "ScrcpyLauncher",
    "build_scrcpy_command",
    "find_scrcpy",
]
