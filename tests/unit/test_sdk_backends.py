"""Unit tests for the Android SDK tooling backends (S1a + S1b).

The tests build a throw-away fake SDK inside ``tmp_path``: executable shims
(``.cmd`` files on Windows, shell scripts elsewhere) for ``sdkmanager``,
``avdmanager``, ``adb``, ``emulator`` and the new ``android`` CLI, plus a small
Python stand-in that implements the process contract of
``contracts/fakes-spec.md`` §4.

The shims are generated here on purpose: ``tests/fakes`` is owned by another
agent, and the backends must be testable at the real process boundary without
it.  Every test is hermetic -- no ambient Android SDK, no ``PATH`` lookups, no
network.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from avdfleet.core.config import Config
from avdfleet.core.errors import CommandError, ConfigError, ConflictError, NotFoundError
from avdfleet.core.interfaces import SdkBackend
from avdfleet.core.models import ImageKind, SdkPaths
from avdfleet.platform.sdk import (
    AndroidCliBackend,
    LegacySdkBackend,
    ToolResult,
    get_backend,
)
from avdfleet.platform.sdk.base import (
    ToolNotFoundError,
    parse_avd_entries,
    parse_package_table,
)
from avdfleet.platform.sdk.legacy import parsing

# ---------------------------------------------------------------------------
# The fake tool implementation (invoked through generated shims)
# ---------------------------------------------------------------------------

FAKE_CLI_SOURCE = r'''
"""Process-boundary fake for the Android SDK tools used by SDK backend tests."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

STATE_PATH = Path(os.environ["AVDFLEET_TEST_STATE"])
SDK_ROOT = Path(os.environ.get("AVDFLEET_TEST_SDK_ROOT") or ".")
AVD_HOME = Path(os.environ.get("AVDFLEET_TEST_AVD_HOME") or ".")

CATALOGUE = [
    "cmdline-tools;latest",
    "platform-tools",
    "emulator",
    "build-tools;35.0.0",
    "platforms;android-35",
    "platforms;android-36",
    "cmdline-tools;11.0",
    "system-images;android-35;google_apis_playstore;x86_64",
    "system-images;android-35;google_apis;x86_64",
    "system-images;android-36;google_apis_playstore;x86_64",
    "system-images;android-36;google_apis;x86_64",
    "system-images;android-35;aosp_atd;x86_64",
    "system-images;android-34;google_apis_playstore;x86_64",
]

VERSIONS = {
    "cmdline-tools;latest": "11.0.0",
    "platform-tools": "35.0.2",
    "emulator": "35.1.4",
    "build-tools;35.0.0": "35.0.0",
    "platforms;android-35": "35",
    "platforms;android-36": "36",
    "cmdline-tools;11.0": "11.0",
    "system-images;android-35;google_apis_playstore;x86_64": "8",
    "system-images;android-35;google_apis;x86_64": "8",
    "system-images;android-36;google_apis_playstore;x86_64": "9",
    "system-images;android-36;google_apis;x86_64": "9",
    "system-images;android-35;aosp_atd;x86_64": "2",
    "system-images;android-34;google_apis_playstore;x86_64": "7",
}

DEVICES = [
    ("pixel_7", "Pixel 7"),
    ("pixel_6", "Pixel 6"),
    ("pixel_tablet", "Pixel Tablet"),
    ("Nexus 5X", "Nexus 5X"),
    ("pixel_9", "Pixel 9"),
]


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {"schema": 1, "installed": [], "licenses": [], "avds": {}, "events": []}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def record(state, tool, argv, stdin=""):
    state.setdefault("events", []).append({"tool": tool, "argv": list(argv), "stdin": stdin})


def read_stdin():
    try:
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""


def value_after(values, flag):
    for index, value in enumerate(values):
        if value == flag and index + 1 < len(values):
            return values[index + 1]
    return ""


def image_metadata(package):
    parts = package.split(";")
    if len(parts) != 4:
        return 35, "google_apis", "x86_64"
    target, tag, abi = parts[1], parts[2], parts[3]
    digits = "".join(ch for ch in target if ch.isdigit())
    return (int(digits) if digits else 0), tag, abi


def print_package_table(state, installed_only=False):
    installed = set(state["installed"])
    print("Installed packages:")
    print("  Path | Version | Description | Location")
    print("  ------- | ------- | ------- | -------")
    for ident in CATALOGUE:
        if ident in installed:
            location = ident.replace(";", os.sep)
            print(f"  {ident} | {VERSIONS.get(ident, '1.0.0')} | Fake package | {location}")
    if installed_only:
        return
    print()
    print("Available Packages:")
    print("  Path | Version | Description | Location")
    print("  ------- | ------- | ------- | -------")
    for ident in CATALOGUE:
        if ident not in installed:
            print(f"  {ident} | {VERSIONS.get(ident, '1.0.0')} | Fake package | ")


def install_packages(state, ids, stdin):
    if not ids:
        print("Error: no packages specified", file=sys.stderr)
        return 2
    unknown = [ident for ident in ids if ident not in CATALOGUE]
    if unknown:
        for ident in unknown:
            print(f"Warning: Failed to find package {ident}", file=sys.stderr)
        return 1
    accepted = any(line.strip().lower().startswith("y") for line in stdin.splitlines())
    if accepted:
        for license_id in ("android-sdk-license", "android-sdk-preview-license"):
            if license_id not in state["licenses"]:
                state["licenses"].append(license_id)
    for ident in ids:
        print(f'Preparing "Install {ident}".')
        print("Downloading...")
        print("Unzipping...")
        print("Installing...")
        if ident not in state["installed"]:
            state["installed"].append(ident)
        print(f'Installed "{ident}".')
    return 0


def license_review(state, stdin):
    print("Review licenses that have not been accepted (y/N)?")
    accepted = any(line.strip().lower().startswith("y") for line in stdin.splitlines())
    if not accepted:
        print("License not accepted", file=sys.stderr)
        return 1
    for license_id in ("android-sdk-license", "android-sdk-preview-license"):
        if license_id not in state["licenses"]:
            state["licenses"].append(license_id)
    print("All SDK package licenses accepted")
    return 0


def avd_block(name, avd):
    api, tag, abi = image_metadata(avd.get("package", ""))
    target = "Google Play (Google Inc.)" if "playstore" in tag else "Google APIs (Google Inc.)"
    return [
        "    Name: " + name,
        "    Path: " + str(AVD_HOME / (name + ".avd")),
        "  Target: " + target,
        "  Based on: Android API %d Tag/ABI: %s/%s" % (api, tag, abi),
    ]


def list_avds(state):
    print("Available Android Virtual Devices:")
    for name in sorted(state["avds"]):
        for line in avd_block(name, state["avds"][name]):
            print(line)
        print("---------")


def create_avd(state, name, package, device, force):
    if package not in state["installed"]:
        print("Error: Package path is not valid.", file=sys.stderr)
        return 1
    ini = AVD_HOME / (name + ".ini")
    avd_dir = AVD_HOME / (name + ".avd")
    if (name in state["avds"] or ini.exists()) and not force:
        print(f"Error: AVD '{name}' already exists.", file=sys.stderr)
        return 1
    api, tag, abi = image_metadata(package)
    AVD_HOME.mkdir(parents=True, exist_ok=True)
    avd_dir.mkdir(parents=True, exist_ok=True)
    ini.write_text(
        "avd.ini.encoding=UTF-8\npath=%s\ntarget=android-%d\n"
        % (str(avd_dir).replace(os.sep, "/"), api),
        encoding="utf-8",
    )
    config_lines = [
        "image.sysdir.1=system-images/android-%d/%s/%s/" % (api, tag, abi),
        "tag.id=" + tag,
        "abi.type=" + abi,
        "hw.device.name=" + device,
        "hw.ramSize=2048",
        "disk.dataPartition.size=6G",
        "PlayStore.enabled=" + ("true" if "playstore" in tag else "false"),
        "fastboot.forceColdBoot=yes",
    ]
    (avd_dir / "config.ini").write_text("\n".join(config_lines) + "\n", encoding="utf-8")
    state["avds"][name] = {
        "package": package,
        "device": device,
        "path": str(avd_dir),
        "state": "stopped",
        "api": api,
        "abi": abi,
    }
    print("The AVD '%s' was created successfully." % name)
    return 0


def delete_avd(state, name, force=False):
    ini = AVD_HOME / (name + ".ini")
    avd_dir = AVD_HOME / (name + ".avd")
    if name not in state["avds"] and not ini.exists():
        print("Error: AVD '%s' does not exist." % name, file=sys.stderr)
        return 1
    state["avds"].pop(name, None)
    for path in (ini, avd_dir / "config.ini"):
        try:
            path.unlink()
        except OSError:
            pass
    try:
        avd_dir.rmdir()
    except OSError:
        pass
    print("AVD '%s' deleted." % name)
    return 0


def console_ports(state):
    ports = {}
    running = [
        name for name, avd in state["avds"].items() if avd.get("state") in ("running", "booting")
    ]
    for index, name in enumerate(sorted(state["avds"])):
        if name in running:
            ports[name] = 5554 + 2 * index
    return ports


def run_sdkmanager(state, args):
    if "--version" in args:
        print("11.0.0")
        return 0
    if "--list_installed" in args:
        print_package_table(state, installed_only=True)
        return 0
    if "--list" in args:
        print_package_table(state)
        return 0
    if "--install" in args:
        index = args.index("--install")
        ids = [arg for arg in args[index + 1:] if not arg.startswith("--")]
        stdin = read_stdin()
        record(state, "sdkmanager", args, stdin)
        return install_packages(state, ids, stdin)
    if "--uninstall" in args:
        index = args.index("--uninstall")
        ids = [arg for arg in args[index + 1:] if not arg.startswith("--")]
        for ident in ids:
            if ident in state["installed"]:
                state["installed"].remove(ident)
            print("Uninstalled " + ident)
        return 0
    if "--licenses" in args:
        stdin = read_stdin()
        record(state, "sdkmanager", args, stdin)
        return license_review(state, stdin)
    print("Error: unsupported sdkmanager arguments", file=sys.stderr)
    return 2


def run_avdmanager(state, args):
    if "--version" in args:
        print("11.0.0")
        return 0
    if args[:2] == ["list", "avd"]:
        list_avds(state)
        return 0
    if args[:2] == ["list", "device"]:
        for index, (identifier, name) in enumerate(DEVICES):
            print('id: %d or "%s"' % (index, identifier))
            print("    Name: " + name)
            print("    Type: Phone")
            print("---------")
        return 0
    if args[:2] == ["create", "avd"]:
        name = value_after(args, "-n")
        package = value_after(args, "-k")
        device = value_after(args, "-d") or "pixel_7"
        force = "--force" in args
        stdin = read_stdin()
        record(state, "avdmanager", args, stdin)
        if not name or not package:
            print("Error: --name/--package required", file=sys.stderr)
            return 2
        return create_avd(state, name, package, device, force)
    if args[:2] == ["delete", "avd"]:
        name = value_after(args, "-n")
        if not name:
            print("Error: --name required", file=sys.stderr)
            return 2
        return delete_avd(state, name)
    print("Error: unsupported avdmanager arguments", file=sys.stderr)
    return 2


def run_adb(state, args):
    select = None
    remaining = []
    index = 0
    while index < len(args):
        if args[index] == "-s" and index + 1 < len(args):
            select = args[index + 1]
            index += 2
            continue
        remaining.append(args[index])
        index += 1
    if not remaining:
        return 2
    command, rest = remaining[0], remaining[1:]
    selected_name = None
    for name, port in console_ports(state).items():
        if select == "emulator-%d" % port:
            selected_name = name
    if command == "version":
        print("Android Debug Bridge version 1.0.41")
        print("Version 35.0.2-android-tools")
        return 0
    if command in ("start-server", "kill-server"):
        return 0
    if command == "devices":
        print("List of devices attached")
        for name, port in sorted(console_ports(state).items()):
            print(
                "emulator-%d\tdevice product:sdk_gphone64_x86_64 "
                "model:sdk_gphone64_x86_64 device:emu64x transport_id:1" % port
            )
        return 0
    if command == "wait-for-device":
        return 0
    if command == "get-state":
        print("device")
        return 0
    if command == "emu":
        sub = rest[0] if rest else ""
        if sub == "kill":
            if selected_name:
                state["avds"][selected_name]["state"] = "stopped"
            return 0
        if sub == "avd" and len(rest) >= 2 and rest[1] == "name":
            print(selected_name or "")
            return 0
        return 0
    if command == "shell":
        if rest and rest[0] == "getprop":
            prop = rest[1] if len(rest) > 1 else ""
            if prop == "sys.boot_completed":
                print("1")
            elif prop == "ro.build.version.sdk":
                print("35")
            elif prop == "ro.product.cpu.abi":
                print("x86_64")
            else:
                print("")
            return 0
        if rest and rest[0] == "echo":
            print(" ".join(rest[1:]))
            return 0
        print("<fake-adb-shell>: " + " ".join(rest))
        return 0
    print("fake adb: unhandled " + command, file=sys.stderr)
    return 0


def run_emulator(state, args):
    if "-version" in args:
        print("Android emulator version 35.1.4.0 (build_id 12345678)")
        return 0
    if "-list-avds" in args:
        for name in sorted(state["avds"]):
            print(name)
        return 0
    print("INFO: fake emulator launch not used by these tests")
    return 0


def run_android(state, args):
    if not args:
        return 2
    command, rest = args[0], args[1:]
    json_enabled = os.environ.get("AVDFLEET_TEST_ANDROID_JSON", "1") != "0"
    if command == "--version":
        print(os.environ.get("AVDFLEET_TEST_ANDROID_VERSION", "android 1.0.0 (fake)"))
        return 0
    if command == "sdk":
        sub = rest[0] if rest else ""
        flags = rest[1:]
        installed_only = "--installed" in flags
        if sub == "list":
            if "--json" in flags:
                if not json_enabled:
                    print("Error: unsupported option --json", file=sys.stderr)
                    return 2
                installed = set(state["installed"])
                payload = [
                    {
                        "id": ident,
                        "version": VERSIONS.get(ident, "1.0.0"),
                        "installed": ident in installed,
                    }
                    for ident in CATALOGUE
                    if not installed_only or ident in installed
                ]
                print(json.dumps(payload))
                return 0
            print_package_table(state, installed_only=installed_only)
            return 0
        if sub == "install":
            ids = [flag for flag in flags if not flag.startswith("--")]
            stdin = read_stdin()
            record(state, "android", args, stdin)
            return install_packages(state, ids, stdin)
        if sub == "licenses":
            stdin = read_stdin()
            record(state, "android", args, stdin)
            return license_review(state, stdin)
        if sub in ("remove", "uninstall"):
            if os.environ.get("AVDFLEET_TEST_ANDROID_UNINSTALL", "1") == "0":
                print("Error: unsupported subcommand 'sdk " + sub + "'", file=sys.stderr)
                return 2
            for ident in [flag for flag in flags if not flag.startswith("--")]:
                if ident in state["installed"]:
                    state["installed"].remove(ident)
                print("Uninstalled " + ident)
            return 0
        print("Error: unknown sdk subcommand", file=sys.stderr)
        return 2
    if command == "avd":
        sub = rest[0] if rest else ""
        flags = rest[1:]
        if sub == "list":
            if "--json" in flags:
                if not json_enabled:
                    print("Error: unsupported option --json", file=sys.stderr)
                    return 2
                payload = []
                for name in sorted(state["avds"]):
                    avd = state["avds"][name]
                    api, tag, abi = image_metadata(avd.get("package", ""))
                    payload.append(
                        {
                            "name": name,
                            "package": avd.get("package", ""),
                            "device": avd.get("device", ""),
                            "api": api,
                            "tag": tag,
                            "abi": abi,
                            "path": str(AVD_HOME / (name + ".avd")),
                        }
                    )
                print(json.dumps(payload))
                return 0
            list_avds(state)
            return 0
        if sub == "create":
            name = value_after(flags, "--name")
            package = value_after(flags, "--package")
            device = value_after(flags, "--device") or "pixel_7"
            force = "--force" in flags
            stdin = read_stdin()
            record(state, "android", args, stdin)
            if not name or not package:
                print("Error: --name and --package are required", file=sys.stderr)
                return 2
            return create_avd(state, name, package, device, force)
        if sub == "delete":
            name = value_after(flags, "--name")
            force = "--force" in flags
            if not name:
                print("Error: --name is required", file=sys.stderr)
                return 2
            return delete_avd(state, name, force=force)
        print("Error: unknown avd subcommand", file=sys.stderr)
        return 2
    if command == "emulator" and rest and rest[0] == "list":
        ports = console_ports(state)
        payload = [
            {
                "name": name,
                "consolePort": port,
                "serial": "emulator-%d" % port,
                "state": state["avds"][name].get("state", "running"),
            }
            for name, port in sorted(ports.items())
        ]
        print(json.dumps(payload))
        return 0
    print("Error: unsupported subcommand " + " ".join(args), file=sys.stderr)
    return 2


def main(argv):
    if len(argv) < 2:
        return 2
    tool, args = argv[1], argv[2:]
    state = load_state()
    record(state, tool, args)
    sleep_for = float(os.environ.get("AVDFLEET_TEST_SLEEP", "0") or 0)
    if sleep_for:
        time.sleep(sleep_for)
    failures = [item.strip() for item in os.environ.get("AVDFLEET_TEST_FAIL", "").split(",") if item.strip()]
    if tool in failures or any((tool + ":" + arg) in failures for arg in args):
        print("Error: forced failure for " + tool, file=sys.stderr)
        save_state(state)
        return 1
    if tool == "sdkmanager":
        code = run_sdkmanager(state, args)
    elif tool == "avdmanager":
        code = run_avdmanager(state, args)
    elif tool == "adb":
        code = run_adb(state, args)
    elif tool == "emulator":
        code = run_emulator(state, args)
    elif tool == "android":
        code = run_android(state, args)
    else:
        print("unknown fake tool " + tool, file=sys.stderr)
        code = 2
    save_state(state)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
'''


# ---------------------------------------------------------------------------
# Fake SDK layout / shims
# ---------------------------------------------------------------------------

DEFAULT_CATALOGUE = [
    "cmdline-tools;latest",
    "platform-tools",
    "emulator",
    "build-tools;35.0.0",
    "platforms;android-35",
    "platforms;android-36",
    "cmdline-tools;11.0",
    "system-images;android-35;google_apis_playstore;x86_64",
    "system-images;android-35;google_apis;x86_64",
    "system-images;android-36;google_apis_playstore;x86_64",
    "system-images;android-36;google_apis;x86_64",
    "system-images;android-35;aosp_atd;x86_64",
    "system-images;android-34;google_apis_playstore;x86_64",
]

DEFAULT_INSTALLED = [
    "cmdline-tools;latest",
    "platform-tools",
    "emulator",
    "build-tools;35.0.0",
    "platforms;android-35",
    "system-images;android-35;google_apis_playstore;x86_64",
]

PLAY_PACKAGE = "system-images;android-35;google_apis_playstore;x86_64"
GOOGLE_PACKAGE = "system-images;android-35;google_apis;x86_64"


@dataclass
class FakeSdk:
    """Paths and environment of one generated fake SDK."""

    root: Path
    sdk_root: Path
    avd_home: Path
    state_path: Path
    environ: dict[str, str]

    @property
    def paths(self) -> SdkPaths:
        return SdkPaths(
            sdk_root=str(self.sdk_root),
            avd_home=str(self.avd_home),
            cmdline_tools=str(self.sdk_root / "cmdline-tools" / "latest"),
            platform_tools=str(self.sdk_root / "platform-tools"),
            emulator=str(self.sdk_root / "emulator"),
        )

    def backend_kind(self, kind: str, **env_overrides: str):
        environ = {**self.environ, **env_overrides}
        if kind == "legacy":
            return LegacySdkBackend(sdk_paths=self.paths, environ=environ)
        if kind == "androidcli":
            return AndroidCliBackend(sdk_paths=self.paths, environ=environ)
        raise AssertionError(f"unknown backend kind {kind!r}")

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def events(self, tool: str | None = None) -> list[dict[str, Any]]:
        events = self.state().get("events", [])
        if tool is None:
            return events
        return [event for event in events if event.get("tool") == tool]

    def stdin_seen(self, tool: str) -> str:
        return "\n".join(event.get("stdin", "") for event in self.events(tool))


def _write_shim(bin_dir: Path, tool: str, cli_path: Path) -> None:
    """Create an executable shim; ``-S`` keeps the fake's startup cheap."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        shim = bin_dir / f"{tool}.cmd"
        shim.write_text(
            f'@echo off\r\nREM avdfleet test shim\r\n"{sys.executable}" -S "{cli_path}" {tool} %*\r\n',
            encoding="ascii",
        )
    else:
        shim = bin_dir / tool
        shim.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" -S "{cli_path}" {tool} "$@"\n',
            encoding="ascii",
        )
        shim.chmod(0o755)


def build_fake_sdk(
    tmp_path: Path,
    *,
    android: bool = True,
    sdkmanager: bool = True,
    installed: list[str] | None = None,
    avds: dict[str, dict[str, Any]] | None = None,
    images_on_disk: list[str] | None = None,
) -> FakeSdk:
    root = tmp_path / "fake"
    sdk_root = root / "sdk"
    avd_home = root / "avd"
    cmdline_bin = sdk_root / "cmdline-tools" / "latest" / "bin"
    platform_tools = sdk_root / "platform-tools"
    emulator_dir = sdk_root / "emulator"
    for directory in (cmdline_bin, platform_tools, emulator_dir, avd_home):
        directory.mkdir(parents=True, exist_ok=True)
    cli_path = root / "fake_cli.py"
    cli_path.write_text(FAKE_CLI_SOURCE, encoding="utf-8")
    if sdkmanager:
        _write_shim(cmdline_bin, "sdkmanager", cli_path)
    _write_shim(cmdline_bin, "avdmanager", cli_path)
    _write_shim(platform_tools, "adb", cli_path)
    _write_shim(emulator_dir, "emulator", cli_path)
    if android:
        _write_shim(cmdline_bin, "android", cli_path)

    state_path = root / "state.json"
    state = {
        "schema": 1,
        "installed": list(DEFAULT_INSTALLED if installed is None else installed),
        "licenses": [],
        "avds": dict(avds or {}),
        "events": [],
    }
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    for package in (
        images_on_disk
        if images_on_disk is not None
        else [PLAY_PACKAGE, "system-images;android-33;google_apis;x86_64"]
    ):
        parts = package.split(";")
        if len(parts) != 4:
            continue
        image_dir = sdk_root / "system-images" / parts[1] / parts[2] / parts[3]
        image_dir.mkdir(parents=True, exist_ok=True)
        (image_dir / "system.img").write_bytes(b"fake-system-image")

    empty_bin = root / "empty-bin"
    empty_bin.mkdir(exist_ok=True)
    environ = {
        "AVDFLEET_TEST_STATE": str(state_path),
        "AVDFLEET_TEST_SDK_ROOT": str(sdk_root),
        "AVDFLEET_TEST_AVD_HOME": str(avd_home),
        "AVDFLEET_TEST_ANDROID_JSON": "1",
        "PATH": str(empty_bin),
    }
    return FakeSdk(root=root, sdk_root=sdk_root, avd_home=avd_home, state_path=state_path, environ=environ)


def seed_avd(name: str = "avd-01", *, state: str = "running", console_port: int = 5554) -> dict[str, Any]:
    return {
        "name": name,
        "package": PLAY_PACKAGE,
        "device": "pixel_7",
        "api": 35,
        "abi": "x86_64",
        "state": state,
        "console_port": console_port,
    }


def remove_shim(fake: FakeSdk, tool: str) -> None:
    """Delete a generated tool shim regardless of the platform suffix used."""
    for directory in (
        fake.sdk_root / "cmdline-tools" / "latest" / "bin",
        fake.sdk_root / "platform-tools",
        fake.sdk_root / "emulator",
    ):
        for suffix in ("", ".cmd", ".bat", ".exe"):
            candidate = directory / (tool + suffix)
            try:
                if candidate.is_file():
                    candidate.unlink()
            except OSError:
                pass


@pytest.fixture()
def fake_sdk(tmp_path: Path) -> FakeSdk:
    return build_fake_sdk(tmp_path, avds={"avd-01": seed_avd()})


@pytest.fixture(params=["legacy", "androidcli"])
def backend(request: pytest.FixtureRequest, fake_sdk: FakeSdk):
    """Same protocol tests run against both toolchains."""
    return fake_sdk.backend_kind(request.param)


# ---------------------------------------------------------------------------
# Parsing fixtures (no subprocesses)
# ---------------------------------------------------------------------------

SDKMANAGER_LIST_FIXTURE = """
Loading package information...                                                   | 0% Downloading...
Installed packages:
  Path | Version | Description | Location
  ------- | ------- | ------- | -------
  build-tools;35.0.0 | 35.0.0 | Android SDK Build-Tools 35 | build-tools\\35.0.0
  emulator | 35.1.4 | Android Emulator | emulator
  platform-tools | 35.0.2 | Android SDK Platform-Tools | platform-tools
  platforms;android-35 | 35 | Android SDK Platform 35 | platforms\\android-35

Available Packages:
  Path | Version | Description | Location
  ------- | ------- | ------- | -------
  system-images;android-35;google_apis_playstore;x86_64 | 8 | Google Play Intel x86_64 | system-images
  system-images;android-35;google_apis;x86_64 | 8 | Google APIs Intel x86_64 | system-images
"""

AVDMANAGER_LIST_FIXTURE = """
Available Android Virtual Devices:
    Name: avd-01
    Path: C:\\Users\\tester\\.android\\avd\\avd-01.avd
  Target: Google Play (Google Inc.)
Based on: Android API 35 Tag/ABI: google_apis_playstore/x86_64
---------
    Name: avd-02
    Path: /home/tester/.android/avd/avd-02.avd
  Target: Google APIs (Google Inc.)
Based on: Android 15 Tag/ABI: google_apis/arm64-v8a
---------
"""


def test_parse_sdkmanager_table_fixture() -> None:
    installed, available = parse_package_table(SDKMANAGER_LIST_FIXTURE)
    assert [package.id for package in installed] == [
        "build-tools;35.0.0",
        "emulator",
        "platform-tools",
        "platforms;android-35",
    ]
    assert all(package.installed for package in installed)
    assert "system-images;android-35;google_apis_playstore;x86_64" in {
        package.id for package in available
    }
    assert available[0].version == "8"
    parsed_install, parsed_available = parsing.parse_sdkmanager_list(SDKMANAGER_LIST_FIXTURE)
    assert (parsed_install, parsed_available) == (installed, available)


def test_parse_avdmanager_list_fixture() -> None:
    entries = parse_avd_entries(AVDMANAGER_LIST_FIXTURE)
    assert [entry.name for entry in entries] == ["avd-01", "avd-02"]
    assert entries[0].api_level == 35
    assert entries[0].tag == "google_apis_playstore"
    assert entries[0].abi == "x86_64"
    assert entries[1].tag == "google_apis"
    assert entries[1].abi == "arm64-v8a"
    assert entries[1].image_package == "system-images;android-15;google_apis;arm64-v8a"


def test_parse_device_profiles_fixture() -> None:
    fixture = """
id: 7 or "pixel_7"
    Name: Pixel 7
    Type: Phone
---------
id: pixel_tablet
    Name: Pixel Tablet
    Type: Tablet
---------
"""
    profiles = parsing.parse_avdmanager_devices(fixture)
    assert [profile["id"] for profile in profiles] == ["pixel_7", "pixel_tablet"]
    assert profiles[0]["name"] == "Pixel 7"


def test_parse_versions_and_install_classification() -> None:
    assert parsing.parse_sdkmanager_version("11.0.0\n") == "11.0.0"
    assert parsing.parse_emulator_version("Android emulator version 35.1.4.0 (build_id 1)") == "35.1.4.0"
    assert parsing.parse_adb_version("Android Debug Bridge version 1.0.41") == "1.0.41"
    assert parsing.parse_emulator_avd_list("INFO: booting\navd-01\navd-02\n") == ["avd-01", "avd-02"]

    good = ToolResult(argv=["sdkmanager"], returncode=0, stdout="Installed system-images;x")
    bad = ToolResult(
        argv=["sdkmanager"],
        returncode=1,
        stderr="Warning: Failed to find package system-images;bogus",
    )
    license = ToolResult(
        argv=["sdkmanager"],
        returncode=1,
        stderr="Review licenses that have not been accepted (y/N)?",
    )
    assert parsing.install_failed(good) is False
    assert parsing.install_failed(bad) is True
    assert parsing.unknown_package(bad) is True
    assert parsing.license_required(license) is True
    assert parsing.licenses_accepted(
        ToolResult(argv=[], returncode=0, stdout="All SDK package licenses accepted")
    )
    duplicate = ToolResult(argv=[], returncode=1, stderr="Error: AVD 'avd-01' already exists.")
    missing = ToolResult(argv=[], returncode=1, stderr="Error: AVD 'nope' does not exist.")
    assert parsing.duplicate_avd(duplicate) is True
    assert parsing.missing_avd(missing) is True


def test_parse_adb_devices_fixture() -> None:
    fixture = (
        "List of devices attached\n"
        "emulator-5554\tdevice product:sdk_gphone64_x86_64 model:sdk_gphone64_x86_64 "
        "device:emu64x transport_id:1\n"
        "emulator-5556\toffline\n"
    )
    devices = parsing.parse_adb_devices(fixture)
    assert [device.serial for device in devices] == ["emulator-5554", "emulator-5556"]
    assert devices[0].console_port == 5554
    assert devices[0].adb_port == 5555
    assert devices[0].is_online is True
    assert devices[1].is_online is False


def test_androidcli_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    from avdfleet.platform.sdk.androidcli import parsing as android_parsing

    assert android_parsing.parse_android_version("android 1.0.0 (fake)") == "1.0.0"
    assert android_parsing.parse_android_version("android 1.2.3\n") == "1.2.3"

    payload = [
        {"name": "avd-01", "package": PLAY_PACKAGE, "device": "pixel_7"},
        {"name": "avd-02", "api": 36, "tag": "google_apis", "abi": "x86_64"},
    ]
    entries = android_parsing.parse_avd_list_output("noise\n" + json.dumps(payload))
    assert [entry.name for entry in entries] == ["avd-01", "avd-02"]
    assert entries[0].api_level == 35
    assert entries[0].tag == "google_apis_playstore"
    assert entries[1].image_package == "system-images;android-36;google_apis;x86_64"

    table = android_parsing.parse_avd_list_output(AVDMANAGER_LIST_FIXTURE)
    assert [entry.name for entry in table] == ["avd-01", "avd-02"]

    packages_payload = {"installed": [{"id": "emulator", "version": "35.1.4"}], "available": ["platform-tools"]}
    installed, available = android_parsing.parse_sdk_list_output(json.dumps(packages_payload))
    assert [package.id for package in installed] == ["emulator"]
    assert [package.id for package in available] == ["platform-tools"]

    emulators = android_parsing.parse_emulator_list_output(
        json.dumps([{"name": "avd-01", "consolePort": 5554}])
    )
    assert emulators[0]["name"] == "avd-01"


# ---------------------------------------------------------------------------
# Both backends: packages, images, install, AVD lifecycle
# ---------------------------------------------------------------------------


def test_protocol_conformance(backend) -> None:
    assert isinstance(backend, SdkBackend)
    assert backend.name in {"legacy", "androidcli"}
    assert isinstance(backend.fallback_reason, str)


def test_list_packages_merges_installed_flag(backend) -> None:
    packages = backend.list_packages()
    ids = [package.id for package in packages]
    assert ids == sorted(ids)
    by_id = {package.id: package for package in packages}
    assert by_id["emulator"].installed is True
    assert by_id["platforms;android-36"].installed is False
    assert by_id[PLAY_PACKAGE].installed is True


def test_list_installed(backend) -> None:
    installed = backend.list_installed()
    assert {package.id for package in installed} == set(DEFAULT_INSTALLED)
    assert all(package.installed for package in installed)


def test_system_images_merge_and_kinds(backend) -> None:
    images = backend.list_system_images()
    by_id = {image.id: image for image in images}
    play = by_id[PLAY_PACKAGE]
    assert play.kind is ImageKind.GOOGLE_APIS_PLAYSTORE
    assert play.api_level == 35
    assert play.tag == "google_apis_playstore"
    assert play.abi == "x86_64"
    assert play.installed is True
    assert play.local_path is not None and Path(play.local_path).is_dir()
    assert by_id[GOOGLE_PACKAGE].kind is ImageKind.GOOGLE_APIS
    assert by_id["system-images;android-35;aosp_atd;x86_64"].kind is ImageKind.AOSP
    # Installed on disk but missing from the package table -> still catalogued.
    scanned = by_id["system-images;android-33;google_apis;x86_64"]
    assert scanned.installed is True
    assert scanned.local_path is not None and Path(scanned.local_path).is_dir()
    # In the catalogue but neither installed nor on disk -> reported as not installed.
    assert by_id["system-images;android-34;google_apis_playstore;x86_64"].installed is False
    # Deterministic ordering, stable across calls.
    assert [image.id for image in images] == [image.id for image in backend.list_system_images()]
    assert images == sorted(images, key=lambda image: (image.api_level, image.tag, image.abi, image.id))


def test_install_packages_dry_run_does_not_execute(backend, fake_sdk: FakeSdk) -> None:
    result = backend.install_packages([GOOGLE_PACKAGE], dry_run=True)
    assert result.ok is True
    assert result.data["dry_run"] is True
    assert result.data["package_ids"] == [GOOGLE_PACKAGE]
    assert "install" in " ".join(result.data["argv"])
    assert fake_sdk.events("sdkmanager") == [] or all(
        "--install" not in event["argv"] for event in fake_sdk.events("sdkmanager")
    )
    assert GOOGLE_PACKAGE not in fake_sdk.state()["installed"]


def test_install_packages_pipes_licenses_and_updates_state(backend, fake_sdk: FakeSdk) -> None:
    result = backend.install_packages([GOOGLE_PACKAGE], accept_licenses=True)
    assert result.ok is True, result.message
    state = fake_sdk.state()
    assert GOOGLE_PACKAGE in state["installed"]
    assert "android-sdk-license" in state["licenses"]
    tool = "sdkmanager" if backend.name == "legacy" else "android"
    assert "y" in fake_sdk.stdin_seen(tool)


def test_install_packages_unknown_id_reports_failure(backend) -> None:
    result = backend.install_packages(["system-images;android-99;bogus;x86_64"])
    assert result.ok is False
    assert "package" in result.message.lower()


def test_install_packages_empty_is_noop(backend) -> None:
    result = backend.install_packages([])
    assert result.ok is True
    assert result.data["package_ids"] == []


def test_uninstall_packages(backend, fake_sdk: FakeSdk) -> None:
    backend.install_packages([GOOGLE_PACKAGE], accept_licenses=True)
    assert GOOGLE_PACKAGE in fake_sdk.state()["installed"]
    result = backend.uninstall_packages([GOOGLE_PACKAGE])
    assert result.ok is True, result.message
    assert GOOGLE_PACKAGE not in fake_sdk.state()["installed"]


def test_uninstall_packages_dry_run(backend, fake_sdk: FakeSdk) -> None:
    result = backend.uninstall_packages([PLAY_PACKAGE], dry_run=True)
    assert result.ok is True
    assert result.data["dry_run"] is True
    assert PLAY_PACKAGE in fake_sdk.state()["installed"]


def test_accept_licenses(backend, fake_sdk: FakeSdk) -> None:
    result = backend.accept_licenses()
    assert result.ok is True, result.message
    assert "android-sdk-license" in fake_sdk.state()["licenses"]


def test_list_avds(backend, fake_sdk: FakeSdk) -> None:
    assert backend.list_avds() == ["avd-01"]


def test_create_avd_returns_path_and_writes_config(backend, fake_sdk: FakeSdk) -> None:
    created = backend.create_avd(
        "avd-new",
        PLAY_PACKAGE,
        "pixel_6",
        config={"hw.ramSize": "3072", "disk.dataPartition.size": "8G"},
    )
    created_path = Path(created)
    assert created_path.name == "avd-new.avd"
    assert created_path.parent == fake_sdk.avd_home
    assert (fake_sdk.avd_home / "avd-new.ini").is_file()
    config = backend.read_avd_config("avd-new")
    assert config["hw.ramSize"] == "3072"
    assert config["disk.dataPartition.size"] == "8G"
    assert config["hw.device.name"] == "pixel_6"
    tool = "avdmanager" if backend.name == "legacy" else "android"
    assert "no" in fake_sdk.stdin_seen(tool)
    assert "avd-new" in backend.list_avds()


def test_create_avd_duplicate_raises_conflict(backend) -> None:
    backend.create_avd("avd-dup", PLAY_PACKAGE, "pixel_7")
    with pytest.raises(ConflictError):
        backend.create_avd("avd-dup", PLAY_PACKAGE, "pixel_7")


def test_create_avd_force_recreates(backend, fake_sdk: FakeSdk) -> None:
    backend.create_avd("avd-force", PLAY_PACKAGE, "pixel_7")
    config_path = fake_sdk.avd_home / "avd-force.avd" / "config.ini"
    config_path.write_text("hw.ramSize=1\n", encoding="utf-8")
    created = backend.create_avd("avd-force", PLAY_PACKAGE, "pixel_7", force=True)
    assert Path(created).name == "avd-force.avd"
    assert backend.read_avd_config("avd-force")["hw.ramSize"] == "2048"


def test_create_avd_requires_installed_image(backend) -> None:
    with pytest.raises(CommandError):
        backend.create_avd("avd-missing-image", GOOGLE_PACKAGE, "pixel_7")


def test_delete_avd(backend) -> None:
    backend.create_avd("avd-gone", PLAY_PACKAGE, "pixel_7")
    result = backend.delete_avd("avd-gone", force=True)
    assert result.ok is True
    assert "avd-gone" not in backend.list_avds()


def test_delete_missing_avd_raises_and_force_is_idempotent(backend) -> None:
    with pytest.raises(NotFoundError):
        backend.delete_avd("avd-absent")
    forced = backend.delete_avd("avd-absent", force=True)
    assert forced.ok is True


def test_environment_dict(backend, fake_sdk: FakeSdk) -> None:
    environment = backend.environment()
    assert environment["ANDROID_SDK_ROOT"] == str(fake_sdk.sdk_root)
    assert environment["ANDROID_HOME"] == str(fake_sdk.sdk_root)
    assert environment["ANDROID_AVD_HOME"] == str(fake_sdk.avd_home)
    assert str(fake_sdk.sdk_root / "platform-tools") in environment["PATH"]


def test_emulator_launch_argv(backend) -> None:
    argv = backend.build_launch_argv(
        "avd-01",
        port=5554,
        gpu_mode="swiftshader_indirect",
        headless=True,
        proxy_url="http://user:secret@127.0.0.1:8081",
        snapshot="avdfleet-golden",
        grpc_port=8554,
    )
    assert argv[1:3] == ["-avd", "avd-01"]
    assert argv[3:5] == ["-port", "5554"]
    assert "-no-window" in argv
    assert argv[argv.index("-gpu"): argv.index("-gpu") + 2] == ["-gpu", "swiftshader_indirect"]
    assert "-http-proxy" in argv
    assert argv[argv.index("-snapshot"): argv.index("-snapshot") + 2] == [
        "-snapshot",
        "avdfleet-golden",
    ]
    assert argv[argv.index("-grpc"): argv.index("-grpc") + 2] == ["-grpc", "8554"]
    with pytest.raises(ValueError):
        backend.build_launch_argv("avd-01", port=5555)
    with pytest.raises(ValueError):
        backend.build_launch_argv("avd-01", snapshot="x", wipe_data=True)


def test_adb_helpers(backend, fake_sdk: FakeSdk) -> None:
    devices = backend.adb_devices()
    assert [device.serial for device in devices] == ["emulator-5554"]
    assert devices[0].state == "device"
    assert backend.wait_for_device("emulator-5554") == "device"
    assert backend.adb_avd_name(serial="emulator-5554") == "avd-01"
    assert backend.stop_emulator(serial="emulator-5554").ok is True
    assert fake_sdk.state()["avds"]["avd-01"]["state"] == "stopped"


# ---------------------------------------------------------------------------
# Legacy-specific behaviour
# ---------------------------------------------------------------------------


def test_legacy_tool_versions(fake_sdk: FakeSdk) -> None:
    backend = fake_sdk.backend_kind("legacy")
    versions = backend.tool_versions()
    assert versions["sdkmanager"] == "11.0.0"
    assert versions["avdmanager"] == "11.0.0"
    assert versions["adb"] == "1.0.41"
    assert versions["emulator"] == "35.1.4.0"


def test_legacy_device_profiles(fake_sdk: FakeSdk) -> None:
    backend = fake_sdk.backend_kind("legacy")
    profiles = {profile.id: profile for profile in backend.list_device_profiles()}
    assert {"pixel_7", "pixel_6", "pixel_tablet", "pixel_9"} <= set(profiles)
    assert profiles["pixel_7"].name == "Pixel 7"


def test_legacy_device_profiles_fallback_is_builtin(fake_sdk: FakeSdk) -> None:
    backend = fake_sdk.backend_kind("legacy")
    remove_shim(fake_sdk, "avdmanager")
    profiles = {profile.id for profile in backend.list_device_profiles()}
    assert "pixel_7" in profiles


def test_legacy_list_avds_falls_back_to_emulator(fake_sdk: FakeSdk) -> None:
    backend = fake_sdk.backend_kind("legacy")
    remove_shim(fake_sdk, "avdmanager")
    assert backend.list_avds() == ["avd-01"]


def test_legacy_missing_sdkmanager_raises(fake_sdk: FakeSdk) -> None:
    backend = fake_sdk.backend_kind("legacy")
    remove_shim(fake_sdk, "sdkmanager")
    with pytest.raises(ToolNotFoundError):
        backend.list_packages()


def test_legacy_install_timeout_reports_failure(fake_sdk: FakeSdk) -> None:
    backend = fake_sdk.backend_kind("legacy", AVDFLEET_TEST_SLEEP="5")
    result = backend.install_packages([GOOGLE_PACKAGE], accept_licenses=True, timeout_s=1)
    assert result.ok is False
    assert "timed out" in result.message.lower()
    assert result.data["timed_out"] is True


def test_legacy_install_failure_is_reported_not_raised(fake_sdk: FakeSdk) -> None:
    backend = fake_sdk.backend_kind("legacy", AVDFLEET_TEST_FAIL="sdkmanager:--install")
    result = backend.install_packages([GOOGLE_PACKAGE], accept_licenses=True)
    assert result.ok is False
    assert "failure" in result.message.lower() or "exit code" in result.message.lower()


def test_legacy_path_based_resolution(fake_sdk: FakeSdk) -> None:
    """When SdkPaths are empty, tools are discovered on PATH."""
    environ = dict(fake_sdk.environ)
    environ["PATH"] = os.pathsep.join(
        [
            str(fake_sdk.sdk_root / "cmdline-tools" / "latest" / "bin"),
            str(fake_sdk.sdk_root / "platform-tools"),
        ]
    )
    backend = LegacySdkBackend(sdk_paths=SdkPaths(), environ=environ)
    assert backend.sdk_paths.sdk_root == ""
    assert backend.executable("adb").name.startswith("adb")
    assert backend.executable("sdkmanager").name.startswith("sdkmanager")


# ---------------------------------------------------------------------------
# Android CLI specific behaviour
# ---------------------------------------------------------------------------


def test_androidcli_available_and_version(fake_sdk: FakeSdk) -> None:
    backend = fake_sdk.backend_kind("androidcli")
    assert backend.available() is True
    assert backend.version() == "1.0.0"
    assert backend.tool_versions()["android"] == "1.0.0"
    assert backend.supports("avd_create") is True
    assert backend.supports("not-a-feature") is False


def test_androidcli_missing_binary_is_unavailable(tmp_path: Path) -> None:
    fake = build_fake_sdk(tmp_path, android=False)
    backend = fake.backend_kind("androidcli")
    assert backend.available() is False
    assert "android" in backend.unavailable_reason.lower()
    with pytest.raises(ToolNotFoundError):
        backend.list_packages()


def test_androidcli_unsupported_version_is_unavailable(tmp_path: Path) -> None:
    fake = build_fake_sdk(tmp_path, android=True)
    backend = fake.backend_kind(
        "androidcli", AVDFLEET_TEST_ANDROID_VERSION="android 0.9.0 (fake)"
    )
    assert backend.available() is False
    assert "older" in backend.unavailable_reason.lower() or "0.9.0" in backend.unavailable_reason


def test_androidcli_avd_list_json_and_table_fallback(fake_sdk: FakeSdk) -> None:
    json_backend = fake_sdk.backend_kind("androidcli")
    entries = json_backend.avd_entries()
    assert [entry.name for entry in entries] == ["avd-01"]
    assert entries[0].api_level == 35
    assert entries[0].tag == "google_apis_playstore"
    assert entries[0].abi == "x86_64"

    table_backend = fake_sdk.backend_kind("androidcli", AVDFLEET_TEST_ANDROID_JSON="0")
    entries = table_backend.avd_entries()
    assert [entry.name for entry in entries] == ["avd-01"]
    assert entries[0].image_package == PLAY_PACKAGE
    assert table_backend.list_avds() == ["avd-01"]


def test_androidcli_sdk_list_table_fallback(fake_sdk: FakeSdk) -> None:
    backend = fake_sdk.backend_kind("androidcli", AVDFLEET_TEST_ANDROID_JSON="0")
    packages = {package.id: package for package in backend.list_packages()}
    assert packages["emulator"].installed is True
    assert packages["platforms;android-36"].installed is False


def test_androidcli_emulator_list(fake_sdk: FakeSdk) -> None:
    backend = fake_sdk.backend_kind("androidcli")
    emulators = backend.emulator_list()
    assert emulators[0]["name"] == "avd-01"
    assert emulators[0]["consolePort"] == 5554


def test_androidcli_uninstall_falls_back_to_legacy_sdkmanager(fake_sdk: FakeSdk) -> None:
    backend = fake_sdk.backend_kind("androidcli", AVDFLEET_TEST_ANDROID_UNINSTALL="0")
    backend.install_packages([GOOGLE_PACKAGE], accept_licenses=True)
    result = backend.uninstall_packages([GOOGLE_PACKAGE])
    assert result.ok is True, result.message
    assert GOOGLE_PACKAGE not in fake_sdk.state()["installed"]
    assert any(event.get("tool") == "sdkmanager" for event in fake_sdk.events())


def test_androidcli_uninstall_without_fallback_reports_unsupported(fake_sdk: FakeSdk) -> None:
    remove_shim(fake_sdk, "sdkmanager")
    backend = fake_sdk.backend_kind("androidcli", AVDFLEET_TEST_ANDROID_UNINSTALL="0")
    result = backend.uninstall_packages([GOOGLE_PACKAGE])
    assert result.ok is False
    assert "sdkmanager" in result.message.lower()


# ---------------------------------------------------------------------------
# Selector behaviour
# ---------------------------------------------------------------------------


def _fix_env_for_selector(monkeypatch: pytest.MonkeyPatch, fake_sdk: FakeSdk) -> None:
    for key, value in fake_sdk.environ.items():
        monkeypatch.setenv(key, value)


def test_get_backend_auto_prefers_androidcli(
    fake_sdk: FakeSdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fix_env_for_selector(monkeypatch, fake_sdk)
    backend = get_backend(host=fake_sdk.paths)
    assert backend.name == "androidcli"
    assert backend.fallback_reason == ""
    assert isinstance(backend, AndroidCliBackend)


def test_get_backend_auto_falls_back_to_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = build_fake_sdk(tmp_path, android=False)
    _fix_env_for_selector(monkeypatch, fake)
    backend = get_backend(host=fake.paths)
    assert backend.name == "legacy"
    assert isinstance(backend, LegacySdkBackend)
    assert "android" in backend.fallback_reason.lower()


def test_get_backend_explicit_legacy_wins(
    fake_sdk: FakeSdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fix_env_for_selector(monkeypatch, fake_sdk)
    backend = get_backend(host=fake_sdk.paths, prefer="legacy")
    assert backend.name == "legacy"
    assert backend.fallback_reason == ""


def test_get_backend_explicit_androidcli_without_binary_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = build_fake_sdk(tmp_path, android=False)
    _fix_env_for_selector(monkeypatch, fake)
    backend = get_backend(host=fake.paths, prefer="androidcli")
    assert backend.name == "legacy"
    assert backend.fallback_reason


def test_get_backend_unknown_preference_raises() -> None:
    with pytest.raises(ConfigError):
        get_backend(prefer="quantum")


def test_get_backend_accepts_config_paths(
    fake_sdk: FakeSdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fix_env_for_selector(monkeypatch, fake_sdk)
    config = Config(sdk_root=str(fake_sdk.sdk_root), avd_home=str(fake_sdk.avd_home))
    backend = get_backend(config)
    assert backend.name == "androidcli"
    assert Path(backend.sdk_paths.sdk_root) == fake_sdk.sdk_root


# ---------------------------------------------------------------------------
# Result redaction / tool result model
# ---------------------------------------------------------------------------


def test_tool_result_redacts_secrets_in_summary_and_dict() -> None:
    result = ToolResult(
        argv=[
            "emulator",
            "-avd",
            "avd-01",
            "-http-proxy",
            "http://alice:hunter2@127.0.0.1:8081",
        ],
        returncode=1,
        stdout="proxy http://alice:hunter2@127.0.0.1:8081 failed",
        stderr="password=hunter2",
        duration_s=0.5,
        timed_out=False,
    )
    payload = result.to_dict()
    assert "hunter2" not in json.dumps(payload)
    assert "***" in payload["stdout"]
    assert "hunter2" not in result.summary()
    assert "hunter2" not in " ".join(result.redacted_argv())
    assert result.ok is False
    assert "truncated" in result.to_dict(limit=5)["stdout"]


def test_failed_day_command_error_is_redacted() -> None:
    from avdfleet.platform.sdk.base import failed_command_error

    result = ToolResult(
        argv=["sdkmanager", "--install", "x"],
        returncode=1,
        stderr="failed http://alice:hunter2@proxy:8080",
    )
    error = failed_command_error(result, "install")
    assert "hunter2" not in str(error)
    assert "hunter2" not in json.dumps(error.to_dict())
    assert error.exit_code == 1
    assert error.argv[0] == "sdkmanager"
