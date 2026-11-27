"""Single executable fake for every host tool AVD Fleet talks to.

The file is dispatched either on its first argument (``fake_cli.py adb devices``,
which is how the POSIX shims and Windows ``.cmd`` shims call it) or on the
basename of ``sys.argv[0]`` (the copied-python-exe shims on Windows).

Tools implemented (fakes-spec section 4): ``adb``, ``emulator``, ``sdkmanager``,
``avdmanager``, ``android``, ``appium``, ``node``, ``npm``, ``npx``, ``uv``,
``java``, ``scrcpy`` and ``python``.

All state lives below ``$AVDFLEET_FAKE_HOME``; every invocation is appended to
``events.jsonl``.  Exit codes: 0 success, 1 failure, 2 usage error.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import shlex
import signal
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:  # imported as ``tests.fakes.fake_cli`` (pytest) ----
    from . import runtime as fake_runtime
except ImportError:  # executed as a plain script by the process-boundary shims
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import runtime as fake_runtime

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

TOOL_NAMES: tuple[str, ...] = fake_runtime.TOOLS

SEEDED_PACKAGES: tuple[str, ...] = (
    "com.android.settings",
    "com.android.launcher3",
    "com.google.android.apps.nexuslauncher",
    "com.example.test",
)

LOGCAT_LINES: tuple[str, ...] = (
    "--------- beginning of main",
    "I/ActivityManager( 1234): Start proc 5678:com.android.settings/u0a12 for pre-top-activity",
    "W/System  ( 1234): Fake warning line from the avdfleet logcat stream",
    "I/ActivityManager( 1234): Displayed com.android.settings/.Settings: +120ms",
    "I/chatty  ( 1234): uid=1000(system) FakeLogcat identical 1 line",
)

ADB_USAGE = "Android Debug Bridge version 1.0.41"
EMULATOR_USAGE = (
    "Usage: emulator [options] [-avd <name>]\n"
    "\n"
    "Options:\n"
    "    -avd <name>            Use a given AVD\n"
    "    -port <port>           Use the given console port\n"
    "    -no-window             Disable graphical window display\n"
    "    -gpu <mode>            Select GPU emulation mode\n"
    "    -http-proxy <url>      Use the given proxy for guest traffic\n"
    "    -wipe-data             Reset the user-data partition\n"
    "    -snapshot <name>       Load a snapshot on boot\n"
    "    -no-snapshot           Do not load or save snapshots\n"
    "    -grpc <port>           Use the given gRPC control port\n"
    "    -list-avds             List available AVDs\n"
    "    -version               Print the emulator version\n"
)
SDKMANAGER_USAGE = "Usage: sdkmanager [--list] [--install <package>...] [--uninstall <package>...]"
AVDMANAGER_USAGE = (
    "Usage: avdmanager list avd|device|target | create avd -n <name> -k <package> | "
    "delete avd -n <name> | move avd -n <name> -p <path>"
)
ANDROID_USAGE = "Usage: android <sdk|avd|emulator> <subcommand> [options]"


class UsageError(Exception):
    """Exit code 2."""


class ToolFailure(Exception):
    """Exit code 1."""


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def out(text: str = "") -> None:
    sys.stdout.write(text)
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def err(text: str = "") -> None:
    sys.stderr.write(text)
    if text and not text.endswith("\n"):
        sys.stderr.write("\n")
    sys.stderr.flush()


def out_bytes(payload: bytes) -> None:
    sys.stdout.flush()
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def _watch_parent(stop_event: threading.Event) -> None:
    """Stop a long-lived fake when the process that launched it disappears.

    Windows launch shims start the fake through a small launcher executable, so
    ``terminate()`` on the launcher handle does not deliver a signal to the fake
    itself; watching the parent keeps ``stop()`` semantics correct there.
    """
    parent = os.getppid()
    while not stop_event.wait(0.1):
        if parent <= 1 or not fake_runtime.parent_alive():
            stop_event.set()
            return


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _state() -> dict[str, Any]:
    return fake_runtime.load_state()


def _mutate(mutator: Any) -> Any:
    return fake_runtime.mutate_state(mutator)


def _home() -> Path:
    return fake_runtime.fake_home()


def _avd_entries(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    avds = state.get("avds")
    if not isinstance(avds, dict):
        return {}
    return {str(name): entry for name, entry in avds.items() if isinstance(entry, dict)}


def _active_avds(state: dict[str, Any]) -> list[dict[str, Any]]:
    entries = [entry for entry in _avd_entries(state).values() if entry.get("state") in {"booting", "running"}]
    return sorted(entries, key=lambda entry: int(entry.get("console_port") or 0))


def _find_avd(state: dict[str, Any], serial_or_name: str) -> dict[str, Any] | None:
    token = serial_or_name.strip()
    if not token:
        return None
    for entry in _avd_entries(state).values():
        console = entry.get("console_port")
        if token == f"emulator-{console}" or token == str(console):
            return entry
        if token == entry.get("name") or token == entry.get("display_name"):
            return entry
        if token == entry.get("adb_serial"):
            return entry
    return None


def _serial(entry: dict[str, Any]) -> str:
    return f"emulator-{entry.get('console_port')}"


def _booted(entry: dict[str, Any]) -> bool:
    if entry.get("boot_completed"):
        return True
    complete_at = entry.get("boot_complete_at")
    return bool(complete_at is not None and fake_runtime.now() >= float(complete_at))


def _split_global_target(args: list[str]) -> tuple[str | None, list[str]]:
    """Parse ``adb`` global target flags that appear before the subcommand."""
    serial: str | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"-s", "--serial"} and index + 1 < len(args):
            serial = args[index + 1]
            index += 2
        elif token.startswith("--serial="):
            serial = token.split("=", 1)[1]
            index += 1
        elif token in {"-e", "-d"}:
            index += 1
        elif token in {"-H", "-P"} and index + 1 < len(args):
            index += 2
        elif (token.startswith("-H") or token.startswith("-P")) and len(token) > 2:
            index += 1
        else:
            break
    return serial, args[index:]


def _require_target(state: dict[str, Any], serial: str | None) -> dict[str, Any]:
    if serial:
        entry = _find_avd(state, serial)
        if entry is None or entry.get("state") not in {"booting", "running"}:
            raise ToolFailure(f"error: device '{serial}' not found")
        return entry
    active = _active_avds(state)
    if not active:
        raise ToolFailure("error: no devices/emulators found")
    if len(active) > 1:
        raise ToolFailure("error: more than one device/emulator")
    return active[0]


def _stop_avd(entry: dict[str, Any]) -> None:
    name = str(entry.get("name"))
    pid = entry.get("pid")

    def _mutator(state: dict[str, Any]) -> None:
        current = _avd_entries(state).get(name)
        if current is not None:
            current["state"] = "stopped"
            current["pid"] = None
            current["boot_completed"] = False
            current["boot_complete_at"] = None
            current["started_at"] = None

    _mutate(_mutator)
    if isinstance(pid, int):
        fake_runtime.terminate_registered(tool="emulator", avd=name, timeout=0.5)
        fake_runtime.unregister_pid(pid)


# ---------------------------------------------------------------------------
# adb
# ---------------------------------------------------------------------------


def adb(args: list[str]) -> int:
    serial, rest = _split_global_target(args)
    if not rest:
        err(ADB_USAGE)
        return EXIT_USAGE
    command, tail = rest[0], rest[1:]
    if fake_runtime.should_fail("adb:install") and command == "install":
        err("Failure [INSTALL_FAILED_INVALID_APK: fake failure injected]")
        return EXIT_FAIL
    if fake_runtime.should_fail("adb:uninstall") and command == "uninstall":
        err("Failure [DELETE_FAILED_INTERNAL_ERROR: fake failure injected]")
        return EXIT_FAIL
    state = _state()
    if command in {"start-server", "kill-server"}:
        return EXIT_OK
    if command == "version":
        out("Android Debug Bridge version 1.0.41")
        out("Version 35.0.2-13114725")
        return EXIT_OK
    if command == "devices":
        return _adb_devices(state, tail)
    if command == "wait-for-device":
        return _adb_wait_for_device(state, serial)
    if command == "get-state":
        entry = _require_target(state, serial)
        out("device")
        return EXIT_OK
    if command in {"shell", "exec-out"}:
        entry = _require_target(state, serial)
        return _adb_shell(entry, tail, exec_out=command == "exec-out")
    if command == "getprop":
        entry = _require_target(state, serial)
        return _adb_getprop(entry, tail)
    if command == "logcat":
        return _adb_logcat(tail)
    if command in {"install", "install-multiple"}:
        return _adb_install(tail)
    if command == "uninstall":
        return _adb_uninstall(tail)
    if command in {"forward", "reverse"}:
        entry = _require_target(state, serial)
        return _adb_forward(entry, tail, reverse=command == "reverse")
    if command == "emu":
        entry = _require_target(state, serial)
        return _adb_emu(entry, tail)
    if command == "pull":
        return _adb_pull(tail)
    if command == "push":
        return _adb_push(tail)
    if command in {"root", "remount", "unroot", "reconnect", "disconnect"}:
        out("restarting adbd as root" if command == "root" else "")
        return EXIT_OK
    if command == "reboot":
        entry = _require_target(state, serial)
        _stop_avd(entry)
        return EXIT_OK
    err(f"adb: unknown command {command}")
    return EXIT_OK  # fakes-spec 4.3: unknown subcommands exit 0 with a diagnostic


def _adb_devices(state: dict[str, Any], tail: list[str]) -> int:
    long_format = any(token in {"-l", "--long"} for token in tail)
    for index, entry in enumerate(_active_avds(state), start=1):
        abi = "arm64" if str(entry.get("abi")) == "arm64-v8a" else "x86_64"
        serial = _serial(entry)
        if long_format:
            out(
                f"{serial}\tdevice product:sdk_gphone64_{abi} model:sdk_gphone64_{abi} "
                f"device:emu64x transport_id:{index}"
            )
        else:
            out(f"{serial}\tdevice")
    return EXIT_OK


def _adb_wait_for_device(state: dict[str, Any], serial: str | None) -> int:
    timeout = fake_runtime.boot_seconds() + 5.0
    deadline = time.monotonic() + timeout
    while True:
        current = _state()
        if serial:
            entry = _find_avd(current, serial)
            ready = entry is not None and entry.get("state") in {"booting", "running"}
        else:
            ready = bool(_active_avds(current))
        if ready:
            return EXIT_OK
        if time.monotonic() >= deadline:
            raise ToolFailure("error: device not found")
        time.sleep(0.02)


def _adb_shell(entry: dict[str, Any], shell_args: list[str], *, exec_out: bool = False) -> int:
    if not shell_args:
        # Interactive shell fed from stdin: execute each line sequentially.
        exit_code = EXIT_OK
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            exit_code = _run_shell_command(entry, line, _state())
            if exit_code != EXIT_OK:
                break
        return exit_code
    command = " ".join(shell_args)
    if shell_args[0] in {"sh", "bash"}:
        remaining = shell_args[1:]
        if remaining and remaining[0] == "-c":
            command = " ".join(remaining[1:])
        elif remaining:
            command = " ".join(remaining)
    elif shell_args[0] == "-c":
        command = " ".join(shell_args[1:])
    return _run_shell_command(entry, command, _state(), exec_out=exec_out)


def _lookup_script(command: str) -> dict[str, Any] | None:
    mapping = fake_runtime.script_map()
    if not mapping:
        return None
    candidates = [command, command.strip()]
    with contextlib.suppress(ValueError):
        candidates.append(" ".join(shlex.split(command)))
    for candidate in candidates:
        if candidate in mapping:
            return mapping[candidate]
    return None


def _apply_script(result: dict[str, Any]) -> int:
    if result.get("stdout"):
        out(str(result["stdout"]))
    if result.get("stderr"):
        err(str(result["stderr"]))
    return int(result.get("exit_code") or 0)


def _run_shell_command(
    entry: dict[str, Any],
    command: str,
    state: dict[str, Any],
    *,
    exec_out: bool = False,
) -> int:
    command = command.strip()
    if not command:
        return EXIT_OK
    mapped = _lookup_script(command)
    if mapped is not None:
        return _apply_script(mapped)
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return EXIT_OK
    name = tokens[0]
    if name in {"true", ":"}:
        return EXIT_OK
    if name == "false":
        return EXIT_FAIL
    if name == "echo":
        out(" ".join(tokens[1:]))
        return EXIT_OK
    if name == "exit":
        try:
            return int(tokens[1]) if len(tokens) > 1 else EXIT_OK
        except ValueError:
            return EXIT_USAGE
    if name == "getprop":
        return _adb_getprop(entry, tokens[1:])
    if name == "dumpsys":
        return _shell_dumpsys(state, entry, tokens[1:])
    if name == "am":
        return _shell_am(entry, tokens[1:])
    if name == "settings":
        return _shell_settings(entry, tokens[1:])
    if name == "pm":
        return _shell_pm(state, entry, tokens[1:])
    if name == "input":
        return _shell_input(state, entry, tokens[1:])
    if name == "screencap":
        return _screencap(state, entry)
    if name == "wm":
        return _shell_wm(entry, tokens[1:])
    if name == "logcat":
        return _adb_logcat(tokens[1:])
    if name == "id":
        out("uid=0(root) gid=0(root) context=u:r:shell:s0")
        return EXIT_OK
    if name in {"setprop", "getenforce", "stop", "start", "svc", "cmd", "content", "monkey"}:
        return EXIT_OK
    if exec_out and name not in {"sh", "echo"}:
        # fall through to the deterministic fake-shell echo below
        pass
    out(f"<fake-adb-shell>: {command}")
    return EXIT_OK


def _property_map(entry: dict[str, Any]) -> dict[str, str]:
    abi = str(entry.get("abi") or "x86_64")
    abi_tag = "arm64" if abi == "arm64-v8a" else "x86_64"
    meta = fake_runtime.image_metadata(str(entry.get("package") or "")) if str(
        entry.get("package") or ""
    ).startswith("system-images;") else {"api": entry.get("api", 35), "release": "15"}
    booted = _booted(entry)
    android_id = f"AVDFLEET{int(entry.get('console_port') or 0):06d}"
    return {
        "sys.boot_completed": "1" if booted else "0",
        "dev.bootcomplete": "1" if booted else "0",
        "ro.build.version.sdk": str(meta.get("api", entry.get("api", 35))),
        "ro.build.version.release": str(meta.get("release", "15")),
        "ro.build.version.codename": "REL",
        "ro.build.version.incremental": "12345678",
        "ro.product.cpu.abi": abi,
        "ro.product.cpu.abilist": f"{abi},armeabi-v7a,armeabi",
        "ro.product.cpu.abilist64": abi,
        "ro.serialno": _serial(entry),
        "ro.boot.serialno": _serial(entry),
        "ro.kernel.qemu": "1",
        "ro.boot.qemu": "1",
        "ro.hardware": "ranchu",
        "ro.product.model": f"sdk_gphone64_{abi_tag}",
        "ro.product.name": f"sdk_gphone64_{abi_tag}",
        "ro.product.device": "emu64x",
        "ro.product.manufacturer": "Google",
        "ro.product.brand": "google",
        "ro.build.type": "user",
        "ro.build.tags": "release-keys",
        "ro.build.id": "AP3A.240905.015.A2",
        "ro.build.fingerprint": f"google/sdk_gphone64_{abi_tag}/emu64x:15/AP3A.240905.015.A2:user/release-keys",
        "ro.debuggable": "0",
        "ro.secure": "1",
        "ro.adb.secure": "1",
        "init.svc.bootanim": "stopped" if booted else "running",
        "service.bootanim.exit": "1" if booted else "0",
        "android_id": android_id,
        "gsm.sim.state": "READY",
        "sys.usb.state": "configured",
        "ro.boot.qemu.avd_name": str(entry.get("name") or ""),
    }


def _adb_getprop(entry: dict[str, Any], tail: list[str]) -> int:
    props = _property_map(entry)
    if tail:
        key = tail[0]
        out(props.get(key, ""))
        return EXIT_OK
    for key in sorted(props):
        out(f"[{key}]: [{props[key]}]")
    return EXIT_OK


def _current_activity(entry: dict[str, Any]) -> str:
    return str(entry.get("activity") or fake_runtime.SETUP_WIZARD_ACTIVITY)


def _shell_dumpsys(state: dict[str, Any], entry: dict[str, Any], tokens: list[str]) -> int:
    section = tokens[0] if tokens else ""
    activity = _current_activity(entry)
    if section == "activity":
        out("ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)")
        out("  Display #0 (activities from top to bottom):")
        out(f"  * Task{{1a2b3c4 d0 type=standard A=1000:{activity.split('/')[0]}}}")
        out(f"    mResumedActivity: ActivityRecord{{1a2b3c u0 {activity} t1}}")
        out(f"    topResumedActivity=ActivityRecord{{1a2b3c u0 {activity} t1}}")
        out(f"  mFocusedApp=ActivityRecord{{1a2b3c u0 {activity} t1}}")
        return EXIT_OK
    if section == "window":
        out("WINDOW MANAGER WINDOWS (dumpsys window)")
        out("  Display: mDisplayId=0")
        out(f"    mCurrentFocus=Window{{1a2b3c u0 {activity}}}")
        out(f"    mFocusedApp=AppWindowToken{{1a2b3c token=Token{{1a2b3c ActivityRecord{{1a2b3c u0 {activity} t1}}}}}}")
        return EXIT_OK
    if section == "package":
        package = tokens[1] if len(tokens) > 1 else "com.android.settings"
        out("Packages:")
        out(f"  Package [{package}] (1a2b3c):")
        out("    userId=1000")
        out("    pkg=Package{1a2b3c " + package + "}")
        out("    versionName=15")
        return EXIT_OK
    out(f"DUMP OF SERVICE {section or 'unknown'}:")
    return EXIT_OK


def _component(value: str) -> str:
    value = value.strip()
    if value.startswith("."):
        return value
    if "/" not in value and "." in value:
        return value + "/."
    return value


def _shell_am(entry: dict[str, Any], tokens: list[str]) -> int:
    if not tokens:
        err("Error: no command specified")
        return EXIT_FAIL
    subcommand = tokens[0]
    remaining = tokens[1:]
    if subcommand in {"start", "start-activity"}:
        component = ""
        action = ""
        index = 0
        while index < len(remaining):
            token = remaining[index]
            if token in {"-n", "--component"} and index + 1 < len(remaining):
                component = remaining[index + 1]
                index += 2
            elif token.startswith("--component="):
                component = token.split("=", 1)[1]
                index += 1
            elif token in {"-a", "--action"} and index + 1 < len(remaining):
                action = remaining[index + 1]
                index += 2
            elif token.startswith("--action="):
                action = token.split("=", 1)[1]
                index += 1
            elif token in {"-d", "-c", "-t", "-f", "--user", "-p"} and index + 1 < len(remaining):
                index += 2
            elif token in {"--es", "--ez", "--ei", "--el", "--ef", "--ecn", "--eia", "--eial"} and index + 2 < len(
                remaining
            ):
                index += 3
            else:
                index += 1
        if not component and remaining and "/" in remaining[-1] and not remaining[-1].startswith("-"):
            component = remaining[-1]

        def _mutator(state: dict[str, Any]) -> None:
            current = _avd_entries(state).get(str(entry.get("name")))
            if current is None:
                return
            if component:
                resolved = _component(component)
                current["activity"] = resolved
                current["oobe"] = (
                    "setup" if "setupwizard" in resolved.lower() else "complete"
                )
                if current["oobe"] == "complete" and not resolved:
                    current["activity"] = fake_runtime.LAUNCHER_ACTIVITY
            elif action == "android.intent.action.MAIN" and current.get("oobe") == "complete":
                current["activity"] = fake_runtime.LAUNCHER_ACTIVITY

        _mutate(_mutator)
        if component:
            out(f"Starting: Intent {{ cmp={_component(component)} }}")
        elif action:
            out(f"Starting: Intent {{ act={action} }}")
        else:
            out("Starting: Intent { }")
        return EXIT_OK
    if subcommand in {"force-stop", "force-stop-package", "kill"}:
        package = remaining[0] if remaining else ""
        if package:
            out(f"Stopping {package}")

            def _mutator(state: dict[str, Any]) -> None:
                current = _avd_entries(state).get(str(entry.get("name")))
                if current is not None:
                    current["activity"] = fake_runtime.LAUNCHER_ACTIVITY
                    current["oobe"] = "complete"

            _mutate(_mutator)
        return EXIT_OK
    if subcommand in {"broadcast", "instrument", "monitor", "dumpheap", "set-debug-app"}:
        if subcommand == "broadcast":
            out("Broadcasting: Intent { }")
            out("Broadcast completed: result=0")
        return EXIT_OK
    return EXIT_OK


def _shell_settings(entry: dict[str, Any], tokens: list[str]) -> int:
    if not tokens:
        return EXIT_USAGE
    action = tokens[0]
    state = _state()
    current = _avd_entries(state).get(str(entry.get("name"))) or entry
    settings = current.get("settings")
    if not isinstance(settings, dict):
        settings = {}
    if action == "get" and len(tokens) >= 2:
        key = tokens[-1]
        if key == "user_setup_complete":
            out("1" if current.get("oobe") == "complete" else "0")
            return EXIT_OK
        value = settings.get(key)
        out(str(value) if value is not None else "null")
        return EXIT_OK
    if action in {"put", "delete"}:
        key = tokens[2] if len(tokens) >= 3 else ""
        value = tokens[3] if action == "put" and len(tokens) >= 4 else ""
        if key == "user_setup_complete":
            numeric = str(value) if action == "put" else ""
            if numeric in {"1", "true"}:

                def _set_complete(state_dict: dict[str, Any]) -> None:
                    target = _avd_entries(state_dict).get(str(entry.get("name")))
                    if target is not None:
                        target["oobe"] = "complete"
                        target["activity"] = fake_runtime.LAUNCHER_ACTIVITY

                _mutate(_set_complete)
            elif numeric in {"0", "false"}:

                def _set_setup(state_dict: dict[str, Any]) -> None:
                    target = _avd_entries(state_dict).get(str(entry.get("name")))
                    if target is not None:
                        target["oobe"] = "setup"
                        target["activity"] = fake_runtime.SETUP_WIZARD_ACTIVITY

                _mutate(_set_setup)
        return EXIT_OK
    if action == "list":
        out("user_setup_complete=" + ("1" if current.get("oobe") == "complete" else "0"))
        return EXIT_OK
    return EXIT_OK


def _shell_pm(state: dict[str, Any], entry: dict[str, Any], tokens: list[str]) -> int:
    if not tokens:
        return EXIT_USAGE
    action = tokens[0]
    remaining = tokens[1:]
    if action == "list":
        packages = list(SEEDED_PACKAGES)
        filter_value = ""
        for token in remaining:
            if not token.startswith("-") and token != "packages":
                filter_value = token
        for package in sorted(packages):
            if filter_value and filter_value not in package:
                continue
            out(f"package:{package}")
        return EXIT_OK
    if action == "path":
        if not remaining:
            return EXIT_USAGE
        package = remaining[0]
        if package in SEEDED_PACKAGES:
            out(f"package:/data/app/{package}/base.apk")
        return EXIT_OK
    if action in {"clear", "grant", "revoke", "install", "uninstall", "enable", "disable"}:
        if action == "clear":
            out("Success")
        return EXIT_OK
    if action in {"get-install-location", "get-max-users"}:
        out("0[auto]")
        return EXIT_OK
    return EXIT_OK


def _shell_input(state: dict[str, Any], entry: dict[str, Any], tokens: list[str]) -> int:
    if not tokens:
        return EXIT_USAGE
    name = str(entry.get("name"))
    kind = tokens[0]
    remaining = [token for token in tokens[1:] if not token.startswith("-")]
    record: dict[str, Any] = {"instance": name, "kind": kind}
    try:
        if kind == "tap" and len(remaining) >= 2:
            record.update({"x": int(remaining[0]), "y": int(remaining[1])})
        elif kind == "swipe" and len(remaining) >= 4:
            record.update(
                {
                    "x1": int(remaining[0]),
                    "y1": int(remaining[1]),
                    "x2": int(remaining[2]),
                    "y2": int(remaining[3]),
                }
            )
            if len(remaining) >= 5:
                record["duration_ms"] = int(remaining[4])
        elif kind == "text":
            record["text"] = " ".join(remaining)
        elif kind == "keyevent":
            record["keycode"] = remaining[0] if remaining else ""
        elif kind in {"draganddrop", "roll", "press", "swipe"}:
            pass
        else:
            err(f"Error: Invalid arguments for input {kind}")
            return EXIT_FAIL
    except ValueError:
        err("Error: Invalid arguments")
        return EXIT_FAIL

    def _mutator(current: dict[str, Any]) -> None:
        inputs = current.setdefault("inputs", [])
        inputs.append(record)
        if len(inputs) > 500:
            del inputs[: len(inputs) - 500]

    _mutate(_mutator)
    return EXIT_OK


def _shell_wm(entry: dict[str, Any], tokens: list[str]) -> int:
    action = tokens[0] if tokens else ""
    profile = fake_runtime.DEVICE_PROFILES.get(str(entry.get("device") or "")) or {
        "resolution": "1080x1920",
        "density": 420,
    }
    if action == "size":
        out(f"Physical size: {profile['resolution']}")
        return EXIT_OK
    if action == "density":
        out(f"Physical density: {profile['density']}")
        return EXIT_OK
    return EXIT_OK


def _screencap(state: dict[str, Any], entry: dict[str, Any]) -> int:
    screenshots = state.get("screenshots")
    spec: dict[str, Any] = {}
    if isinstance(screenshots, dict):
        candidate = screenshots.get(str(entry.get("name")))
        if isinstance(candidate, dict):
            spec = candidate
    encoded = spec.get("png_base64")
    if isinstance(encoded, str) and encoded:
        out_bytes(base64.b64decode(encoded))
        return EXIT_OK
    width = int(spec.get("width") or 1080)
    height = int(spec.get("height") or 1920)
    out_bytes(fake_runtime.png_bytes(width, height))
    return EXIT_OK


def _adb_logcat(tail: list[str]) -> int:
    if any(token in {"-c", "--clear"} for token in tail):
        return EXIT_OK
    dump_only = any(token in {"-d", "--dump"} for token in tail)
    if dump_only:
        for line in LOGCAT_LINES:
            out(line)
        return EXIT_OK
    counter = 0
    try:
        while True:
            line = LOGCAT_LINES[counter % len(LOGCAT_LINES)]
            out(f"{line} [{counter}]")
            counter += 1
            time.sleep(0.05)
    except KeyboardInterrupt:
        return EXIT_OK


def _adb_install(tail: list[str]) -> int:
    flags_with_value = {"--user", "-t", "--abi", "-g", "-d", "-l", "-s", "--install-location"}
    path = ""
    index = 0
    while index < len(tail):
        token = tail[index]
        if token in flags_with_value and index + 1 < len(tail) and token in {"--user", "--abi", "--install-location"}:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        path = token
        index += 1
    if not path:
        err("adb: install requires an APK path")
        return EXIT_USAGE
    out("Performing Streamed Install")
    out("Success")
    return EXIT_OK


def _adb_uninstall(tail: list[str]) -> int:
    packages = [token for token in tail if not token.startswith("-")]
    if not packages:
        err("adb: uninstall requires a package name")
        return EXIT_USAGE
    out("Success")
    return EXIT_OK


def _adb_forward(entry: dict[str, Any], tail: list[str], *, reverse: bool) -> int:
    name = str(entry.get("name"))
    key = "reverses" if reverse else "forwards"
    if not tail:
        err(f"adb: {('reverse' if reverse else 'forward')} requires arguments")
        return EXIT_USAGE
    if "--remove-all" in tail:
        def _clear(state: dict[str, Any]) -> None:
            current = _avd_entries(state).get(name)
            if current is not None:
                current[key] = []

        _mutate(_clear)
        return EXIT_OK
    if "--list" in tail:
        for record in entry.get(key) or []:
            out(f"{_serial(entry)} {record.get('local')} {record.get('remote')}")
        return EXIT_OK
    if "--remove" in tail:
        target = tail[tail.index("--remove") + 1] if tail.index("--remove") + 1 < len(tail) else ""
        if target and "=" in target:
            target = target.split("=", 1)[1]

        def _remove(state: dict[str, Any]) -> None:
            current = _avd_entries(state).get(name)
            if current is None:
                return
            records = current.get(key) or []
            current[key] = [record for record in records if record.get("local") != target]

        _mutate(_remove)
        return EXIT_OK
    specs = [token for token in tail if not token.startswith("-")]
    if len(specs) < 2:
        err(f"adb: {('reverse' if reverse else 'forward')} requires local and remote specs")
        return EXIT_USAGE
    local, remote = specs[0], specs[1]

    def _add(state: dict[str, Any]) -> None:
        current = _avd_entries(state).get(name)
        if current is None:
            return
        records = current.setdefault(key, [])
        records.append({"local": local, "remote": remote})

    _mutate(_add)
    out(local.split(":", 1)[1] if local.startswith("tcp:") else local)
    return EXIT_OK


def _adb_emu(entry: dict[str, Any], tail: list[str]) -> int:
    if not tail:
        err("KO: no emulator command given")
        return EXIT_USAGE
    command = tail[0]
    if command == "kill":
        _stop_avd(entry)
        out("OK")
        return EXIT_OK
    if command == "avd":
        subcommand = tail[1] if len(tail) > 1 else ""
        if subcommand == "name":
            out(str(entry.get("name")))
            return EXIT_OK
        if subcommand == "path":
            out(str(fake_runtime.avd_dir_for(_state().get("avd_home", ""), str(entry.get("name")))))
            return EXIT_OK
    err(f"KO: unknown command '{command}'")
    return EXIT_OK


def _adb_pull(tail: list[str]) -> int:
    positional = [token for token in tail if not token.startswith("-")]
    if len(positional) >= 2:
        destination = Path(positional[1])
        if not destination.is_absolute():
            destination = Path.cwd() / destination
        with contextlib.suppress(OSError):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"/data/local/tmp/fake-pull\n")
    out("1 file pulled, 0 skipped. 0.0 MB/s")
    return EXIT_OK


def _adb_push(tail: list[str]) -> int:
    out("1 file pushed, 0 skipped. 0.0 MB/s")
    return EXIT_OK


# ---------------------------------------------------------------------------
# sdkmanager / android (package catalogue)
# ---------------------------------------------------------------------------


def _table(headers: list[str], rows: list[list[str]], *, separator_lengths: list[int] | None = None) -> list[str]:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    separator = separator_lengths or [len(header) for header in headers]
    lines = ["  " + " | ".join(header.ljust(widths[i]) for i, header in enumerate(headers))]
    lines.append("  " + " | ".join("-" * separator[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  " + " | ".join(row[i].ljust(widths[i]) for i in range(len(headers))))
    return lines


def _installed_packages(state: dict[str, Any]) -> list[str]:
    installed = state.get("installed")
    return [str(item) for item in installed] if isinstance(installed, list) else []


def _package_rows(package_ids: list[str], sdk_root: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for package_id in sorted(package_ids):
        package = fake_runtime.CATALOGUE.get(package_id)
        if package is None:
            continue
        location = fake_runtime.package_location(package_id)
        rows.append([package_id, package.version, package.description, location])
    return rows


def _sdkmanager_list(state: dict[str, Any], *, installed_only: bool, sdk_root: str) -> None:
    installed = _installed_packages(state)
    installed_rows = _package_rows(installed, sdk_root)
    lines = ["Installed packages:"]
    lines.extend(_table(["Path", "Version", "Description", "Location"], installed_rows))
    if not installed_only:
        available = [package_id for package_id in fake_runtime.CATALOGUE if package_id not in installed]
        lines.append("Available Packages:")
        lines.extend(_table(["Path", "Version", "Description", "Location"], _package_rows(available, sdk_root)))
    for line in lines:
        out(line)


def _sdk_root_for(tail: list[str], state: dict[str, Any]) -> tuple[str, list[str]]:
    sdk_root = str(state.get("sdk_root") or fake_runtime.env_value("ANDROID_SDK_ROOT") or "")
    remaining: list[str] = []
    index = 0
    while index < len(tail):
        token = tail[index]
        if token == "--sdk_root" and index + 1 < len(tail):
            sdk_root = tail[index + 1]
            index += 2
            continue
        if token.startswith("--sdk_root="):
            sdk_root = token.split("=", 1)[1]
            index += 1
            continue
        remaining.append(token)
        index += 1
    return sdk_root, remaining


def _sdkmanager_install(package_ids: list[str], sdk_root: str, *, verbose: bool) -> int:
    if not package_ids:
        err(SDKMANAGER_USAGE)
        return EXIT_USAGE
    unknown = [package_id for package_id in package_ids if package_id not in fake_runtime.CATALOGUE]
    if unknown:
        for package_id in unknown:
            err(f"Warning: Failed to find package {package_id}")
        return EXIT_FAIL
    if fake_runtime.should_fail("sdkmanager:install"):
        err("Warning: Failed to install packages: fake failure injected")
        return EXIT_FAIL
    for package_id in package_ids:
        out(f'Preparing "Install {package_id}".')
        out("Downloading...")
        out("Unzipping...")
        out("Installing...")
        if verbose:
            err(f"INFO: Loaded package {package_id} from the fake repository")
        fake_runtime.materialize_package(sdk_root, package_id)

    def _install(state: dict[str, Any]) -> None:
        installed = state.setdefault("installed", [])
        for package_id in package_ids:
            if package_id not in installed:
                installed.append(package_id)
        installed.sort()

    _mutate(_install)
    bin_dir = fake_runtime.env_value("AVDFLEET_FAKE_BIN")
    tool_packages = {"platform-tools", "emulator", "cmdline-tools;latest", "cmdline-tools;11.0"}
    if bin_dir and tool_packages & set(package_ids):
        fake_runtime.write_sdk_tool_wrappers(sdk_root, bin_dir)
    out("done")
    return EXIT_OK


def _sdkmanager_uninstall(package_ids: list[str], sdk_root: str) -> int:
    if not package_ids:
        err(SDKMANAGER_USAGE)
        return EXIT_USAGE
    installed = _installed_packages(_state())
    for package_id in package_ids:
        if package_id not in installed and package_id not in fake_runtime.CATALOGUE:
            err(f"Warning: Failed to find package {package_id}")
            return EXIT_FAIL

    def _remove(state: dict[str, Any]) -> None:
        current = state.setdefault("installed", [])
        for package_id in package_ids:
            with contextlib.suppress(ValueError):
                current.remove(package_id)

    _mutate(_remove)
    for package_id in package_ids:
        directory = fake_runtime.sdk_package_dir(sdk_root, package_id)
        with contextlib.suppress(OSError):
            import shutil

            shutil.rmtree(directory)
    return EXIT_OK


def _licenses(state: dict[str, Any], sdk_root: str, *, include_obsolete: bool) -> int:
    out("Review licenses that have not been accepted (y/N)?")
    accepted = False
    for line in sys.stdin:
        if line.strip().lower().startswith("y"):
            accepted = True
            break
    if not accepted:
        err("License not accepted")
        return EXIT_FAIL
    licenses = {"android-sdk-license": fake_runtime.ANDROID_SDK_LICENSE_HASH}
    if include_obsolete:
        licenses["android-sdk-preview-license"] = fake_runtime.ANDROID_PREVIEW_LICENSE_HASH
    else:
        licenses["android-sdk-preview-license"] = fake_runtime.ANDROID_PREVIEW_LICENSE_HASH
    license_dir = Path(sdk_root) / "licenses"
    license_dir.mkdir(parents=True, exist_ok=True)
    for name, digest in licenses.items():
        (license_dir / name).write_text(digest + "\n", encoding="utf-8")

    def _record(state_dict: dict[str, Any]) -> None:
        current = state_dict.setdefault("licenses_accepted", [])
        for name in licenses:
            if name not in current:
                current.append(name)
        current.sort()

    _mutate(_record)
    out("All SDK package licenses accepted.")
    return EXIT_OK


def sdkmanager(args: list[str]) -> int:
    if "--version" in args or "-v" in args:
        out("11.0.0")
        return EXIT_OK
    if "--help" in args or "-h" in args:
        out(SDKMANAGER_USAGE)
        return EXIT_OK
    state = _state()
    sdk_root, tail = _sdk_root_for(args, state)
    if not sdk_root:
        raise ToolFailure("ERROR: ANDROID_SDK_ROOT is not set")
    channel = "0"
    verbose = False
    command = ""
    package_ids: list[str] = []
    include_obsolete = False
    for token in tail:
        if token in {"--list"}:
            command = "list"
        elif token in {"--list_installed"}:
            command = "list_installed"
        elif token in {"--licenses"}:
            command = "licenses"
        elif token in {"--install", "--uninstall"}:
            command = token[2:]
        elif token.startswith("--channel="):
            channel = token.split("=", 1)[1]
        elif token in {"--channel"}:
            channel = "0"
        elif token in {"--include_obsolete"}:
            include_obsolete = True
        elif token in {"--verbose"}:
            verbose = True
        elif token.startswith("--") or token.startswith("-"):
            continue
        else:
            package_ids.append(token)
    channel_names = {"0": "stable", "1": "beta", "2": "dev", "3": "canary"}
    if verbose:
        err(f"INFO: Using SDK channel {channel_names.get(channel, 'stable')}")
    if command == "list":
        _sdkmanager_list(state, installed_only=False, sdk_root=sdk_root)
        return EXIT_OK
    if command == "list_installed":
        _sdkmanager_list(state, installed_only=True, sdk_root=sdk_root)
        return EXIT_OK
    if command == "licenses":
        return _licenses(state, sdk_root, include_obsolete=include_obsolete)
    if command == "install":
        return _sdkmanager_install(package_ids, sdk_root, verbose=verbose)
    if command == "uninstall":
        return _sdkmanager_uninstall(package_ids, sdk_root)
    err(SDKMANAGER_USAGE)
    return EXIT_USAGE


def _avd_list_json(state: dict[str, Any]) -> list[dict[str, Any]]:
    entries = []
    for name in sorted(_avd_entries(state)):
        entry = _avd_entries(state)[name]
        entries.append(
            {
                "name": name,
                "display_name": entry.get("display_name", name),
                "package": entry.get("package", ""),
                "device": entry.get("device", ""),
                "api": entry.get("api"),
                "abi": entry.get("abi", ""),
                "state": entry.get("state", "stopped"),
                "console_port": entry.get("console_port"),
                "adb_port": entry.get("adb_port"),
            }
        )
    return entries


def _create_avd(
    *,
    name: str,
    package: str,
    device: str,
    force: bool,
    silent: bool,
    path: str | None = None,
    sdk_root: str,
) -> int:
    if not name:
        err("Error: --name is required")
        return EXIT_USAGE
    if not package:
        err("Error: --package is required")
        return EXIT_USAGE
    state = _state()
    installed = _installed_packages(state)
    if package not in installed:
        err("Error: Package path is not valid.")
        return EXIT_FAIL
    canonical = fake_runtime.canonical_device(device) if device else fake_runtime.DEFAULT_DEVICE
    if not canonical:
        err(f"Error: Invalid --device value '{device}'")
        return EXIT_FAIL
    avd_home = str(state.get("avd_home") or fake_runtime.env_value("ANDROID_AVD_HOME") or "")
    if not avd_home:
        raise ToolFailure("ERROR: ANDROID_AVD_HOME is not set")
    existing = _avd_entries(state).get(name)
    if existing is not None and not force:
        err(f"Error: AVD '{name}' already exists.")
        return EXIT_FAIL
    if existing is not None and force:
        fake_runtime.remove_avd_files(avd_home, name)
    entry = fake_runtime.build_avd_entry(name, package, canonical)
    if path:
        entry["avd_path"] = path
    fake_runtime.write_avd_files(avd_home, entry, path=path)

    def _add(state_dict: dict[str, Any]) -> None:
        state_dict.setdefault("avds", {})[name] = entry

    _mutate(_add)
    if not silent:
        out(f"Auto-selecting single ABI {entry['abi']}")
        out(f"Created AVD '{name}' based on {package}.")
    return EXIT_OK


def _list_devices() -> int:
    out("Available devices definitions:")
    for name, profile in fake_runtime.DEVICE_PROFILES.items():
        out(f'id: {profile["id"]} or "{name}"')
        out(f'    Name: {profile["name"]}')
        out(f"    OEM : {profile['oem']}")
        out("---------")
    return EXIT_OK


def _list_avds_text(state: dict[str, Any], *, compact: bool) -> int:
    entries = _avd_entries(state)
    if compact:
        for name in sorted(entries):
            out(name)
        return EXIT_OK
    out("Available Android Virtual Devices:")
    for name in sorted(entries):
        entry = entries[name]
        avd_home = str(state.get("avd_home") or "")
        package = str(entry.get("package") or "")
        meta = fake_runtime.image_metadata(package) if package.startswith("system-images;") else {
            "api": entry.get("api", 35),
            "tag": "google_apis_playstore",
            "abi": entry.get("abi", "x86_64"),
        }
        target = {
            "google_apis_playstore": "Google Play (Google Inc.)",
            "google_apis": "Google APIs (Google Inc.)",
            "aosp_atd": "Android Open Source Project",
        }.get(meta["tag"], "Android Open Source Project")
        out(f"    Name: {entry.get('display_name', name)}")
        out(f"    Path: {avd_home}/{name}.avd")
        out(f"  Target: {target}")
        out(f"  Based on: Android API {meta['api']} Tag/ABI: {meta['tag']}/{meta['abi']}")
        out("---------")
    return EXIT_OK


def _list_targets(state: dict[str, Any]) -> int:
    installed = _installed_packages(state)
    out("Available Android targets:")
    index = 0
    for package_id in sorted(installed):
        if not package_id.startswith("platforms;android-"):
            continue
        index += 1
        api = package_id.split("android-")[-1]
        out("----------")
        out(f'id: {index} or "android-{api}"')
        out(f"     Name: Android API {api}")
        out("     Type: Platform")
        out(f"     API level: {api}")
        out("----------")
    return EXIT_OK


def _delete_avd(*, name: str) -> int:
    if not name:
        err("Error: --name is required")
        return EXIT_USAGE
    state = _state()
    if name not in _avd_entries(state):
        err(f"Error: AVD '{name}' does not exist.")
        return EXIT_FAIL
    avd_home = str(state.get("avd_home") or fake_runtime.env_value("ANDROID_AVD_HOME") or "")
    fake_runtime.remove_avd_files(avd_home, name)

    def _remove(state_dict: dict[str, Any]) -> None:
        state_dict.setdefault("avds", {}).pop(name, None)

    _mutate(_remove)
    return EXIT_OK


def _move_avd(*, name: str, new_path: str) -> int:
    if not name or not new_path:
        err("Error: -n and -p are required")
        return EXIT_USAGE
    state = _state()
    entry = _avd_entries(state).get(name)
    if entry is None:
        err(f"Error: AVD '{name}' does not exist.")
        return EXIT_FAIL
    avd_home = str(state.get("avd_home") or fake_runtime.env_value("ANDROID_AVD_HOME") or "")
    moved = dict(entry)
    moved["avd_path"] = new_path
    fake_runtime.remove_avd_files(avd_home, name)
    fake_runtime.write_avd_files(avd_home, moved, path=new_path)

    def _update(state_dict: dict[str, Any]) -> None:
        state_dict.setdefault("avds", {})[name] = moved

    _mutate(_update)
    return EXIT_OK


def _avdmanager_options(tail: list[str]) -> dict[str, Any]:
    options: dict[str, Any] = {
        "name": "",
        "package": "",
        "device": "",
        "force": False,
        "silent": False,
        "path": None,
        "sd_card": None,
    }
    value_flags = {
        "-n": "name",
        "--name": "name",
        "-k": "package",
        "--package": "package",
        "-d": "device",
        "--device": "device",
        "-p": "path",
        "--path": "path",
        "-c": "sd_card",
        "--sd-card": "sd_card",
    }
    index = 0
    while index < len(tail):
        token = tail[index]
        if token.startswith("--") and "=" in token:
            flag, value = token.split("=", 1)
            key = value_flags.get(flag)
            if key is not None:
                options[key] = value
                index += 1
                continue
        if token in value_flags and index + 1 < len(tail):
            options[value_flags[token]] = tail[index + 1]
            index += 2
            continue
        if token in {"--force", "-f"}:
            options["force"] = True
        elif token == "--silent":
            options["silent"] = True
        index += 1
    return options


def avdmanager(args: list[str]) -> int:
    if not args:
        err(AVDMANAGER_USAGE)
        return EXIT_USAGE
    command = args[0]
    state = _state()
    if command == "list":
        section = args[1] if len(args) > 1 else "avd"
        if section == "avd":
            return _list_avds_text(state, compact="-c" in args)
        if section == "device":
            return _list_devices()
        if section == "target":
            return _list_targets(state)
        err(f"Error: unknown list target '{section}'")
        return EXIT_USAGE
    if command == "create":
        options = _avdmanager_options(args[1:])
        sdk_root = str(state.get("sdk_root") or fake_runtime.env_value("ANDROID_SDK_ROOT") or "")
        return _create_avd(
            name=options["name"],
            package=options["package"],
            device=options["device"] or fake_runtime.DEFAULT_DEVICE,
            force=options["force"],
            silent=options["silent"],
            path=options["path"],
            sdk_root=sdk_root,
        )
    if command == "delete":
        options = _avdmanager_options(args[1:])
        return _delete_avd(name=options["name"])
    if command == "move":
        options = _avdmanager_options(args[1:])
        return _move_avd(name=options["name"], new_path=options["path"] or "")
    err(AVDMANAGER_USAGE)
    return EXIT_USAGE


# ---------------------------------------------------------------------------
# android (new CLI, S1a)
# ---------------------------------------------------------------------------


def android_cli(args: list[str]) -> int:
    if not args:
        err(ANDROID_USAGE)
        return EXIT_USAGE
    if args[0] in {"--version", "-v", "version"}:
        out("android 1.0.0 (fake)")
        return EXIT_OK
    if args[0] in {"--help", "-h", "help"}:
        out(ANDROID_USAGE)
        return EXIT_OK
    group = args[0]
    tail = args[1:]
    state = _state()
    if group == "sdk":
        if not tail:
            err(ANDROID_USAGE)
            return EXIT_USAGE
        subcommand = tail[0]
        rest = tail[1:]
        sdk_root = str(state.get("sdk_root") or fake_runtime.env_value("ANDROID_SDK_ROOT") or "")
        if subcommand == "list":
            _sdkmanager_list(state, installed_only="--installed" in rest, sdk_root=sdk_root)
            return EXIT_OK
        if subcommand == "install":
            package_ids = [token for token in rest if not token.startswith("-")]
            return _sdkmanager_install(package_ids, sdk_root, verbose=False)
        if subcommand == "uninstall":
            package_ids = [token for token in rest if not token.startswith("-")]
            return _sdkmanager_uninstall(package_ids, sdk_root)
        if subcommand == "licenses":
            return _licenses(state, sdk_root, include_obsolete="--include_obsolete" in rest)
        err(ANDROID_USAGE)
        return EXIT_USAGE
    if group == "avd":
        if not tail:
            err(ANDROID_USAGE)
            return EXIT_USAGE
        subcommand = tail[0]
        rest = tail[1:]
        if subcommand == "list":
            if "--json" in rest:
                out(json.dumps(_avd_list_json(state), indent=2))
            else:
                out("NAME  PACKAGE  DEVICE  STATE")
                for entry in _avd_list_json(state):
                    out(
                        f"{entry['name']}  {entry['package']}  {entry['device']}  {entry['state']}"
                    )
            return EXIT_OK
        if subcommand == "create":
            options = _avdmanager_options(rest)
            sdk_root = str(state.get("sdk_root") or fake_runtime.env_value("ANDROID_SDK_ROOT") or "")
            return _create_avd(
                name=options["name"],
                package=options["package"],
                device=options["device"] or fake_runtime.DEFAULT_DEVICE,
                force=options["force"],
                silent="--silent" in rest,
                path=options["path"],
                sdk_root=sdk_root,
            )
        if subcommand == "delete":
            options = _avdmanager_options(rest)
            return _delete_avd(name=options["name"])
        err(ANDROID_USAGE)
        return EXIT_USAGE
    if group == "emulator":
        if not tail:
            err(ANDROID_USAGE)
            return EXIT_USAGE
        if tail[0] == "list":
            running = [
                {
                    "name": entry.get("name"),
                    "serial": _serial(entry),
                    "state": entry.get("state"),
                    "console_port": entry.get("console_port"),
                    "adb_port": entry.get("adb_port"),
                    "grpc_port": entry.get("grpc_port"),
                }
                for entry in _active_avds(state)
            ]
            out(json.dumps(running, indent=2))
            return EXIT_OK
        err(ANDROID_USAGE)
        return EXIT_USAGE
    err(ANDROID_USAGE)
    return EXIT_USAGE


# ---------------------------------------------------------------------------
# emulator
# ---------------------------------------------------------------------------

_EMULATOR_VALUE_FLAGS = {
    "-avd": "avd",
    "@": "avd_at",
    "-port": "port",
    "-gpu": "gpu",
    "-http-proxy": "proxy",
    "-grpc": "grpc",
    "-accel": "accel",
    "-snapshot": "snapshot",
    "-memory": "memory",
    "-cores": "cores",
    "-netdelay": "netdelay",
    "-netspeed": "netspeed",
    "-camera-back": "camera_back",
    "-camera-front": "camera_front",
    "-sdcard": "sdcard",
    "-partition-size": "partition_size",
    "-dns-server": "dns_server",
    "-prop": "prop",
    "-qemu": "qemu",
    "-logcat": "logcat",
    "-feature": "feature",
    "-tcpdump": "tcpdump",
    "-timezone": "timezone",
}


def _parse_emulator_args(args: list[str]) -> dict[str, Any]:
    options: dict[str, Any] = {
        "avd": "",
        "port": None,
        "gpu": None,
        "proxy": None,
        "grpc": None,
        "accel": None,
        "snapshot": None,
        "no_snapshot": False,
        "wipe_data": False,
        "no_window": False,
        "memory": None,
        "cores": None,
        "extra": [],
    }
    index = 0
    while index < len(args):
        token = args[index]
        name = _EMULATOR_VALUE_FLAGS.get(token)
        if name is not None and index + 1 < len(args):
            key = "avd" if name == "avd_at" else name
            options[key] = args[index + 1]
            index += 2
            continue
        if token.startswith("@"):
            options["avd"] = token[1:]
            index += 1
            continue
        if token == "-no-window":
            options["no_window"] = True
        elif token == "-wipe-data":
            options["wipe_data"] = True
        elif token in {"-no-snapshot", "-no-snapshot-load", "-no-snapshot-save"}:
            options["no_snapshot"] = True
        elif token in {"-read-only", "-no-boot-anim", "-no-audio", "-no-metrics", "-verbose", "-show-kernel",
                       "-skip-adb-auth", "-no-sim", "-shell", "-writable-system"} or token.startswith("-"):
            options["extra"].append(token)
        index += 1
    for key in ("port", "grpc"):
        value = options.get(key)
        if value is not None:
            try:
                options[key] = int(value)
            except ValueError as exc:
                raise UsageError(f"emulator: invalid value for {key}: {value}") from exc
    return options


class _EmulatorConsole(threading.Thread):
    """Minimal emulator console line protocol served on the console port."""

    def __init__(self, server: _FakeEmulatorServer) -> None:
        super().__init__(name=f"emulator-console-{server.console_port}", daemon=True)
        self.server = server

    def run(self) -> None:
        while not self.server.stop_event.is_set():
            try:
                connection, _address = self.server.socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        with connection:
            connection.settimeout(30.0)
            buffer = b""
            authed = False
            self._send(
                connection,
                "Android Console: type 'help' for a list of commands\nOK\n",
            )
            while not self.server.stop_event.is_set():
                try:
                    chunk = connection.recv(1024)
                except (TimeoutError, OSError):
                    return
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", 1)
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    if not authed:
                        if line.startswith("auth "):
                            supplied = line.split(" ", 1)[1].strip()
                            if supplied == self.server.auth_token:
                                authed = True
                                self._send(connection, "OK\n")
                            else:
                                self._send(connection, "KO: authentication failed\n")
                            continue
                        if line == "help":
                            self._send(connection, self.server.help_text())
                            continue
                        self._send(connection, "KO: authentication required\n")
                        continue
                    response = self.server.handle_command(line)
                    if response is None:
                        return
                    self._send(connection, response)

    @staticmethod
    def _send(connection: socket.socket, text: str) -> None:
        with contextlib.suppress(OSError):
            connection.sendall(text.encode("utf-8"))


class _FakeEmulatorServer:
    """State shared between the emulator main loop and its console server."""

    def __init__(self, name: str, console_port: int, home: Path) -> None:
        self.name = name
        self.console_port = console_port
        self.home = home
        self.stop_event = threading.Event()
        self.auth_token = fake_runtime.CONSOLE_AUTH_TOKEN
        token_path = home / ".emulator_console_auth_token"
        with contextlib.suppress(OSError):
            content = token_path.read_text(encoding="utf-8").strip()
            if content:
                self.auth_token = content
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            # On Windows SO_REUSEADDR would let two emulators bind the same
            # console port; there we rely on exclusive bind semantics instead.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", console_port))
        self.socket.listen(5)
        self.socket.settimeout(0.2)
        self.console = _EmulatorConsole(self)

    def help_text(self) -> str:
        return (
            "Available commands:\n"
            "  avd name\n"
            "  kill\n"
            "  snapshot save <name>\n"
            "  snapshot load <name>\n"
            "  help\n"
            "OK\n"
        )

    def handle_command(self, line: str) -> str | None:
        if line == "help":
            return self.help_text()
        if line in {"quit", "exit"}:
            return "OK\n"
        if line == "avd name":
            return f"{self.name}\nOK\n"
        if line == "kill":
            self.stop_event.set()
            return "OK\n"
        if line.startswith("snapshot save "):
            snapshot = line[len("snapshot save ") :].strip()
            self._record_snapshot(snapshot)
            return "OK\n"
        if line.startswith("snapshot load "):
            snapshot = line[len("snapshot load ") :].strip()
            self._load_snapshot(snapshot)
            return "OK\n"
        return "KO: unknown command\n"

    def _record_snapshot(self, snapshot: str) -> None:
        def _save(state: dict[str, Any]) -> None:
            current = _avd_entries(state).get(self.name)
            snapshots = state.setdefault("snapshots", {})
            snapshots.setdefault(self.name, {})[snapshot] = {
                "name": snapshot,
                "created_at": fake_runtime.now(),
                "golden": "golden" in snapshot.lower(),
            }
            if current is not None:
                current["snapshot"] = snapshot

        fake_runtime.mutate_state(_save)

    def _load_snapshot(self, snapshot: str) -> None:
        def _load(state: dict[str, Any]) -> None:
            current = _avd_entries(state).get(self.name)
            if current is None:
                return
            current["snapshot"] = snapshot
            if "golden" in snapshot.lower():
                current["oobe"] = "setup"
                current["activity"] = fake_runtime.SETUP_WIZARD_ACTIVITY
                current["boot_completed"] = True

        fake_runtime.mutate_state(_load)


def _emulator_state_mutate(name: str, mutator: Any) -> None:
    def _wrapper(state: dict[str, Any]) -> None:
        current = _avd_entries(state).get(name)
        if current is not None:
            mutator(current)

    fake_runtime.mutate_state(_wrapper)


def emulator(args: list[str]) -> int:
    if "-version" in args or "--version" in args:
        out("Android emulator version 35.1.4.0 (build_id 12345678)")
        return EXIT_OK
    if "-list-avds" in args:
        for name in sorted(_avd_entries(_state())):
            out(name)
        return EXIT_OK
    if any(token in {"-help", "--help", "-h"} for token in args):
        out(fake_runtime.env_value("AVDFLEET_FAKE_EMULATOR_USAGE", "") or EMULATOR_USAGE)
        return EXIT_OK
    options = _parse_emulator_args(args)
    name = str(options.get("avd") or "")
    if not name:
        err(EMULATOR_USAGE)
        return EXIT_USAGE
    state = _state()
    entry = _avd_entries(state).get(name)
    if entry is None:
        err(f"PANIC: Unknown AVD name [{name}], use -list-avds to see valid options")
        return EXIT_FAIL
    if entry.get("state") in {"booting", "running"} and entry.get("pid") is not None:
        err("ERROR: The emulator is already running")
        return EXIT_FAIL
    if options.get("accel") == "off":
        err("WARNING: fake emulator accepts -accel off for installer-guard tests")
    state_without_self = dict(state)
    state_without_self["avds"] = {
        other_name: other for other_name, other in _avd_entries(state).items() if other_name != name
    }
    # Bind the console first: another fake session on the same host may hold a
    # port that looked free a moment ago, so retry with the next candidate.
    requested_port = options.get("port")
    avoid: set[int] = set()
    server: _FakeEmulatorServer | None = None
    console_port = 0
    for attempt in range(8):
        candidate = fake_runtime.next_console_port(
            state_without_self, requested_port if attempt == 0 else None, avoid=avoid
        )
        try:
            server = _FakeEmulatorServer(name, candidate, _home())
        except OSError:
            avoid.add(candidate)
            continue
        console_port = candidate
        break
    if server is None:
        err("ERROR: could not bind an emulator console port")
        return EXIT_FAIL
    if requested_port is not None and console_port != requested_port:
        err(f"WARNING: console port {requested_port} unavailable, using {console_port}")
    grpc_port = options.get("grpc") or fake_runtime.grpc_port_for(console_port)
    boot_seconds = fake_runtime.boot_seconds()
    started_at = fake_runtime.now()
    proxy = options.get("proxy")
    snapshot = options.get("snapshot")
    wipe_data = bool(options.get("wipe_data"))
    launch_options = {
        "gpu": options.get("gpu"),
        "no_window": bool(options.get("no_window")),
        "http_proxy": proxy,
        "grpc": grpc_port,
        "snapshot": snapshot,
        "no_snapshot": bool(options.get("no_snapshot")),
        "wipe_data": wipe_data,
        "accel": options.get("accel"),
        "memory": options.get("memory"),
        "cores": options.get("cores"),
        "extra": list(options.get("extra") or []),
    }

    def _launch(current: dict[str, Any]) -> None:
        current["state"] = "booting"
        current["pid"] = os.getpid()
        current["console_port"] = console_port
        current["adb_port"] = console_port + 1
        current["adb_serial"] = f"emulator-{console_port}"
        current["grpc_port"] = grpc_port
        current["boot_completed"] = False
        current["boot_complete_at"] = started_at + boot_seconds
        current["started_at"] = started_at
        current["launch_options"] = launch_options
        current["no_window"] = bool(options.get("no_window"))
        current["gpu"] = options.get("gpu")
        if proxy:
            current["proxy"] = proxy
        if snapshot:
            current["snapshot"] = snapshot
            if "golden" in snapshot.lower():
                current["oobe"] = "setup"
                current["activity"] = fake_runtime.SETUP_WIZARD_ACTIVITY
                current["boot_completed"] = False
        if wipe_data:
            current["oobe"] = "setup"
            current["activity"] = fake_runtime.SETUP_WIZARD_ACTIVITY
            current["settings"] = {}
        if options.get("no_snapshot"):
            current["snapshot"] = None

    try:
        _emulator_state_mutate(name, _launch)
    except Exception as exc:  # pragma: no cover - defensive
        with contextlib.suppress(OSError):
            server.socket.close()
        err(f"PANIC: failed to update fake state: {exc}")
        return EXIT_FAIL

    fake_runtime.register_pid(os.getpid(), "emulator", avd=name)
    grpc_file = _home() / "grpc" / f"{console_port}.ini"
    grpc_file.parent.mkdir(parents=True, exist_ok=True)
    grpc_file.write_text(
        f"grpc.port={grpc_port}\ngrpc.token={fake_runtime.GRPC_TOKEN}\n",
        encoding="utf-8",
    )

    def _signal_handler(_signum: int, _frame: Any) -> None:
        server.stop_event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError, AttributeError):
            signal.signal(signum, _signal_handler)

    server.console.start()
    out("INFO: QEMU waiting for connection, starting emulator...")
    threading.Thread(target=_watch_parent, args=(server.stop_event,), daemon=True).start()

    def _still_running() -> bool:
        try:
            current = _avd_entries(_state()).get(name)
        except Exception:  # transient Windows state-file sharing error: assume still ours
            return True
        return bool(current and current.get("pid") == os.getpid() and current.get("state") != "stopped")

    booted = False
    try:
        deadline = time.monotonic() + boot_seconds
        while time.monotonic() < deadline:
            if server.stop_event.wait(0.02):
                break
            if not _still_running():
                break
        if not server.stop_event.is_set() and _still_running():
            def _mark_running(current: dict[str, Any]) -> None:
                current["state"] = "running"
                current["boot_completed"] = True
                current["boot_complete_at"] = fake_runtime.now()

            _emulator_state_mutate(name, _mark_running)
            booted = True
            out("boot completed")
        while not server.stop_event.wait(0.05):
            if not _still_running():
                break
    except KeyboardInterrupt:
        pass
    finally:
        def _mark_stopped(current: dict[str, Any]) -> None:
            if current.get("pid") == os.getpid() and current.get("state") != "stopped":
                current["state"] = "stopped"
                current["pid"] = None
                current["boot_completed"] = False
                current["boot_complete_at"] = None
                current["started_at"] = None

        with contextlib.suppress(Exception):
            _emulator_state_mutate(name, _mark_stopped)
        with contextlib.suppress(OSError):
            server.socket.close()
        with contextlib.suppress(OSError):
            grpc_file.unlink()
        fake_runtime.unregister_pid(os.getpid())
    del booted
    return EXIT_OK


# ---------------------------------------------------------------------------
# appium
# ---------------------------------------------------------------------------


def _appium_state() -> dict[str, Any]:
    state = _state()
    value = state.get("appium")
    if not isinstance(value, dict):
        value = {"running": False, "port": None, "pid": None, "sessions": []}
        state["appium"] = value
    value.setdefault("sessions", [])
    return state


class _AppiumHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeAppium/4.0.0"

    def log_message(self, *args: Any) -> None:
        return

    # -- plumbing ----------------------------------------------------------
    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def _base_path(self) -> str:
        base = getattr(self.server, "appium_base_path", "") or ""
        return base.rstrip("/")

    def _relative_path(self) -> str:
        path = self.path.split("?", 1)[0]
        base = self._base_path()
        if base and path.startswith(base):
            path = path[len(base) :] or "/"
        return path

    # -- endpoints ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self._relative_path()
        if path == "/status" or path.endswith("/status"):
            self._send_json(200, {"value": {"ready": True, "build": {"version": "4.0.0"}}})
            return
        if path.endswith("/sessions"):
            state = _appium_state()
            sessions = state["appium"].get("sessions") or []
            self._send_json(
                200,
                {"value": [{"id": item.get("id"), "capabilities": item.get("capabilities", {})} for item in sessions]},
            )
            return
        if path in {"/", "/wd/hub"}:
            self._send_json(200, {"value": {"build": {"version": "4.0.0"}}})
            return
        self._send_json(404, {"value": {"error": "unknown command", "message": path, "stacktrace": ""}})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        path = self._relative_path()
        if path in {"/session", "/sessions"}:
            body = self._body()
            capabilities: dict[str, Any] = {}
            desired = body.get("desiredCapabilities")
            if isinstance(desired, dict):
                capabilities.update(desired)
            capability_block = body.get("capabilities")
            if isinstance(capability_block, dict):
                always = capability_block.get("alwaysMatch")
                if isinstance(always, dict):
                    capabilities.update(always)
                first = capability_block.get("firstMatch")
                if isinstance(first, list) and first and isinstance(first[0], dict):
                    capabilities.update(first[0])
            session_id = ""

            def _create(state: dict[str, Any]) -> str:
                appium_state = state.setdefault("appium", {})
                sessions = appium_state.setdefault("sessions", [])
                sid = f"fake-session-{len(sessions) + 1}"
                sessions.append({"id": sid, "capabilities": capabilities})
                return sid

            session_id = str(fake_runtime.mutate_state(_create))
            self._send_json(200, {"value": {"sessionId": session_id, "capabilities": capabilities}})
            return
        self._send_json(404, {"value": {"error": "unknown command", "message": path, "stacktrace": ""}})

    def do_DELETE(self) -> None:  # noqa: N802 - http.server API
        path = self._relative_path()
        match = re.search(r"/session/([^/]+)$", path)
        if match:
            session_id = match.group(1)

            def _delete(state: dict[str, Any]) -> None:
                appium_state = state.setdefault("appium", {})
                sessions = appium_state.setdefault("sessions", [])
                appium_state["sessions"] = [item for item in sessions if item.get("id") != session_id]

            fake_runtime.mutate_state(_delete)
            self._send_json(200, {"value": None})
            return
        self._send_json(404, {"value": {"error": "unknown command", "message": path, "stacktrace": ""}})


def _appium_driver(args: list[str]) -> int:
    if not args:
        err("Usage: appium driver list|install|uninstall")
        return EXIT_USAGE
    action = args[0]
    if action == "list":
        out("Installed drivers:")
        out("uiautomator2@3.0.0 [installed (npm)]")
        return EXIT_OK
    if action == "install":
        driver = args[1] if len(args) > 1 else "uiautomator2"

        def _install(state: dict[str, Any]) -> None:
            appium = state.setdefault("appium", {})
            drivers = appium.setdefault("drivers", [])
            if driver not in drivers:
                drivers.append(driver)

        fake_runtime.mutate_state(_install)
        out(f"Driver {driver}@3.0.0 installed")
        return EXIT_OK
    if action == "uninstall":
        driver = args[1] if len(args) > 1 else "uiautomator2"

        def _uninstall(state: dict[str, Any]) -> None:
            appium = state.setdefault("appium", {})
            drivers = appium.setdefault("drivers", [])
            if driver in drivers:
                drivers.remove(driver)

        fake_runtime.mutate_state(_uninstall)
        out(f"Driver {driver} uninstalled")
        return EXIT_OK
    err(f"Unknown driver command: {action}")
    return EXIT_USAGE


def appium(args: list[str]) -> int:
    if "--version" in args or "-v" in args:
        out("4.0.0")
        return EXIT_OK
    if "--help" in args or "-h" in args:
        out("Usage: appium [--port <port>] [--base-path <path>] [--log-level <level>] | appium driver ...")
        return EXIT_OK
    if args and args[0] == "driver":
        return _appium_driver(args[1:])
    state = _appium_state()
    port = fake_runtime.appium_default_port()
    base_path = ""
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"--port", "-p"} and index + 1 < len(args):
            with contextlib.suppress(ValueError):
                port = int(args[index + 1])
            index += 2
        elif token.startswith("--port="):
            with contextlib.suppress(ValueError):
                port = int(token.split("=", 1)[1])
            index += 1
        elif token == "--base-path" and index + 1 < len(args):
            base_path = args[index + 1]
            index += 2
        elif token.startswith("--base-path="):
            base_path = token.split("=", 1)[1]
            index += 1
        else:
            index += 1
    current = state["appium"]
    if current.get("running") and current.get("pid"):
        err(f"ERROR: Appium is already running on port {current.get('port')}")
        return EXIT_FAIL
    server_class: type[ThreadingHTTPServer] = ThreadingHTTPServer
    if os.name == "nt":  # SO_REUSEADDR on Windows would allow duplicate binds
        class _ExclusiveHTTPServer(ThreadingHTTPServer):
            allow_reuse_address = False

        server_class = _ExclusiveHTTPServer
    try:
        server = server_class(("127.0.0.1", port), _AppiumHandler)
    except OSError:
        err(f"ERROR: Port {port} is already in use")
        return EXIT_FAIL
    server.daemon_threads = True
    server.appium_base_path = base_path  # type: ignore[attr-defined]

    def _startup(state_dict: dict[str, Any]) -> None:
        appium_state = state_dict.setdefault("appium", {})
        appium_state.update({"running": True, "port": port, "pid": os.getpid(), "base_path": base_path})
        appium_state.setdefault("sessions", [])

    fake_runtime.mutate_state(_startup)
    fake_runtime.register_pid(os.getpid(), "appium")
    stop_event = threading.Event()

    def _signal_handler(_signum: int, _frame: Any) -> None:
        stop_event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError, AttributeError):
            signal.signal(signum, _signal_handler)

    thread = threading.Thread(target=server.serve_forever, name=f"fake-appium-{port}", daemon=True)
    thread.start()
    threading.Thread(target=_watch_parent, args=(stop_event,), daemon=True).start()
    out("[Appium] Welcome to Appium v4.0.0 (fake)")
    out(f"Appium REST http interface listener started on http://127.0.0.1:{port}{base_path or ''}")
    try:
        while not stop_event.wait(0.05):
            try:
                latest = _appium_state().get("appium") or {}
            except Exception:  # transient Windows state-file sharing error
                continue
            if latest.get("pid") != os.getpid() or not latest.get("running"):
                break
    except KeyboardInterrupt:
        pass
    finally:
        def _stop(state_dict: dict[str, Any]) -> None:
            appium_state = state_dict.setdefault("appium", {})
            if appium_state.get("pid") == os.getpid():
                appium_state.update({"running": False, "port": None, "pid": None, "sessions": []})

        with contextlib.suppress(Exception):
            fake_runtime.mutate_state(_stop)
        with contextlib.suppress(Exception):
            server.shutdown()
        with contextlib.suppress(Exception):
            server.server_close()
        fake_runtime.unregister_pid(os.getpid())
    return EXIT_OK


# ---------------------------------------------------------------------------
# Utility fakes
# ---------------------------------------------------------------------------


def node(args: list[str]) -> int:
    if "--version" in args or "-v" in args:
        out("v22.14.0")
        return EXIT_OK
    return EXIT_OK


def npm(args: list[str]) -> int:
    if "--version" in args or "-v" in args:
        out("10.9.2")
        return EXIT_OK
    if args and args[0] in {"install", "i", "ci"}:
        out("added 1 package, and audited 1 package in 1s")
        return EXIT_OK
    if args and args[0] == "list":
        out("fake-package@1.0.0")
        return EXIT_OK
    return EXIT_OK


def npx(args: list[str]) -> int:
    if "--version" in args or "-v" in args:
        out("10.9.2")
        return EXIT_OK
    return EXIT_OK


def uv(args: list[str]) -> int:
    if "--version" in args or "-V" in args:
        out("uv 0.6.0")
        return EXIT_OK
    if args[:2] == ["tool", "install"] or args[:2] == ["tool", "upgrade"]:
        packages = [token for token in args[2:] if not token.startswith("-")]
        if packages:
            def _record(state: dict[str, Any]) -> None:
                installs = state.setdefault("tool_installs", [])
                for package in packages:
                    installs.append({"tool": "uv", "package": package})

            fake_runtime.mutate_state(_record)
            out(f"Installed {len(packages)} executable(s)")
        return EXIT_OK
    if args and args[0] in {"python", "venv", "pip", "sync", "run"}:
        return EXIT_OK
    return EXIT_OK


def java(args: list[str]) -> int:
    if "--version" in args:
        out('openjdk 21.0.6 2025-01-21')
        return EXIT_OK
    if "-version" in args:
        err('openjdk version "21.0.6" 2025-01-21')
        return EXIT_OK
    return EXIT_OK


def python_tool(args: list[str]) -> int:
    real = fake_runtime.env_value("AVDFLEET_FAKE_PYTHON") or sys.executable
    version = f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if not args:
        out(version)
        return EXIT_OK
    if args[0] in {"--version", "-V"}:
        out(version)
        return EXIT_OK
    real_path = Path(real)
    fake_home = fake_runtime.env_value("AVDFLEET_FAKE_HOME")
    fake_bin = Path(fake_home) / "bin" if fake_home else None
    try:
        in_fake_bin = fake_bin is not None and real_path.resolve().parent == fake_bin.resolve()
    except OSError:  # pragma: no cover - defensive
        in_fake_bin = False
    if in_fake_bin or not real_path.exists():
        raise ToolFailure(f"fake python: real interpreter unavailable ({real})")
    try:
        os.execv(str(real_path), [str(real_path), *args])
    except OSError as exc:  # pragma: no cover - defensive
        raise ToolFailure(f"fake python: cannot delegate to {real}: {exc}") from exc
    return EXIT_OK  # pragma: no cover - execv does not return


def scrcpy(args: list[str]) -> int:
    if "--version" in args or "-v" in args:
        out("scrcpy 3.1")
        return EXIT_OK
    return EXIT_OK


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, Any] = {
    "adb": adb,
    "emulator": emulator,
    "sdkmanager": sdkmanager,
    "avdmanager": avdmanager,
    "android": android_cli,
    "appium": appium,
    "node": node,
    "npm": npm,
    "npx": npx,
    "uv": uv,
    "java": java,
    "scrcpy": scrcpy,
    "python": python_tool,
}


def main(argv: list[str] | None = None) -> int:
    """Dispatch one fake tool invocation and return its exit code."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        tool = arguments[0].lower()
        rest = arguments[1:]
    else:
        tool = Path(sys.argv[0]).stem.lower()
        rest = []
    if tool == "python.exe":
        tool = "python"
    if not os.environ.get("AVDFLEET_FAKE_HOME"):
        err(f"{tool}: AVDFLEET_FAKE_HOME is not set")
        return EXIT_FAIL
    try:
        fake_runtime.append_event(tool, rest)
        fake_runtime.record_command(tool, rest)
    except Exception as exc:  # pragma: no cover - state must not break tools
        err(f"{tool}: cannot record fake event: {exc}")
        return EXIT_FAIL
    handler = _HANDLERS.get(tool)
    if handler is None:
        err(f"{tool}: unsupported fake tool")
        return EXIT_USAGE
    if fake_runtime.should_fail(tool):
        err(f"{tool}: injected failure (AVDFLEET_FAKE_FAIL)")
        return EXIT_FAIL
    try:
        return int(handler(rest))
    except UsageError as exc:
        err(str(exc))
        return EXIT_USAGE
    except ToolFailure as exc:
        err(str(exc))
        return EXIT_FAIL
    except BrokenPipeError:
        return EXIT_OK
    except KeyboardInterrupt:
        return 130
    except Exception:  # pragma: no cover - unexpected fake failure
        traceback.print_exc()
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
