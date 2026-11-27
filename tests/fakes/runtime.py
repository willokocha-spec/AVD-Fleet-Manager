"""Runtime support for the AVD Fleet executable fakes.

This module owns everything that is shared between the pytest fixtures
(``tests/fakes/fixtures.py``) and the standalone fake command line
(``tests/fakes/fake_cli.py``):

* the catalogue of fake SDK packages / system images / device profiles,
* atomic, lock-protected access to ``$AVDFLEET_FAKE_HOME/state.json`` and the
  ``events.jsonl`` argv audit log,
* creation of per-OS launch shims (``bin/`` on ``PATH``) and of tool wrappers
  inside the fake SDK layout, and
* :class:`FakeTools`, the session-scoped object the fixtures build once.

Only the Python standard library is used; nothing in here imports pytest so the
same code runs in a subprocess.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import platform
import re
import shutil
import socket
import struct
import sys
import threading
import time
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOOLS: tuple[str, ...] = (
    "adb",
    "emulator",
    "sdkmanager",
    "avdmanager",
    "android",
    "appium",
    "node",
    "npm",
    "npx",
    "uv",
    "java",
    "scrcpy",
    "python",
)

DEFAULT_BOOT_SECONDS = 0.2
DEFAULT_APPIUM_PORT = 4723
CONSOLE_AUTH_TOKEN = "fake-token"
GRPC_TOKEN = "fake-grpc-token"
SETUP_WIZARD_ACTIVITY = "com.google.android.setupwizard/.SetupWizardActivity"
LAUNCHER_ACTIVITY = "com.google.android.apps.nexuslauncher/.NexusLauncherActivity"
CONSOLE_PORT_START = 5554
CONSOLE_PORT_END = 5680
GRPC_PORT_OFFSET = 3000

ANDROID_SDK_LICENSE_HASH = "24333f8a63b6825ea9c5514f83c2829b004d1fee"
ANDROID_PREVIEW_LICENSE_HASH = "84831b9409646a918e30573bab4c9c91346d8abd"

FakeState = dict[str, Any]


class FakeToolsError(RuntimeError):
    """Raised when the fake environment cannot be built or used."""


class FakeStateLockError(FakeToolsError):
    """Raised when the state lock cannot be acquired in time."""


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def env_value(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def fake_home(home: str | Path | None = None) -> Path:
    """Return the directory holding ``state.json`` / ``events.jsonl``."""
    if home is not None:
        return Path(home)
    raw = env_value("AVDFLEET_FAKE_HOME")
    if not raw:
        raise FakeToolsError("AVDFLEET_FAKE_HOME is not set; run the tool through the fake fixtures")
    return Path(raw)


def now() -> float:
    """Current fake time (honours ``AVDFLEET_FAKE_NOW``)."""
    fixed = env_value("AVDFLEET_FAKE_NOW")
    if fixed:
        try:
            return float(fixed)
        except ValueError:
            pass
    return time.time()


def boot_seconds() -> float:
    raw = env_value("AVDFLEET_FAKE_BOOT_SECONDS")
    if not raw:
        return DEFAULT_BOOT_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_BOOT_SECONDS


def host_os_override() -> str | None:
    raw = (env_value("AVDFLEET_FAKE_HOST_OS") or "").strip().lower()
    return raw if raw in {"windows", "macos", "linux"} else None


def real_host_os() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def detected_host_os() -> str:
    """Host OS as the fakes should present it (real detection unless overridden)."""
    return host_os_override() or real_host_os()


def arch_override() -> str | None:
    raw = (env_value("AVDFLEET_FAKE_ARCH") or "").strip().lower()
    if raw in {"x86_64", "arm64", "arm64-v8a", "aarch64", "amd64"}:
        return "arm64" if raw in {"arm64", "arm64-v8a", "aarch64"} else "x86_64"
    return None


def real_arch() -> str:
    machine = os.environ.get("PROCESSOR_ARCHITECTURE", "")
    if not machine and hasattr(os, "uname"):
        machine = os.uname().machine
    machine = machine.lower()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return "x86_64"


def detected_arch() -> str:
    return arch_override() or real_arch()


def host_abi() -> str:
    """The ABI seeded for system images: x86_64 or arm64-v8a."""
    return "arm64-v8a" if detected_arch() == "arm64" else "x86_64"


def accel() -> str:
    raw = (env_value("AVDFLEET_FAKE_ACCEL") or "").strip().lower()
    if raw in {"whpx", "kvm", "hvf", "aehd", "none"}:
        return raw
    return {"windows": "whpx", "macos": "hvf", "linux": "kvm"}.get(detected_host_os(), "none")


def appium_default_port() -> int:
    raw = env_value("AVDFLEET_FAKE_APPIUM_PORT")
    if not raw:
        return DEFAULT_APPIUM_PORT
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_APPIUM_PORT


def fail_set() -> set[str]:
    raw = env_value("AVDFLEET_FAKE_FAIL") or ""
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def should_fail(*selectors: str) -> bool:
    failures = fail_set()
    return any(selector.lower() in failures for selector in selectors)


def script_map() -> dict[str, dict[str, Any]]:
    raw = env_value("AVDFLEET_FAKE_SCRIPT_MAP")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for key, value in parsed.items():
        if isinstance(value, str):
            result[str(key)] = {"stdout": value, "stderr": "", "exit_code": 0}
        elif isinstance(value, dict):
            result[str(key)] = {
                "stdout": str(value.get("stdout", "")),
                "stderr": str(value.get("stderr", "")),
                "exit_code": int(value.get("exit_code", 0) or 0),
            }
    return result


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


def _lock_file(handle: Any) -> None:  # pragma: no cover - platform specific
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: Any) -> None:  # pragma: no cover - platform specific
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def file_lock(path: str | Path, *, timeout: float = 10.0) -> Iterator[None]:
    """Process-wide exclusive lock on *path* with a short retry loop."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    handle = None
    acquired = False
    try:
        while True:
            try:
                handle = open(lock_path, "a+")  # noqa: SIM115 - closed in finally
                _lock_file(handle)
                acquired = True
                break
            except OSError:
                if handle is not None:
                    handle.close()
                    handle = None
                if time.monotonic() >= deadline:
                    raise FakeStateLockError(f"timed out waiting for fake lock {lock_path}") from None
                time.sleep(0.005)
        yield
    finally:
        if handle is not None:
            if acquired:
                try:
                    _unlock_file(handle)
                except OSError:
                    pass
            handle.close()


def state_path(home: str | Path | None = None) -> Path:
    return fake_home(home) / "state.json"


def events_path(home: str | Path | None = None) -> Path:
    return fake_home(home) / "events.jsonl"


def _default_state(home: Path) -> FakeState:
    sdk_root = env_value("AVDFLEET_FAKE_SDK_ROOT") or env_value("ANDROID_SDK_ROOT") or str(home / "sdk")
    avd_home = env_value("AVDFLEET_FAKE_AVD_HOME") or env_value("ANDROID_AVD_HOME") or str(home / "avd")
    return {
        "schema": 1,
        "sdk_root": sdk_root,
        "avd_home": avd_home,
        "installed": [],
        "licenses_accepted": [],
        "avds": {},
        "appium": {"running": False, "port": None, "pid": None, "sessions": []},
        "inputs": [],
        "screenshots": {},
        "commands": [],
        "snapshots": {},
        "tool_installs": [],
    }


def _retryable_oserror(exc: OSError) -> bool:
    """True for transient Windows sharing/lock errors while state is replaced."""
    if getattr(exc, "winerror", None) in {5, 32, 33}:  # ACCESS_DENIED, SHARING_VIOLATION, LOCK_VIOLATION
        return True
    return exc.errno in {11, 13, 16}  # EAGAIN, EACCES, EBUSY


def _read_state_file(home: Path) -> FakeState:
    path = home / "state.json"
    raw = ""
    for attempt in range(20):
        try:
            raw = path.read_text(encoding="utf-8")
            break
        except FileNotFoundError:
            return _default_state(home)
        except OSError as exc:
            if not _retryable_oserror(exc) or attempt == 19:
                raise
            time.sleep(0.025)
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return _default_state(home)
    if not isinstance(loaded, dict):
        return _default_state(home)
    return loaded


def _write_state_file(home: Path, state: FakeState) -> None:
    home.mkdir(parents=True, exist_ok=True)
    target = home / "state.json"
    tmp = home / f".state.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(state, indent=2, sort_keys=False, ensure_ascii=False), encoding="utf-8")
    for attempt in range(80):
        try:
            os.replace(tmp, target)
            return
        except OSError as exc:
            if not _retryable_oserror(exc) and not isinstance(exc, PermissionError):
                with contextlib.suppress(OSError):
                    tmp.unlink()
                raise
            if attempt == 79:
                with contextlib.suppress(OSError):
                    tmp.unlink()
                raise
            time.sleep(0.025)


def load_state(home: str | Path | None = None) -> FakeState:
    """Read the state file without locking (readers never block writers)."""
    return _read_state_file(fake_home(home))

def save_state(state: FakeState, home: str | Path | None = None) -> None:
    home_path = fake_home(home)
    with file_lock(home_path / "state.lock"):
        _write_state_file(home_path, state)


def mutate_state(mutator: Callable[[FakeState], Any], home: str | Path | None = None) -> Any:
    """Atomically read-modify-write ``state.json`` and return *mutator*'s result."""
    home_path = fake_home(home)
    with file_lock(home_path / "state.lock"):
        state = _read_state_file(home_path)
        result = mutator(state)
        _write_state_file(home_path, state)
        return result


def append_event(
    tool: str,
    argv: Iterable[str],
    *,
    cwd: str | Path | None = None,
    home: str | Path | None = None,
) -> dict[str, Any]:
    """Append an invocation record to ``events.jsonl`` (deterministic shape)."""
    home_path = fake_home(home)
    home_path.mkdir(parents=True, exist_ok=True)
    event = {
        "ts": _event_timestamp(),
        "tool": tool,
        "argv": [str(item) for item in argv],
        "cwd": str(cwd if cwd is not None else Path.cwd()),
    }
    line = json.dumps(event, ensure_ascii=False)
    with file_lock(home_path / "events.lock"), open(home_path / "events.jsonl", "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return event


def read_events(home: str | Path | None = None) -> list[dict[str, Any]]:
    path = fake_home(home) / "events.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def clear_events(home: str | Path | None = None) -> None:
    home_path = fake_home(home)
    with file_lock(home_path / "events.lock"):
        (home_path / "events.jsonl").write_text("", encoding="utf-8")


def record_command(tool: str, argv: Iterable[str], *, home: str | Path | None = None) -> None:
    """Mirror the argv audit log into ``state["commands"]`` (bounded)."""

    def _record(state: FakeState) -> None:
        commands = state.setdefault("commands", [])
        commands.append({"tool": tool, "argv": [str(item) for item in argv]})
        if len(commands) > 500:
            del commands[: len(commands) - 500]

    mutate_state(_record, home)


def _event_timestamp() -> str:
    from datetime import datetime

    return datetime.fromtimestamp(now(), tz=UTC).isoformat()


# ---------------------------------------------------------------------------
# Process registry (used to clean up long-lived fakes between tests)
# ---------------------------------------------------------------------------


def pids_dir(home: str | Path | None = None) -> Path:
    return fake_home(home) / "pids"


def register_pid(pid: int, tool: str, *, avd: str | None = None, home: str | Path | None = None) -> None:
    directory = pids_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"pid": int(pid), "tool": tool, "avd": avd}
    (directory / f"{int(pid)}.json").write_text(json.dumps(payload), encoding="utf-8")


def unregister_pid(pid: int, *, home: str | Path | None = None) -> None:
    path = pids_dir(home) / f"{int(pid)}.json"
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def registered_pids(home: str | Path | None = None) -> list[dict[str, Any]]:
    directory = pids_dir(home)
    try:
        files = sorted(directory.glob("*.json"))
    except FileNotFoundError:
        return []
    entries: list[dict[str, Any]] = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("pid"), int):
            payload["_path"] = str(path)
            entries.append(payload)
    return entries


def terminate_registered(
    *,
    tool: str | None = None,
    avd: str | None = None,
    home: str | Path | None = None,
    timeout: float = 3.0,
) -> list[int]:
    """Terminate fake long-lived processes matching the filters."""
    import signal

    killed: list[int] = []
    for entry in registered_pids(home):
        if tool is not None and entry.get("tool") != tool:
            continue
        if avd is not None and entry.get("avd") != avd:
            continue
        pid = int(entry["pid"])
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except OSError:
            pass
        with contextlib.suppress(FileNotFoundError):
            Path(str(entry["_path"])).unlink()
    if killed:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and _any_alive(killed):
            time.sleep(0.02)
    return killed


def process_alive(pid: int) -> bool:
    """Cross-platform liveness probe that never signals the process."""
    if os.name == "nt":  # pragma: no cover - Windows only
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # type: ignore[attr-defined]  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))  # type: ignore[attr-defined]
            return code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def parent_alive() -> bool:
    """True while the process that started this fake is still running."""
    parent = os.getppid()
    return parent > 1 and process_alive(parent)


def _any_alive(pids: Iterable[int]) -> bool:
    return any(process_alive(pid) for pid in pids)


# ---------------------------------------------------------------------------
# SDK catalogue (seeded per fakes-spec sections 3 and 6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Package:
    id: str
    version: str
    description: str
    location: str
    revision: str = ""

    @property
    def path(self) -> str:
        return self.location


_SYSTEM_IMAGE_SPECS: tuple[tuple[int, str, str], ...] = (
    (34, "google_apis_playstore", "x86_64"),
    (35, "google_apis_playstore", "x86_64"),
    (35, "google_apis", "x86_64"),
    (35, "aosp_atd", "x86_64"),
    (36, "google_apis_playstore", "x86_64"),
    (36, "google_apis", "x86_64"),
    (33, "google_apis", "x86_64"),
    (34, "google_apis_playstore", "arm64-v8a"),
    (35, "google_apis_playstore", "arm64-v8a"),
    (35, "google_apis", "arm64-v8a"),
    (35, "aosp_atd", "arm64-v8a"),
    (36, "google_apis_playstore", "arm64-v8a"),
    (36, "google_apis", "arm64-v8a"),
)

_TAG_DISPLAY = {
    "google_apis_playstore": "Google Play",
    "google_apis": "Google APIs",
    "aosp_atd": "AOSP ATD",
    "default": "Default",
}

_API_RELEASE = {33: "13", 34: "14", 35: "15", 36: "16"}


def _image_description(tag: str, abi: str) -> str:
    abi_label = "ARM 64 v8a" if abi == "arm64-v8a" else "Intel x86_64 Atom"
    if tag == "google_apis_playstore":
        return f"Google Play {abi_label} System Image"
    if tag == "google_apis":
        return f"Google APIs {abi_label} System Image"
    if tag == "aosp_atd":
        return f"AOSP ATD {abi_label} System Image"
    return f"Android {abi_label} System Image"


def package_location(package_id: str) -> str:
    parts = package_id.split(";")
    if not parts:
        return ""
    name = parts[0]
    if name in {"platform-tools", "emulator", "tools", "docs", "sources", "add-ons"}:
        return package_id.replace(";", "/")
    if name == "cmdline-tools":
        return f"cmdline-tools/{parts[1] if len(parts) > 1 else 'latest'}"
    if name == "build-tools":
        return f"build-tools/{parts[1] if len(parts) > 1 else '35.0.0'}"
    if name == "platforms":
        return f"platforms/{parts[1] if len(parts) > 1 else 'android-35'}"
    if name == "system-images":
        return "system-images/" + "/".join(parts[1:]) if len(parts) > 1 else "system-images"
    return "/".join(parts)


def _build_catalogue() -> dict[str, Package]:
    entries: dict[str, Package] = {
        "cmdline-tools;latest": Package(
            "cmdline-tools;latest", "19.0", "Android SDK Command-line Tools (latest)", "cmdline-tools/latest"
        ),
        "cmdline-tools;11.0": Package(
            "cmdline-tools;11.0", "11.0", "Android SDK Command-line Tools", "cmdline-tools/11.0"
        ),
        "platform-tools": Package("platform-tools", "35.0.2", "Android SDK Platform-Tools", "platform-tools"),
        "emulator": Package("emulator", "35.1.4", "Android Emulator", "emulator"),
        "build-tools;35.0.0": Package(
            "build-tools;35.0.0", "35.0.0", "Android SDK Build-Tools 35", "build-tools/35.0.0"
        ),
        "platforms;android-34": Package(
            "platforms;android-34", "3", "Android SDK Platform 34", "platforms/android-34"
        ),
        "platforms;android-35": Package(
            "platforms;android-35", "2", "Android SDK Platform 35", "platforms/android-35"
        ),
        "platforms;android-36": Package(
            "platforms;android-36", "1", "Android SDK Platform 36", "platforms/android-36"
        ),
    }
    for api, tag, abi in _SYSTEM_IMAGE_SPECS:
        package_id = f"system-images;android-{api};{tag};{abi}"
        entries[package_id] = Package(
            package_id,
            "11.0",
            _image_description(tag, abi),
            package_location(package_id),
        )
    return entries


CATALOGUE: dict[str, Package] = _build_catalogue()

IMAGE_METADATA: dict[str, dict[str, Any]] = {}
for _spec in _SYSTEM_IMAGE_SPECS:
    _api, _tag, _abi = _spec
    _package = f"system-images;android-{_api};{_tag};{_abi}"
    IMAGE_METADATA[_package] = {
        "api": _api,
        "tag": _tag,
        "abi": _abi,
        "kind": _tag if _tag in {"google_apis_playstore", "google_apis"} else "aosp",
        "play": _tag == "google_apis_playstore",
        "release": _API_RELEASE.get(_api, str(_api - 20)),
        "display": f"{_TAG_DISPLAY.get(_tag, _tag)} (API {_api}, {_abi})",
    }

DEVICE_PROFILES: dict[str, dict[str, Any]] = {
    "pixel_7": {"id": 7, "name": "Pixel 7", "oem": "Google", "resolution": "1080x2400", "density": 420, "ram_mb": 2048},
    "pixel_6": {"id": 6, "name": "Pixel 6", "oem": "Google", "resolution": "1080x2400", "density": 420, "ram_mb": 2048},
    "pixel_tablet": {
        "id": 10,
        "name": "Pixel Tablet",
        "oem": "Google",
        "resolution": "2560x1600",
        "density": 320,
        "ram_mb": 4096,
    },
    "Nexus 5X": {"id": 5, "name": "Nexus 5X", "oem": "LGE", "resolution": "1080x1920", "density": 420, "ram_mb": 2048},
    "pixel_9": {"id": 9, "name": "Pixel 9", "oem": "Google", "resolution": "1080x2424", "density": 420, "ram_mb": 4096},
}

DEFAULT_DEVICE = "pixel_7"

# Packages the fixture installs before every test (tools plus Play Store images
# for the fake host ABI so tests can create and boot AVDs immediately).
BASE_INSTALLED: tuple[str, ...] = (
    "cmdline-tools;latest",
    "platform-tools",
    "emulator",
    "build-tools;35.0.0",
    "platforms;android-35",
    "platforms;android-36",
)


def seeded_installed(abi: str | None = None) -> list[str]:
    resolved = abi or host_abi()
    return [
        *BASE_INSTALLED,
        f"system-images;android-35;google_apis_playstore;{resolved}",
        f"system-images;android-36;google_apis_playstore;{resolved}",
    ]


def canonical_device(name: str) -> str:
    for candidate in DEVICE_PROFILES:
        if candidate.lower() == name.strip().lower():
            return candidate
    normalized = name.strip().lower()
    for candidate in DEVICE_PROFILES:
        if candidate.lower().replace(" ", "_") == normalized.replace(" ", "_"):
            return candidate
    return ""


def parse_system_image(package_id: str) -> dict[str, Any]:
    parts = package_id.split(";")
    if len(parts) != 4 or parts[0] != "system-images":
        raise FakeToolsError(f"not a system image package: {package_id!r}")
    api_raw = parts[1].replace("android-", "")
    try:
        api = int(api_raw)
    except ValueError as exc:
        raise FakeToolsError(f"invalid API level in {package_id!r}") from exc
    return {"api": api, "tag": parts[2], "abi": parts[3]}


def image_metadata(package_id: str) -> dict[str, Any]:
    meta = IMAGE_METADATA.get(package_id)
    if meta is not None:
        return dict(meta)
    parsed = parse_system_image(package_id)
    tag = parsed["tag"]
    return {
        "api": parsed["api"],
        "tag": tag,
        "abi": parsed["abi"],
        "kind": tag if tag in {"google_apis_playstore", "google_apis"} else "aosp",
        "play": tag == "google_apis_playstore",
        "release": _API_RELEASE.get(parsed["api"], str(parsed["api"] - 20)),
        "display": f"{_TAG_DISPLAY.get(tag, tag)} (API {parsed['api']}, {parsed['abi']})",
    }


def catalogue_package_ids(*, include_catalogue: bool = True) -> list[str]:
    return sorted(CATALOGUE)


# ---------------------------------------------------------------------------
# Filesystem materialisation helpers
# ---------------------------------------------------------------------------


def sdk_package_dir(sdk_root: str | Path, package_id: str) -> Path:
    return Path(sdk_root) / package_location(package_id)


def _write_source_properties(directory: Path, package_id: str) -> None:
    package = CATALOGUE.get(package_id)
    revision = package.version if package else "1.0"
    lines = [
        f"Pkg.Desc={package.description if package else package_id}",
        f"Pkg.Revision={revision}",
        f"Pkg.Path={package_id}",
        "Pkg.SourceUrl=https://dl.google.com/android/repository/repository2-3.xml",
    ]
    if package_id.startswith("system-images;"):
        meta = image_metadata(package_id)
        lines.extend(
            [
                f"AndroidVersion.ApiLevel={meta['api']}",
                f"SystemImage.TagId={meta['tag']}",
                f"SystemImage.TagDisplay={_TAG_DISPLAY.get(meta['tag'], meta['tag'])}",
                f"SystemImage.Abi={meta['abi']}",
                "SystemImage.GpuSupport=true",
            ]
        )
    elif package_id.startswith("platforms;"):
        api = package_id.split("android-")[-1]
        lines.extend([f"AndroidVersion.ApiLevel={api}", "Layoutlib.Api=15", "Layoutlib.Revision=1"])
    (directory / "source.properties").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_image_files(directory: Path, package_id: str) -> None:
    meta = image_metadata(package_id)
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("system.img", "ramdisk.img", "userdata.img", "vendor.img", "cache.img"):
        (directory / name).write_bytes(b"AVDFLEET-FAKE-IMAGE\n")
    build_prop = [
        f"ro.build.version.sdk={meta['api']}",
        f"ro.build.version.release={meta['release']}",
        f"ro.product.cpu.abi={meta['abi']}",
        f"ro.product.cpu.abilist={meta['abi']},armeabi-v7a,armeabi",
        f"ro.build.tags={'google_apis_playstore' if meta['play'] else meta['tag']}",
        "ro.build.type=user",
        "ro.kernel.qemu=1",
        "ro.product.device=emu64x",
    ]
    (directory / "build.prop").write_text("\n".join(build_prop) + "\n", encoding="utf-8")
    (directory / "package.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<ns2:repository xmlns:ns2="http://schemas.android.com/repository/android/common/02"/>\n',
        encoding="utf-8",
    )


def _write_platform_files(directory: Path, package_id: str) -> None:
    api = package_id.split("android-")[-1]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "android.jar").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    (directory / "build.prop").write_text(
        f"ro.build.version.sdk={api}\nro.build.version.release={_API_RELEASE.get(int(api), api)}\n",
        encoding="utf-8",
    )
    (directory / "data").mkdir(exist_ok=True)


def materialize_package(sdk_root: str | Path, package_id: str) -> Path:
    """Create the on-disk SDK layout for *package_id* and return its directory."""
    directory = sdk_package_dir(sdk_root, package_id)
    directory.mkdir(parents=True, exist_ok=True)
    name = package_id.split(";")[0]
    if package_id.startswith("system-images;"):
        _write_image_files(directory, package_id)
    elif package_id.startswith("platforms;"):
        _write_platform_files(directory, package_id)
    elif name == "build-tools":
        directory.mkdir(parents=True, exist_ok=True)
        for tool in ("aapt", "aapt2", "apksigner", "zipalign"):
            (directory / tool).write_bytes(b"")
    elif name == "cmdline-tools":
        (directory / "bin").mkdir(parents=True, exist_ok=True)
    elif name == "platform-tools":
        (directory / "source.properties").write_text("Pkg.Revision=35.0.2\n", encoding="utf-8")
    elif name == "emulator":
        (directory / "lib64").mkdir(parents=True, exist_ok=True)
    _write_source_properties(directory, package_id)
    return directory


def avd_config(entry: Mapping[str, Any]) -> dict[str, str]:
    """Return the ``config.ini`` mapping for an AVD state entry."""
    package = str(entry.get("package") or "")
    meta = image_metadata(package) if package.startswith("system-images;") else {
        "api": entry.get("api", 35),
        "tag": "google_apis_playstore",
        "abi": entry.get("abi", "x86_64"),
        "play": True,
    }
    sysdir = "/".join(package.split(";")[1:]) if package.startswith("system-images;") else ""
    config = dict(entry.get("config") or {})
    config.setdefault("image.sysdir.1", f"{sysdir}/")
    config.setdefault("tag.id", meta["tag"])
    config.setdefault("tag.display", _TAG_DISPLAY.get(meta["tag"], meta["tag"]))
    config.setdefault("abi.type", meta["abi"])
    config.setdefault("hw.cpu.arch", "arm64" if meta["abi"] == "arm64-v8a" else "x86_64")
    config.setdefault("hw.device.name", entry.get("device") or DEFAULT_DEVICE)
    config.setdefault("hw.ramSize", str((DEVICE_PROFILES.get(str(entry.get("device"))) or {}).get("ram_mb", 2048)))
    config.setdefault("disk.dataPartition.size", "6G")
    config.setdefault("PlayStore.enabled", "true" if meta.get("play") else "false")
    config.setdefault("fastboot.forceColdBoot", "yes")
    config.setdefault("hw.lcd.width", "1080")
    config.setdefault("hw.lcd.height", "1920")
    config.setdefault("hw.lcd.density", "420")
    config.setdefault("vm.heapSize", "256")
    return config


def write_avd_files(avd_home: str | Path, entry: Mapping[str, Any], *, path: str | Path | None = None) -> Path:
    """Write ``<name>.ini`` and ``<name>.avd/config.ini`` for *entry*."""
    home = Path(avd_home)
    home.mkdir(parents=True, exist_ok=True)
    name = str(entry["name"])
    avd_dir = Path(path) if path is not None else home / f"{name}.avd"
    avd_dir.mkdir(parents=True, exist_ok=True)
    package = str(entry.get("package") or "")
    api = image_metadata(package)["api"] if package.startswith("system-images;") else entry.get("api", 35)
    ini = home / f"{name}.ini"
    ini.write_text(
        "\n".join(
            [
                "avd.ini.encoding=UTF-8",
                f"path={avd_dir}",
                f"path.rel=avd/{name}.avd",
                f"target=android-{api}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config_lines = [f"{key}={value}" for key, value in sorted(avd_config(entry).items())]
    (avd_dir / "config.ini").write_text("\n".join(config_lines) + "\n", encoding="utf-8")
    return avd_dir


def remove_avd_files(avd_home: str | Path, name: str) -> None:
    home = Path(avd_home)
    with contextlib.suppress(FileNotFoundError):
        (home / f"{name}.ini").unlink()
    shutil.rmtree(home / f"{name}.avd", ignore_errors=True)


def avd_dir_for(avd_home: str | Path, name: str) -> Path:
    return Path(avd_home) / f"{name}.avd"


def build_avd_entry(name: str, package: str, device: str = DEFAULT_DEVICE, **overrides: Any) -> dict[str, Any]:
    """Build a state entry for an AVD (no ports assigned unless overridden)."""
    meta = image_metadata(package) if package.startswith("system-images;") else {
        "api": 35,
        "abi": "x86_64",
        "tag": "google_apis_playstore",
        "play": True,
    }
    profile = DEVICE_PROFILES.get(device) or DEVICE_PROFILES[DEFAULT_DEVICE]
    entry: dict[str, Any] = {
        "name": name,
        "display_name": name,
        "package": package,
        "device": device,
        "api": meta["api"],
        "abi": meta["abi"],
        "state": "stopped",
        "pid": None,
        "console_port": None,
        "adb_port": None,
        "grpc_port": None,
        "boot_completed": False,
        "boot_complete_at": None,
        "started_at": None,
        "proxy": None,
        "snapshot": None,
        "oobe": "setup",
        "activity": SETUP_WIZARD_ACTIVITY,
        "config": {
            "hw.ramSize": str(profile.get("ram_mb", 2048)),
            "disk.dataPartition.size": "6G",
        },
        "forwards": [],
        "reverses": [],
    }
    entry.update(overrides)
    return entry


def next_console_port(
    state: Mapping[str, Any],
    requested: int | None = None,
    *,
    avoid: Iterable[int] = (),
) -> int:
    """Return a free even console port honouring AVDs already in *state*."""
    used: set[int] = set(avoid)
    for entry in (state.get("avds") or {}).values():
        port = entry.get("console_port")
        if isinstance(port, int):
            used.add(port)
    if requested is not None and requested % 2 == 0 and requested not in used and is_port_free(requested):
        return requested
    for port in range(CONSOLE_PORT_START, CONSOLE_PORT_END + 1, 2):
        if port in used:
            continue
        if is_port_free(port):
            return port
    return CONSOLE_PORT_START


def grpc_port_for(console_port: int) -> int:
    candidate = console_port + GRPC_PORT_OFFSET
    return candidate if candidate <= 65535 else console_port + 2000


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


# ---------------------------------------------------------------------------
# Seed state and oobe matrix
# ---------------------------------------------------------------------------


def seed_state(sdk_root: str | Path, avd_home: str | Path, abi: str | None = None) -> FakeState:
    root = Path(sdk_root)
    home = Path(avd_home)
    state = _default_state(home)
    state.update(
        {
            "schema": 1,
            "sdk_root": str(root),
            "avd_home": str(home),
            "installed": seeded_installed(abi),
            "licenses_accepted": [],
            "avds": {},
            "appium": {"running": False, "port": None, "pid": None, "sessions": []},
            "inputs": [],
            "screenshots": {},
            "commands": [],
            "snapshots": {},
            "tool_installs": [],
        }
    )
    return state


def default_oobe_matrix(abi: str | None = None) -> dict[str, Any]:
    """The seeded ``oobe_matrix.json`` contents (fakes-spec section 6)."""
    resolved = abi or host_abi()
    records: list[dict[str, Any]] = []
    for api, tag, image_abi in _SYSTEM_IMAGE_SPECS:
        package = f"system-images;android-{api};{tag};{image_abi}"
        record: dict[str, Any] = {
            "image_id": package,
            "api_level": api,
            "tag": tag,
            "abi": image_abi,
            "status": "unverified",
            "activity": "",
            "first_run_component": "",
            "screenshot_path": "",
            "boot_seconds": None,
            "reset_seconds": None,
            "checked_at": None,
            "checked_by": "fake-seed",
            "emulator_version": "35.1.4.0",
            "notes": "",
            "evidence": [],
        }
        if tag == "google_apis_playstore" and api in {35, 36}:
            record.update(
                {
                    "status": "verified",
                    "activity": SETUP_WIZARD_ACTIVITY,
                    "first_run_component": SETUP_WIZARD_ACTIVITY,
                    "boot_seconds": 12.5,
                    "reset_seconds": 4.0,
                    "checked_at": "2026-01-01T00:00:00+00:00",
                    "notes": "seeded fake verification",
                }
            )
        elif api == 33:
            record.update(
                {
                    "status": "failed",
                    "boot_seconds": 600.0,
                    "checked_at": "2026-01-01T00:00:00+00:00",
                    "notes": "seeded fake failure record: setup wizard never appeared",
                }
            )
        records.append(record)
    return {
        "version": "1",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "host_abi": resolved,
        "records": records,
    }


# ---------------------------------------------------------------------------
# Shims and tool wrappers
# ---------------------------------------------------------------------------


def _python_install_root() -> Path:
    base = Path(getattr(sys, "base_prefix", sys.prefix))
    candidate = base / "python.exe"
    if sys.platform == "win32" and candidate.exists():
        return base
    return Path(sys.prefix)


def _python_layout() -> tuple[Path, Path, Path]:
    """Return (prefix, Lib directory, DLLs directory) of the real interpreter."""
    prefix = _python_install_root()
    lib = prefix / "Lib"
    if not lib.is_dir():
        lib = prefix
    dlls = prefix / "DLLs"
    return prefix, lib, dlls


def _windows_dll_sources() -> dict[str, Path]:
    prefix = _python_install_root()
    sources: dict[str, Path] = {}
    for candidate in sorted(prefix.glob("python3*.dll")):
        sources[candidate.name] = candidate
    for name in ("python3.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        candidate = prefix / name
        if candidate.exists():
            sources.setdefault(name, candidate)
    return sources


def _windows_pth_name() -> str:
    match = re.search(r"python3(\d+)\.dll", " ".join(_windows_dll_sources()))
    if match:
        return f"python3{match.group(1)}._pth"
    return f"python{sys.version_info[0]}{sys.version_info[1]}._pth"


def _link_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)


def _write_windows_pth(directory: Path, bin_dir: Path) -> None:
    """Write a ``pythonXY._pth`` giving copied python.exe shims a usable stdlib."""
    prefix, lib, dlls = _python_layout()
    lines = [str(lib), str(dlls), str(bin_dir / "_dispatch")]
    if not lib.is_dir():
        lines = [str(prefix), str(bin_dir / "_dispatch")]
    lines.append("import site")
    (directory / _windows_pth_name()).write_text("\n".join(lines) + "\n", encoding="utf-8")


_DISPATCH_TEMPLATE = '''# Auto-generated by tests/fakes/runtime.py. Never edit by hand.
"""Dispatch copied python.exe shims to the fake CLI (Windows subprocess support)."""
import os
import sys
import traceback

_FAKE_CLI = {fake_cli!r}
_TOOLS = {tools!r}


def _dispatch():
    # ``sys.argv`` is not populated yet when sitecustomize runs, so identify the
    # tool from the executable path and take arguments from ``sys.orig_argv``.
    name = os.path.basename(sys.executable).lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if name not in _TOOLS:
        return
    args = list(getattr(sys, "orig_argv", [sys.executable])[1:])
    sys.path.insert(0, os.path.dirname(_FAKE_CLI))
    code = 1
    try:
        import fake_cli

        code = fake_cli.main([name] + args)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException:
        traceback.print_exc()
        code = 1
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(int(code or 0))


_dispatch()
'''


def write_dispatch_hook(bin_dir: str | Path, fake_cli: str | Path) -> Path:
    directory = Path(bin_dir) / "_dispatch"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "sitecustomize.py"
    target.write_text(
        _DISPATCH_TEMPLATE.format(fake_cli=str(Path(fake_cli).resolve()), tools=tuple(sorted(TOOLS))),
        encoding="utf-8",
    )
    return target


# -- Windows launcher exes (pip/distlib console-script stubs) ----------------
#
# A tiny ``python.exe`` copy cannot be used as a shim: CPython consumes
# ``--version`` / ``-h`` before any Python-level hook runs.  The pip launcher
# stub instead embeds the interpreter and runs the ``__main__.py`` appended in a
# ZIP, passing user arguments through untouched, so every argument form reaches
# the fake CLI.

_LAUNCHER_MAIN = '''# Auto-generated by tests/fakes/runtime.py. Never edit by hand.
"""Dispatch this Windows launcher shim to the fake CLI."""
import os
import sys

_FAKE_CLI = {fake_cli!r}
_TOOL = {tool!r}


def _run():
    sys.path.insert(0, os.path.dirname(_FAKE_CLI))
    import fake_cli

    return fake_cli.main([_TOOL] + sys.argv[1:])


if __name__ == "__main__":
    code = 0
    try:
        code = _run()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException:
        import traceback

        traceback.print_exc()
        code = 1
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    sys.exit(code if isinstance(code, int) else 0)
'''


def _zip_payload_start(data: bytes) -> int | None:
    """Offset at which an appended ZIP archive starts, or ``None``."""
    position = data.rfind(b"PK\x05\x06")
    if position < 0 or position + 22 > len(data):
        return None
    _disk, _cd_disk, _n_disk, _n_total, cd_size, cd_offset, comment_len = struct.unpack_from(
        "<HHHHIIH", data, position + 4
    )
    if position + 22 + comment_len != len(data):
        return None
    start = position - cd_size - cd_offset
    if start <= 0 or start >= position:
        return None
    return start


def _launcher_arch_candidates() -> tuple[str, ...]:
    """distlib launcher images that can host this interpreter, best first."""
    machine = (os.environ.get("PROCESSOR_ARCHITECTURE") or platform.machine() or "").lower()
    if struct.calcsize("P") < 8:
        return ("t32.exe",)
    candidates = ["t64-arm.exe", "t64.exe", "t32.exe"] if "arm" in machine else ["t64.exe", "t32.exe"]
    return tuple(candidates)


def _launcher_stub_from_exe(data: bytes) -> bytes | None:
    """Return the bare launcher image of an installed console-script exe.

    distlib writes ``<launcher image><#!interpreter line><zip payload>``, so the
    image is the prefix in front of the ZIP with that interpreter line removed.
    """
    if not data.startswith(b"MZ"):
        return None
    start = _zip_payload_start(data)
    if start is None or start < 1024:
        return None
    prefix = data[:start]
    index = prefix.rfind(b"#!")
    if index > 0:
        line = prefix[index:]
        if line.endswith(b"\n") and b"\x00" not in line and b"\n" not in line[:-1]:
            return prefix[:index]
    return prefix


def _launcher_stub_from_directory(directory: Path) -> bytes | None:
    for name in _launcher_arch_candidates():
        try:
            data = (directory / name).read_bytes()
        except OSError:
            continue
        if data.startswith(b"MZ") and len(data) > 20_000:
            return data
    return None


def _installed_launcher_stub() -> bytes | None:
    """Find a distlib launcher among this environment's console scripts."""
    import sysconfig

    directories: list[Path] = []
    with contextlib.suppress(Exception):
        directories.append(Path(sysconfig.get_path("scripts")))
    directories.extend([Path(sys.executable).parent / "Scripts", Path(sys.prefix) / "Scripts"])
    seen: set[Path] = set()
    for directory in directories:
        resolved = directory.expanduser()
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        for candidate in sorted(resolved.glob("*.exe")):
            try:
                if candidate.stat().st_size < 20_000:
                    continue
                data = candidate.read_bytes()
            except OSError:
                continue
            stub = _launcher_stub_from_exe(data)
            if stub is not None:
                return stub
    return None


def _vendored_launcher_stub() -> bytes | None:
    """Read a distlib launcher shipped with pip, installed or via ``ensurepip``."""
    import zipfile

    try:
        import pip._vendor.distlib as _distlib  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover - depends on the environment
        # Resolved as a module when pip is installed (CI, system Pythons) and as
        # an unresolved Any inside pip-less virtualenvs (uv), hence the ignore.
        _distlib = None  # type: ignore[assignment]
    if _distlib is not None:
        stub = _launcher_stub_from_directory(Path(_distlib.__file__).parent)
        if stub is not None:
            return stub
    # ``ensurepip`` carries pip as a wheel inside the interpreter even when pip
    # itself is not installed (``uv``-created virtualenvs, for instance).
    try:
        import ensurepip
    except Exception:  # pragma: no cover - stripped interpreters
        return None
    try:
        wheels = sorted((Path(ensurepip.__file__).parent / "_bundled").glob("pip-*.whl"))
    except OSError:  # pragma: no cover - defensive
        return None
    for wheel in wheels:
        try:
            with zipfile.ZipFile(wheel) as archive:
                for name in _launcher_arch_candidates():
                    try:
                        data = archive.read(f"pip/_vendor/distlib/{name}")
                    except KeyError:
                        continue
                    if data.startswith(b"MZ"):
                        return data
        except (OSError, zipfile.BadZipFile):  # pragma: no cover - corrupt wheel
            continue
    return None


def find_launcher_stub() -> bytes | None:
    """Locate a bare pip/distlib console-script launcher image for this host.

    The returned bytes are the native launcher only: :func:`launcher_payload`
    appends the ``#!`` interpreter line and the ZIP payload the launcher runs.
    Discovery cannot rely on installed console scripts alone - ``uv`` (and
    several other installers) create them with a private trampoline format that
    is not distlib compatible, so the launcher shipped with pip is tried first.
    """
    return _vendored_launcher_stub() or _installed_launcher_stub()


def _enquote_executable(path: str) -> str:
    """Quote a shebang interpreter path the way distlib does."""
    if " " in path and not path.startswith('"'):
        return f'"{path}"'
    return path


def launcher_payload(
    stub: bytes,
    tool: str,
    fake_cli: str | Path,
    *,
    python_exe: str | Path | None = None,
) -> bytes:
    """Build a ``<tool>.exe`` payload: launcher image + shebang + dispatch ZIP.

    The distlib launcher needs the ``#!`` line to find the interpreter to run;
    without it the executable exits silently with status 1.
    """
    import zipfile

    script = _LAUNCHER_MAIN.format(fake_cli=str(Path(fake_cli).resolve()), tool=tool)
    interpreter = _enquote_executable(str(python_exe or sys.executable))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("__main__.py", script)
    return stub + b"#!" + os.fsencode(interpreter) + b"\n" + buffer.getvalue()


def write_launcher_exe(
    path: str | Path,
    tool: str,
    fake_cli: str | Path,
    stub: bytes,
    *,
    python_exe: str | Path | None = None,
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(launcher_payload(stub, tool, fake_cli, python_exe=python_exe))
    return target


def link_tool_wrapper(
    destination_dir: str | Path,
    tool: str,
    bin_dir: str | Path,
    *,
    host_os: str | None = None,
) -> Path:
    """Create an executable ``<destination_dir>/<tool>`` wrapper for the fake tool.

    POSIX uses a tiny ``/bin/sh`` exec script.  Windows hard-links (or copies)
    the self-contained launcher exe; the legacy copied-``python.exe`` fallback
    additionally needs the runtime DLLs and a ``pythonXY._pth`` next to it.
    """
    destination = Path(destination_dir)
    source_bin = Path(bin_dir)
    destination.mkdir(parents=True, exist_ok=True)
    if (host_os or real_host_os()) == "windows":
        source = source_bin / f"{tool}.exe"
        target = destination / f"{tool}.exe"
        _link_or_copy(source, target)
        if (source_bin / "_dispatch").exists():  # legacy copied-python.exe mode
            for name, dll_source in _windows_dll_sources().items():
                _link_or_copy(dll_source, destination / name)
            _write_windows_pth(destination, source_bin)
        return target
    target = destination / tool
    target.write_text(f'#!/bin/sh\nexec "{source_bin / tool}" "$@"\n', encoding="utf-8")
    target.chmod(0o755)
    return target


def write_sdk_tool_wrappers(sdk_root: str | Path, bin_dir: str | Path, host_os: str | None = None) -> None:
    """Materialise the standard SDK tool paths used by backend implementations."""
    root = Path(sdk_root)
    windows = (host_os or real_host_os()) == "windows"
    layouts = {
        "platform-tools": ("adb",),
        "emulator": ("emulator",),
        "cmdline-tools/latest/bin": ("sdkmanager", "avdmanager", "android"),
        "cmdline-tools/11.0/bin": ("sdkmanager", "avdmanager", "android"),
        "jdk/bin": ("java",),
    }
    for relative, tools in layouts.items():
        directory = root / relative
        directory.mkdir(parents=True, exist_ok=True)
        for tool in tools:
            try:
                link_tool_wrapper(directory, tool, bin_dir, host_os="windows" if windows else "linux")
            except (OSError, FileNotFoundError):
                continue


def write_jdk_files(sdk_root: str | Path) -> None:
    jdk = Path(sdk_root) / "jdk"
    jdk.mkdir(parents=True, exist_ok=True)
    (jdk / "release").write_text(
        'JAVA_VERSION="21.0.6"\nIMPLEMENTOR="Fake JDK"\n',
        encoding="utf-8",
    )
    (jdk / "bin").mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# PNG generation (screencap)
# ---------------------------------------------------------------------------


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def png_bytes(width: int = 1080, height: int = 1920, colour: tuple[int, int, int] = (32, 36, 40)) -> bytes:
    """Return a valid solid-colour PNG of the requested size."""
    width = max(1, int(width))
    height = max(1, int(height))
    row = b"\x00" + bytes(colour) * width
    raw = row * height
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(raw, 1)) + _png_chunk(b"IEND", b"")


# ---------------------------------------------------------------------------
# Loopback listener helper (proxy / token egress verification)
# ---------------------------------------------------------------------------


class FakeLoopbackListener:
    """A loopback HTTP listener recording requests, for egress-verification tests."""

    def __init__(self, token: str = "fake-token", body: str | None = None) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.token = token
        self.hits: list[dict[str, Any]] = []
        self._body = body
        listener = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _respond(self) -> None:  # noqa: D401 - handler
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                listener.hits.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {key: value for key, value in self.headers.items()},
                        "body": body.decode("utf-8", "replace"),
                    }
                )
                payload = listener._body
                if payload is None:
                    payload = json.dumps(
                        {"ok": True, "token": listener.token, "path": self.path}
                    )
                encoded = payload.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                self._respond()

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                self._respond()

            def do_CONNECT(self) -> None:  # noqa: N802 - http.server API
                self._respond()

            def log_message(self, *args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, name=f"fake-listener-{self.port}", daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def token_url(self, path: str | None = None) -> str:
        return f"{self.url}{self.token}{('' if not path else '/' + path.lstrip('/'))}"

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._server.shutdown()
        with contextlib.suppress(Exception):
            self._server.server_close()


# ---------------------------------------------------------------------------
# FakeTools
# ---------------------------------------------------------------------------


@dataclass
class FakeToolPaths:
    root: Path
    bin: Path
    home: Path
    sdk_root: Path
    avd_home: Path


@dataclass
class FakeTools:
    """Session-scoped factory owning shims, seed state and helpers.

    The pytest fixture builds it once per test session; ``reset()`` restores the
    canonical seed so tests can run in any order.
    """

    root: Path
    host_os: str = field(default_factory=detected_host_os)
    arch: str = field(default_factory=detected_arch)
    accel: str = field(default_factory=accel)
    boot_seconds: float = field(default_factory=boot_seconds)
    python: Path = field(default_factory=lambda: Path(sys.executable))
    fake_cli: Path = field(default_factory=lambda: Path(__file__).with_name("fake_cli.py"))
    paths: FakeToolPaths = field(init=False)
    launcher_mode: bool = field(default=False, init=False)
    _launcher_stub: bytes | None = field(default=None, init=False)
    _listeners: list[FakeLoopbackListener] = field(default_factory=list, init=False)
    _closed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser().resolve()
        self.root = root
        self.paths = FakeToolPaths(
            root=root,
            bin=root / "bin",
            home=root / "home",
            sdk_root=root / "sdk",
            avd_home=root / "avd",
        )
        self.paths.bin.mkdir(parents=True, exist_ok=True)
        self._build_shims()
        self.reset()

    # -- construction ------------------------------------------------------
    @classmethod
    def create(
        cls,
        root: str | Path,
        *,
        host_os: str | None = None,
        arch: str | None = None,
        accel: str | None = None,
        boot_seconds: float | None = None,
    ) -> FakeTools:
        return cls(
            root=Path(root),
            host_os=host_os or detected_host_os(),
            arch=arch or detected_arch(),
            accel=accel or globals()["accel"](),
            boot_seconds=DEFAULT_BOOT_SECONDS if boot_seconds is None else float(boot_seconds),
        )

    # -- convenient attributes --------------------------------------------
    @property
    def path(self) -> Path:
        return self.paths.bin

    @property
    def bin_dir(self) -> Path:
        return self.paths.bin

    @property
    def home(self) -> Path:
        return self.paths.home

    @property
    def sdk_root(self) -> Path:
        return self.paths.sdk_root

    @property
    def avd_home(self) -> Path:
        return self.paths.avd_home

    @property
    def state(self) -> Path:
        return self.paths.home / "state.json"

    @property
    def events(self) -> Path:
        return self.paths.home / "events.jsonl"

    @property
    def oobe_matrix_path(self) -> Path:
        return self.paths.home / "oobe_matrix.json"

    @property
    def console_token_path(self) -> Path:
        return self.paths.home / ".emulator_console_auth_token"

    @property
    def grpc_dir(self) -> Path:
        return self.paths.home / "grpc"

    @property
    def host_abi(self) -> str:
        return "arm64-v8a" if self.arch == "arm64" else "x86_64"

    @property
    def env(self) -> dict[str, str]:
        base_path = os.environ.get("PATH", "")
        path_entries = [str(self.paths.bin)]
        if base_path:
            path_entries.append(base_path)
        return {
            "PATH": os.pathsep.join(path_entries),
            "ANDROID_SDK_ROOT": str(self.paths.sdk_root),
            "ANDROID_HOME": str(self.paths.sdk_root),
            "ANDROID_AVD_HOME": str(self.paths.avd_home),
            "ANDROID_USER_HOME": str(self.paths.home / "android"),
            "JAVA_HOME": str(self.paths.sdk_root / "jdk"),
            "AVDFLEET_FAKE_HOME": str(self.paths.home),
            "AVDFLEET_FAKE_SDK_ROOT": str(self.paths.sdk_root),
            "AVDFLEET_FAKE_AVD_HOME": str(self.paths.avd_home),
            "AVDFLEET_FAKE_BIN": str(self.paths.bin),
            "AVDFLEET_FAKE_PYTHON": str(self.python),
            "AVDFLEET_FAKE_BOOT_SECONDS": str(self.boot_seconds),
            "AVDFLEET_FAKE_ACCEL": self.accel,
            "AVDFLEET_FAKE_HOST_OS": self.host_os,
            "AVDFLEET_FAKE_ARCH": self.arch,
            "AVDFLEET_FAKE_APPIUM_PORT": str(DEFAULT_APPIUM_PORT),
            "AVDFLEET_FAKE_OOBE_MATRIX": str(self.oobe_matrix_path),
        }

    def shim(self, tool: str) -> Path:
        suffix = ".exe" if real_host_os() == "windows" else ""
        return self.paths.bin / f"{tool}{suffix}"

    # -- shim creation -----------------------------------------------------
    def _build_shims(self) -> None:
        if real_host_os() == "windows":
            stub = find_launcher_stub()
            self._launcher_stub = stub
            self.launcher_mode = stub is not None
            for tool in TOOLS:
                self._write_windows_cmd_shim(tool)
                if stub is not None:
                    write_launcher_exe(
                        self.paths.bin / f"{tool}.exe", tool, self.fake_cli, stub, python_exe=self.python
                    )
                else:  # pragma: no cover - only on Python installs without launchers
                    self._write_legacy_windows_exe(tool)
            if stub is None:  # pragma: no cover - legacy fallback path
                write_dispatch_hook(self.paths.bin, self.fake_cli)
                for name, source in _windows_dll_sources().items():
                    _link_or_copy(source, self.paths.bin / name)
                _write_windows_pth(self.paths.bin, self.paths.bin)
        else:
            for tool in TOOLS:
                self._write_posix_shim(tool)

    def _write_windows_cmd_shim(self, tool: str) -> None:
        # Exact Windows shim from fakes-spec section 1 (used when cmd.exe runs it).
        command = f'@"{self.python}" "{self.fake_cli}" {tool} %*\r\n'
        (self.paths.bin / f"{tool}.cmd").write_text(command, encoding="utf-8")

    def _write_legacy_windows_exe(self, tool: str) -> None:  # pragma: no cover - fallback
        exe_source = Path(sys.executable)
        base_exe = _python_install_root() / "python.exe"
        if base_exe.exists():
            exe_source = base_exe
        _link_or_copy(exe_source, self.paths.bin / f"{tool}.exe")

    def _write_posix_shim(self, tool: str) -> None:
        script = f'#!/bin/sh\nexec "{self.python}" "{self.fake_cli}" {tool} "$@"\n'
        target = self.paths.bin / tool
        target.write_text(script, encoding="utf-8")
        target.chmod(0o755)

    # -- state -------------------------------------------------------------
    def reset(self) -> FakeState:
        """Restore the canonical seeded state, files and argv log."""
        self.cleanup_processes()
        for directory in (self.paths.home, self.paths.sdk_root, self.paths.avd_home):
            if directory.exists():
                shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(parents=True, exist_ok=True)
        (self.paths.home / "pids").mkdir(exist_ok=True)
        (self.paths.home / "grpc").mkdir(exist_ok=True)
        (self.paths.home / "android").mkdir(exist_ok=True)
        state = seed_state(self.paths.sdk_root, self.paths.avd_home, self.host_abi)
        state["bin_dir"] = str(self.paths.bin)
        state["host_os"] = self.host_os
        state["arch"] = self.arch
        save_state(state, self.paths.home)
        self.console_token_path.write_text(CONSOLE_AUTH_TOKEN, encoding="utf-8")
        self.oobe_matrix_path.write_text(
            json.dumps(default_oobe_matrix(self.host_abi), indent=2) + "\n", encoding="utf-8"
        )
        (self.paths.sdk_root / "oobe_matrix.json").write_text(
            json.dumps(default_oobe_matrix(self.host_abi), indent=2) + "\n", encoding="utf-8"
        )
        write_jdk_files(self.paths.sdk_root)
        for package in state["installed"]:
            materialize_package(self.paths.sdk_root, package)
        write_sdk_tool_wrappers(self.paths.sdk_root, self.paths.bin)
        os.environ.pop("AVDFLEET_FAKE_SCRIPT_MAP", None)
        return state

    def read_state(self) -> FakeState:
        return load_state(self.paths.home)

    def update_state(self, updates: Mapping[str, Any]) -> FakeState:
        def _mutate(state: FakeState) -> FakeState:
            state.update(updates)
            return state

        return mutate_state(_mutate, self.paths.home)

    def seed_avds(self) -> list[str]:
        """Seed ``avd-01..03`` (one stopped, one running, one booting)."""
        state = self.read_state()
        base_package = f"system-images;android-35;google_apis_playstore;{self.host_abi}"
        if base_package not in state.get("installed", []):
            self.install_image(base_package)
            state = self.read_state()
        now_ts = now()
        entries = {
            "avd-01": build_avd_entry("avd-01", base_package, "pixel_7", state="stopped"),
            "avd-02": build_avd_entry(
                "avd-02",
                base_package,
                "pixel_6",
                state="running",
                pid=424202,
                console_port=5554,
                adb_port=5555,
                grpc_port=8554,
                boot_completed=True,
                boot_complete_at=now_ts - 5.0,
                started_at=now_ts - 10.0,
            ),
            "avd-03": build_avd_entry(
                "avd-03",
                base_package,
                "pixel_7",
                state="booting",
                pid=424203,
                console_port=5556,
                adb_port=5557,
                grpc_port=8556,
                boot_completed=False,
                boot_complete_at=now_ts + 3600.0,
                started_at=now_ts,
            ),
        }

        def _mutate(current: FakeState) -> list[str]:
            avds = current.setdefault("avds", {})
            for name, entry in entries.items():
                avds[name] = entry
            return sorted(entries)

        names = mutate_state(_mutate, self.paths.home)
        for entry in entries.values():
            write_avd_files(self.paths.avd_home, entry)
        return names

    def install_image(self, package: str, *, install: bool = True) -> dict[str, Any]:
        """Install (or just materialise) a system image package in the fake SDK."""
        meta = image_metadata(package)

        def _mutate(state: FakeState) -> FakeState:
            if install and package not in state.setdefault("installed", []):
                state["installed"].append(package)
                state["installed"].sort()
            return state

        mutate_state(_mutate, self.paths.home)
        materialize_package(self.paths.sdk_root, package)
        return meta

    # -- helpers -----------------------------------------------------------
    def write_script(self, mapping: Mapping[str, Any] | str) -> str:
        raw = mapping if isinstance(mapping, str) else json.dumps(mapping)
        os.environ["AVDFLEET_FAKE_SCRIPT_MAP"] = raw
        return raw

    def clear_script(self) -> None:
        os.environ.pop("AVDFLEET_FAKE_SCRIPT_MAP", None)

    def add_listener(self, *, token: str = "fake-token", body: str | None = None) -> FakeLoopbackListener:
        listener = FakeLoopbackListener(token=token, body=body)
        self._listeners.append(listener)
        return listener

    def commands(self, tool: str | None = None) -> list[dict[str, Any]]:
        events = read_events(self.paths.home)
        if tool is not None:
            events = [event for event in events if event.get("tool") == tool]
        return events

    def cleanup_processes(self) -> list[int]:
        """Terminate registered fakes and reconcile state left by hard kills."""
        killed = terminate_registered(home=self.paths.home)
        self._reconcile_stale_state()
        return killed

    def _reconcile_stale_state(self) -> None:
        """Mark AVDs/Appium stopped when their recorded pid is already gone."""

        def _mutator(state: FakeState) -> None:
            appium = state.get("appium")
            if isinstance(appium, dict):
                pid = appium.get("pid")
                if isinstance(pid, int) and pid != os.getpid() and not process_alive(pid):
                    appium.update({"running": False, "port": None, "pid": None, "sessions": []})
            for entry in (state.get("avds") or {}).values():
                if not isinstance(entry, dict):
                    continue
                pid = entry.get("pid")
                if not isinstance(pid, int) or entry.get("state") not in {"booting", "running"}:
                    continue
                if pid != os.getpid() and not process_alive(pid):
                    entry.update(
                        {
                            "state": "stopped",
                            "pid": None,
                            "boot_completed": False,
                            "boot_complete_at": None,
                            "started_at": None,
                        }
                    )

        with contextlib.suppress(Exception):
            mutate_state(_mutator, self.paths.home)

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.cleanup_processes()
        for listener in self._listeners:
            listener.close()
        self._listeners.clear()

    def __enter__(self) -> FakeTools:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.cleanup()


# ---------------------------------------------------------------------------
# Public helpers used by the fixtures
# ---------------------------------------------------------------------------


def build_fake_tools(root: str | Path, **kwargs: Any) -> FakeTools:
    """Create a :class:`FakeTools` instance (used by the session fixture)."""
    return FakeTools.create(root, **kwargs)


__all__ = [
    "ANDROID_PREVIEW_LICENSE_HASH",
    "ANDROID_SDK_LICENSE_HASH",
    "BASE_INSTALLED",
    "CATALOGUE",
    "CONSOLE_AUTH_TOKEN",
    "CONSOLE_PORT_END",
    "CONSOLE_PORT_START",
    "DEFAULT_APPIUM_PORT",
    "DEFAULT_BOOT_SECONDS",
    "DEFAULT_DEVICE",
    "DEVICE_PROFILES",
    "FakeLoopbackListener",
    "FakeState",
    "FakeStateLockError",
    "FakeTools",
    "FakeToolsError",
    "GRPC_TOKEN",
    "IMAGE_METADATA",
    "LAUNCHER_ACTIVITY",
    "Package",
    "SETUP_WIZARD_ACTIVITY",
    "TOOLS",
    "accel",
    "append_event",
    "appium_default_port",
    "avd_config",
    "avd_dir_for",
    "boot_seconds",
    "build_avd_entry",
    "build_fake_tools",
    "canonical_device",
    "catalogue_package_ids",
    "clear_events",
    "default_oobe_matrix",
    "detected_arch",
    "detected_host_os",
    "env_value",
    "events_path",
    "fail_set",
    "fake_home",
    "file_lock",
    "find_launcher_stub",
    "grpc_port_for",
    "host_abi",
    "host_os_override",
    "image_metadata",
    "is_port_free",
    "launcher_payload",
    "link_tool_wrapper",
    "load_state",
    "materialize_package",
    "mutate_state",
    "next_console_port",
    "now",
    "package_location",
    "parse_system_image",
    "png_bytes",
    "process_alive",
    "parent_alive",
    "read_events",
    "real_arch",
    "real_host_os",
    "record_command",
    "register_pid",
    "registered_pids",
    "remove_avd_files",
    "save_state",
    "script_map",
    "seed_state",
    "seeded_installed",
    "should_fail",
    "state_path",
    "terminate_registered",
    "unregister_pid",
    "write_avd_files",
    "write_dispatch_hook",
    "write_jdk_files",
    "write_launcher_exe",
    "write_sdk_tool_wrappers",
]
