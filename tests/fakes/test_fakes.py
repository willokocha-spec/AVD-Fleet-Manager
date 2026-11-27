"""Self-tests for the executable fakes (contracts/fakes-spec.md).

Most contract assertions drive ``fake_cli.main()`` in-process so the suite stays
fast even on a loaded host; the process boundary itself (the launch shims, long
lived emulator/Appium processes, exit codes through ``subprocess``) is covered by
the smaller set of subprocess-based tests.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import fake_cli
from tests.fakes.runtime import (
    DEFAULT_APPIUM_PORT,
    FakeTools,
    build_avd_entry,
    default_oobe_matrix,
    read_events,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class CliResult:
    """Result of an in-process fake CLI invocation."""

    def __init__(self, returncode: int, stdout_bytes: bytes, stderr: str) -> None:
        self.returncode = returncode
        self.raw_stdout = stdout_bytes
        self.stderr = stderr

    @property
    def stdout(self) -> str:
        return self.raw_stdout.decode("utf-8", "replace")


def run_cli(tool: str, *args: str, input: str | None = None) -> CliResult:
    """Invoke the fake CLI in-process (fast) while capturing streams."""
    stdout_buffer = io.BytesIO()
    stdout_stream = io.TextIOWrapper(stdout_buffer, encoding="utf-8", newline="\n")
    stderr_stream = io.StringIO()
    original_stdin = sys.stdin
    if input is not None:
        sys.stdin = io.StringIO(input)
    code = 0
    try:
        with contextlib.redirect_stdout(stdout_stream), contextlib.redirect_stderr(stderr_stream):
            try:
                code = fake_cli.main([tool, *args])
            except SystemExit as exc:  # pragma: no cover - handlers return codes
                code = exc.code if isinstance(exc.code, int) else 0
        stdout_stream.flush()
    finally:
        sys.stdin = original_stdin
    return CliResult(int(code or 0), stdout_buffer.getvalue(), stderr_stream.getvalue())


def run_tool(tool: str, *args: str, input: str | None = None, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    """Run a fake tool through the generated PATH (real process boundary)."""
    return subprocess.run(
        [tool, *args],
        capture_output=True,
        text=True,
        input=input,
        timeout=timeout,
        stdin=subprocess.DEVNULL if input is None else None,
    )


def run_raw(tool: str, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [tool, *args], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL
    )


def wait_until(predicate: Callable[[], Any], *, timeout: float = 30.0, message: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {message}")


def wait_avd(tools: FakeTools, name: str, states: set[str], *, timeout: float = 30.0) -> dict[str, Any]:
    def _poll() -> dict[str, Any] | None:
        entry = (tools.read_state().get("avds") or {}).get(name)
        if isinstance(entry, dict) and entry.get("state") in states:
            return entry
        return None

    return wait_until(_poll, timeout=timeout, message=f"AVD {name} in {sorted(states)}")


def read_socket_until(sock: socket.socket, needle: str, *, timeout: float = 10.0) -> str:
    sock.settimeout(timeout)
    data = b""
    deadline = time.monotonic() + timeout
    while needle.encode() not in data and time.monotonic() < deadline:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data.decode("utf-8", "replace")


def png_dimensions(payload: bytes) -> tuple[int, int]:
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    _length, tag = struct.unpack(">I4s", payload[8:16])
    assert tag == b"IHDR"
    return struct.unpack(">II", payload[16:24])


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"} if data else {}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _running_avd(fake_state: Any, name: str, **overrides: Any) -> dict[str, Any]:
    """Add a running fake AVD addressable as ``emulator-5554``."""
    kwargs: dict[str, Any] = {
        "state": "running",
        "boot_completed": True,
        "console_port": 5554,
        "adb_port": 5555,
        "grpc_port": 8554,
        "pid": 424250,
    }
    kwargs.update(overrides)
    return fake_state.add_avd(name, **kwargs)


# ---------------------------------------------------------------------------
# Long-lived process fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def emulator_process(fake_env: FakeTools, tmp_path: Path) -> Iterator[Callable[..., tuple[subprocess.Popen[bytes], Path]]]:
    """Spawn fake emulators with DEVNULL stdin and file-backed output."""
    processes: list[tuple[subprocess.Popen[bytes], Any]] = []
    logs: list[Path] = []

    def _spawn(*args: str) -> tuple[subprocess.Popen[bytes], Path]:
        log_path = tmp_path / f"emulator-{len(logs)}.out"
        logs.append(log_path)
        handle = open(log_path, "wb")  # noqa: SIM115 - closed in teardown
        process = subprocess.Popen(
            [str(fake_env.shim("emulator")), *args],
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        processes.append((process, handle))
        return process, log_path

    try:
        yield _spawn
    finally:
        for process, handle in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                    process.kill()
                    process.wait(timeout=15)
            try:
                handle.close()
            except OSError:  # pragma: no cover - defensive
                pass
        fake_env.cleanup_processes()


def _stop_emulator(tools: FakeTools, name: str) -> None:
    """Stop a fake emulator through the adb contract and wait for its state."""
    entry = (tools.read_state().get("avds") or {}).get(name) or {}
    port = entry.get("console_port")
    if port is not None:
        run_cli("adb", "-s", f"emulator-{port}", "emu", "kill")
    wait_avd(tools, name, {"stopped"}, timeout=30)


# ---------------------------------------------------------------------------
# Environment and shims
# ---------------------------------------------------------------------------


class TestEnvironment:
    def test_env_contract(self, fake_env: FakeTools) -> None:
        import os

        expected = {
            "AVDFLEET_FAKE_HOME": str(fake_env.home),
            "AVDFLEET_FAKE_SDK_ROOT": str(fake_env.sdk_root),
            "AVDFLEET_FAKE_AVD_HOME": str(fake_env.avd_home),
            "AVDFLEET_FAKE_PYTHON": str(fake_env.python),
            "AVDFLEET_FAKE_BOOT_SECONDS": str(fake_env.boot_seconds),
            "AVDFLEET_FAKE_ACCEL": fake_env.accel,
            "AVDFLEET_FAKE_HOST_OS": fake_env.host_os,
            "AVDFLEET_FAKE_ARCH": fake_env.arch,
            "ANDROID_SDK_ROOT": str(fake_env.sdk_root),
            "ANDROID_HOME": str(fake_env.sdk_root),
            "ANDROID_AVD_HOME": str(fake_env.avd_home),
            "JAVA_HOME": str(fake_env.sdk_root / "jdk"),
        }
        for key, value in expected.items():
            assert os.environ[key] == value, key
        assert os.environ["AVDFLEET_FAKE_APPIUM_PORT"] == str(DEFAULT_APPIUM_PORT)
        assert os.environ["PATH"].split(os.pathsep)[0] == str(fake_env.bin_dir)
        for folder in (fake_env.home, fake_env.sdk_root, fake_env.avd_home):
            assert folder.is_dir()
        assert fake_env.state.is_file()
        assert fake_env.oobe_matrix_path.is_file()
        assert fake_env.console_token_path.read_text(encoding="utf-8") == "fake-token"

    def test_shims_exist(self, fake_env: FakeTools) -> None:
        for tool in ("adb", "emulator", "sdkmanager", "avdmanager", "android", "appium"):
            resolved = shutil.which(tool)
            assert resolved is not None, tool
            assert Path(resolved).parent.resolve() == fake_env.bin_dir.resolve(), tool
        assert fake_env.launcher_mode or sys.platform != "win32"

    def test_subprocess_boundary_contract(self, fake_env: FakeTools) -> None:
        # The critical cross-platform contract: subprocess.run(["adb", ...]) with
        # shell=False must reach the fake on Windows too - including --version,
        # which CPython itself would otherwise intercept.
        result = run_tool("adb", "version")
        assert result.returncode == 0, result.stderr
        assert "Android Debug Bridge version 1.0.41" in result.stdout
        assert "Version 35.0.2" in result.stdout
        sdkmanager_version = run_tool("sdkmanager", "--version")
        assert sdkmanager_version.returncode == 0, sdkmanager_version.stderr
        assert sdkmanager_version.stdout.strip() == "11.0.0"

    def test_python_shim_delegates_to_real_interpreter(self, fake_env: FakeTools) -> None:
        result = run_tool("python", "-c", "print(40 + 2)")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "42"
        version = run_tool("python", "--version")
        assert version.stdout.strip() == (
            f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        )

    def test_sdk_layout_contains_real_tool_files(self, fake_env: FakeTools) -> None:
        platform_tools = fake_env.sdk_root / "platform-tools"
        assert (platform_tools / "source.properties").is_file()
        adb_name = "adb.exe" if sys.platform == "win32" else "adb"
        assert (platform_tools / adb_name).is_file()
        cmdline_bin = fake_env.sdk_root / "cmdline-tools" / "latest" / "bin"
        assert (cmdline_bin / ("sdkmanager.exe" if sys.platform == "win32" else "sdkmanager")).exists()
        assert (fake_env.sdk_root / "jdk" / "release").is_file()
        image = fake_env.sdk_root / "system-images" / "android-35" / "google_apis_playstore" / fake_env.host_abi
        assert (image / "system.img").is_file()
        assert (image / "build.prop").is_file()

    def test_events_logged(self, fake_env: FakeTools, fake_state: Any) -> None:
        assert run_cli("adb", "devices").returncode == 0
        events = read_events(fake_env.home)
        assert events, "events.jsonl must record every invocation"
        last = events[-1]
        assert last["tool"] == "adb"
        assert last["argv"] == ["devices"]
        assert Path(last["cwd"]).is_absolute()
        commands = fake_state.commands("adb")
        assert commands and commands[-1]["argv"] == ["devices"]

    def test_tool_versions(self, fake_env: FakeTools) -> None:
        assert run_cli("node", "--version").stdout.strip() == "v22.14.0"
        assert run_cli("npm", "--version").stdout.strip() == "10.9.2"
        assert run_cli("npx", "--version").stdout.strip() == "10.9.2"
        assert run_cli("uv", "--version").stdout.strip() == "uv 0.6.0"
        assert run_cli("scrcpy", "--version").stdout.strip() == "scrcpy 3.1"
        java = run_cli("java", "-version")
        assert java.returncode == 0
        assert 'openjdk version "21.0.6" 2025-01-21' in java.stderr
        assert run_cli("appium", "--version").stdout.strip() == "4.0.0"

    def test_emulator_version_and_help(self, fake_env: FakeTools) -> None:
        version = run_cli("emulator", "-version")
        assert version.returncode == 0
        assert "Android emulator version 35.1.4.0 (build_id 12345678)" in version.stdout
        help_result = run_cli("emulator", "-help")
        assert help_result.returncode == 0
        assert "-avd" in help_result.stdout
        assert run_cli("emulator", "-list-avds").returncode == 0

    def test_injected_tool_failure(self, fake_env: FakeTools, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AVDFLEET_FAKE_FAIL", "emulator")
        result = run_cli("emulator", "-version")
        assert result.returncode == 1
        assert "injected failure" in result.stderr


# ---------------------------------------------------------------------------
# sdkmanager
# ---------------------------------------------------------------------------


class TestSdkmanager:
    def test_version(self, fake_env: FakeTools) -> None:
        assert run_cli("sdkmanager", "--version").stdout.strip() == "11.0.0"

    def test_list_blocks_and_catalogue(self, fake_env: FakeTools) -> None:
        result = run_cli("sdkmanager", "--list")
        assert result.returncode == 0
        assert "Installed packages:" in result.stdout
        assert "Available Packages:" in result.stdout
        for package in (
            "platform-tools",
            "emulator",
            "system-images;android-35;google_apis_playstore;x86_64",
            "system-images;android-34;google_apis_playstore;x86_64",
        ):
            assert package in result.stdout
        assert "Path" in result.stdout and "Version" in result.stdout

    def test_list_installed_only(self, fake_env: FakeTools) -> None:
        result = run_cli("sdkmanager", "--list_installed")
        assert result.returncode == 0
        assert "Installed packages:" in result.stdout
        assert "Available Packages:" not in result.stdout
        assert "platform-tools" in result.stdout

    def test_sdk_root_flag_accepted(self, fake_env: FakeTools, tmp_path: Path) -> None:
        custom = tmp_path / "custom-sdk"
        custom.mkdir()
        assert run_cli("sdkmanager", f"--sdk_root={custom}", "--list_installed").returncode == 0
        assert run_cli("sdkmanager", "--sdk_root", str(custom), "--version").stdout.strip() == "11.0.0"

    def test_install_known_package(self, fake_env: FakeTools, fake_state: Any) -> None:
        package = "system-images;android-35;google_apis;x86_64"
        assert package not in fake_state.installed()
        result = run_cli("sdkmanager", "--install", package)
        assert result.returncode == 0, result.stderr
        assert f'Preparing "Install {package}".' in result.stdout
        assert "Downloading..." in result.stdout
        assert "Unzipping..." in result.stdout
        assert "Installing..." in result.stdout
        assert package in fake_state.installed()
        image_dir = fake_env.sdk_root / "system-images" / "android-35" / "google_apis" / "x86_64"
        assert (image_dir / "system.img").is_file()
        assert (image_dir / "build.prop").is_file()
        assert (image_dir / "source.properties").is_file()

    def test_install_unknown_package_fails(self, fake_env: FakeTools) -> None:
        result = run_cli("sdkmanager", "--install", "system-images;android-99;nope;x86_64")
        assert result.returncode == 1
        assert "Warning: Failed to find package system-images;android-99;nope;x86_64" in result.stderr

    def test_uninstall(self, fake_env: FakeTools, fake_state: Any) -> None:
        assert run_cli("sdkmanager", "--uninstall", "build-tools;35.0.0").returncode == 0
        assert "build-tools;35.0.0" not in fake_state.installed()

    def test_licenses_accept_and_reject(self, fake_env: FakeTools, fake_state: Any) -> None:
        accepted = run_cli("sdkmanager", "--licenses", input="y\n")
        assert accepted.returncode == 0, accepted.stderr
        assert "android-sdk-license" in fake_state.read()["licenses_accepted"]
        assert (fake_env.sdk_root / "licenses" / "android-sdk-license").is_file()
        fake_env.reset()
        rejected = run_cli("sdkmanager", "--licenses", input="")
        assert rejected.returncode == 1

    def test_channel_and_verbose_accepted(self, fake_env: FakeTools) -> None:
        result = run_cli("sdkmanager", "--list", "--channel=2", "--verbose")
        assert result.returncode == 0
        assert "channel dev" in result.stderr

    def test_injected_failure(self, fake_env: FakeTools, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AVDFLEET_FAKE_FAIL", "sdkmanager:install")
        result = run_cli("sdkmanager", "--install", "platforms;android-34")
        assert result.returncode == 1
        assert "Warning" in result.stderr


# ---------------------------------------------------------------------------
# avdmanager
# ---------------------------------------------------------------------------


class TestAvdmanager:
    def test_list_device(self, fake_env: FakeTools) -> None:
        result = run_cli("avdmanager", "list", "device")
        assert result.returncode == 0
        for expected in ('id: 7 or "pixel_7"', "Name: Pixel 7", "pixel_tablet", "Nexus 5X", "pixel_6"):
            assert expected in result.stdout

    def test_list_target(self, fake_env: FakeTools) -> None:
        result = run_cli("avdmanager", "list", "target")
        assert result.returncode == 0
        assert "android-35" in result.stdout
        assert "Available Android targets:" in result.stdout

    def test_list_avd_empty(self, fake_env: FakeTools) -> None:
        result = run_cli("avdmanager", "list", "avd")
        assert result.returncode == 0
        assert "Available Android Virtual Devices:" in result.stdout

    def test_create_list_delete_roundtrip(self, fake_env: FakeTools, fake_state: Any) -> None:
        package = "system-images;android-35;google_apis_playstore;x86_64"
        created = run_cli(
            "avdmanager", "create", "avd", "-n", "avd-test", "-k", package, "-d", "pixel_7"
        )
        assert created.returncode == 0, created.stderr
        entry = fake_state.avd("avd-test")
        assert entry["package"] == package
        assert entry["device"] == "pixel_7"
        assert entry["api"] == 35
        ini = fake_env.avd_home / "avd-test.ini"
        config_ini = fake_env.avd_home / "avd-test.avd" / "config.ini"
        assert ini.is_file() and config_ini.is_file()
        config_text = config_ini.read_text(encoding="utf-8")
        for key in (
            "image.sysdir.1=",
            "tag.id=",
            "abi.type=",
            "hw.device.name=",
            "hw.ramSize=",
            "disk.dataPartition.size=",
            "PlayStore.enabled=",
            "fastboot.forceColdBoot=yes",
        ):
            assert key in config_text, key
        assert patch_config_value(config_text, "PlayStore.enabled") == "true"
        listing = run_cli("avdmanager", "list", "avd")
        assert "Name: avd-test" in listing.stdout
        assert "Based on: Android API 35 Tag/ABI: google_apis_playstore/x86_64" in listing.stdout
        assert run_cli("avdmanager", "delete", "avd", "-n", "avd-test").returncode == 0
        assert "avd-test" not in fake_state.avds()
        assert not ini.exists()

    def test_create_duplicate_and_force(self, fake_env: FakeTools) -> None:
        package = "system-images;android-35;google_apis_playstore;x86_64"
        args = ("avdmanager", "create", "avd", "-n", "dupe", "-k", package)
        assert run_cli(*args).returncode == 0
        duplicate = run_cli(*args)
        assert duplicate.returncode == 1
        assert "already exists" in duplicate.stderr
        assert run_cli(*args, "--force").returncode == 0

    def test_create_invalid_package(self, fake_env: FakeTools) -> None:
        result = run_cli("avdmanager", "create", "avd", "-n", "bad", "-k", "system-images;android-99;x;x86_64")
        assert result.returncode == 1
        assert "Package path is not valid" in result.stderr

    def test_delete_missing_fails(self, fake_env: FakeTools) -> None:
        assert run_cli("avdmanager", "delete", "avd", "-n", "ghost").returncode == 1

    def test_move_rewrites_ini(self, fake_env: FakeTools, fake_state: Any, tmp_path: Path) -> None:
        package = "system-images;android-35;google_apis_playstore;x86_64"
        assert run_cli("avdmanager", "create", "avd", "-n", "mover", "-k", package).returncode == 0
        new_path = tmp_path / "relocated" / "mover.avd"
        assert run_cli("avdmanager", "move", "avd", "-n", "mover", "-p", str(new_path)).returncode == 0
        ini_text = (fake_env.avd_home / "mover.ini").read_text(encoding="utf-8")
        assert str(new_path) in ini_text
        assert fake_state.avd("mover")["avd_path"] == str(new_path)

    def test_silent_create_suppresses_progress(self, fake_env: FakeTools) -> None:
        package = "system-images;android-35;google_apis_playstore;x86_64"
        result = run_cli("avdmanager", "create", "avd", "-n", "quiet", "-k", package, "--silent")
        assert result.returncode == 0
        assert "Created AVD" not in result.stdout


def patch_config_value(config_text: str, key: str) -> str:
    for line in config_text.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    raise AssertionError(f"{key} not found in config.ini")


# ---------------------------------------------------------------------------
# adb
# ---------------------------------------------------------------------------


class TestAdb:
    def test_devices_and_selection(self, fake_env: FakeTools, fake_state: Any) -> None:
        fake_state.seed_avds()
        devices = run_cli("adb", "devices")
        assert devices.returncode == 0
        assert "emulator-5554\tdevice" in devices.stdout
        assert "emulator-5556\tdevice" in devices.stdout
        long_listing = run_cli("adb", "devices", "-l")
        assert "product:sdk_gphone64_x86_64 model:sdk_gphone64_x86_64 device:emu64x" in long_listing.stdout
        assert run_cli("adb", "-s", "emulator-5554", "get-state").stdout.strip() == "device"

    def test_no_devices(self, fake_env: FakeTools) -> None:
        result = run_cli("adb", "shell", "getprop", "sys.boot_completed")
        assert result.returncode == 1
        assert "no devices" in result.stderr

    def test_getprop(self, fake_env: FakeTools, fake_state: Any) -> None:
        fake_state.seed_avds()
        assert run_cli("adb", "-s", "emulator-5554", "shell", "getprop", "sys.boot_completed").stdout.strip() == "1"
        assert run_cli("adb", "-s", "emulator-5556", "shell", "getprop", "sys.boot_completed").stdout.strip() == "0"
        props = {
            "ro.build.version.sdk": "35",
            "ro.product.cpu.abi": "x86_64",
            "ro.build.version.release": "15",
            "ro.kernel.qemu": "1",
            "ro.serialno": "emulator-5554",
        }
        for prop, expected in props.items():
            assert run_cli("adb", "-s", "emulator-5554", "shell", "getprop", prop).stdout.strip() == expected
        unknown = run_cli("adb", "-s", "emulator-5554", "shell", "getprop", "ro.does.not.exist")
        assert unknown.returncode == 0
        assert unknown.stdout.strip() == ""

    def test_dumpsys_activity_and_window(self, fake_env: FakeTools, fake_state: Any) -> None:
        _running_avd(fake_state, "dumper")
        activity = run_cli("adb", "-s", "emulator-5554", "shell", "dumpsys", "activity", "activities")
        assert "mResumedActivity" in activity.stdout
        assert "topResumedActivity" in activity.stdout
        assert "com.google.android.setupwizard/.SetupWizardActivity" in activity.stdout
        window = run_cli("adb", "-s", "emulator-5554", "shell", "dumpsys", "window")
        assert "mCurrentFocus" in window.stdout

    def test_am_start_and_settings(self, fake_env: FakeTools, fake_state: Any) -> None:
        _running_avd(fake_state, "amtest")
        start = run_cli(
            "adb", "-s", "emulator-5554", "shell", "am", "start", "-n", "com.example.app/.MainActivity"
        )
        assert start.returncode == 0
        assert "Starting:" in start.stdout
        assert fake_state.avd("amtest")["oobe"] == "complete"
        assert fake_state.avd("amtest")["activity"] == "com.example.app/.MainActivity"
        assert (
            run_cli("adb", "-s", "emulator-5554", "shell", "settings", "get", "secure", "user_setup_complete")
            .stdout.strip()
            == "1"
        )
        launcher = run_cli(
            "adb", "-s", "emulator-5554", "shell", "am", "start", "-a", "android.intent.action.MAIN"
        )
        assert launcher.returncode == 0
        assert "nexuslauncher" in fake_state.avd("amtest")["activity"]
        assert run_cli("adb", "-s", "emulator-5554", "shell", "am", "force-stop", "com.example.app").returncode == 0
        assert run_cli("adb", "-s", "emulator-5554", "shell", "am", "broadcast", "-a", "ping").returncode == 0
        assert (
            run_cli("adb", "-s", "emulator-5554", "shell", "settings", "put", "secure", "user_setup_complete", "0")
            .returncode
            == 0
        )
        assert fake_state.avd("amtest")["oobe"] == "setup"

    def test_pm(self, fake_env: FakeTools, fake_state: Any) -> None:
        _running_avd(fake_state, "pmtest")
        packages = run_cli("adb", "-s", "emulator-5554", "shell", "pm", "list", "packages")
        assert "package:com.android.settings" in packages.stdout
        assert "package:com.google.android.apps.nexuslauncher" in packages.stdout
        assert "package:com.android.launcher3" in packages.stdout
        path = run_cli("adb", "-s", "emulator-5554", "shell", "pm", "path", "com.android.settings")
        assert path.stdout.strip() == "package:/data/app/com.android.settings/base.apk"

    def test_input_records(self, fake_env: FakeTools, fake_state: Any) -> None:
        _running_avd(fake_state, "inputtest")
        for command in (
            ("input", "tap", "10", "20"),
            ("input", "swipe", "1", "2", "3", "4", "500"),
            ("input", "text", "hello"),
            ("input", "keyevent", "KEYCODE_HOME"),
        ):
            assert run_cli("adb", "-s", "emulator-5554", "shell", *command).returncode == 0
        inputs = fake_state.read()["inputs"]
        assert {"instance": "inputtest", "kind": "tap", "x": 10, "y": 20} in inputs
        assert any(item.get("kind") == "swipe" for item in inputs)
        assert any(item.get("text") == "hello" for item in inputs)
        assert any(item.get("keycode") == "KEYCODE_HOME" for item in inputs)

    def test_screencap_valid_png(self, fake_env: FakeTools, fake_state: Any) -> None:
        _running_avd(fake_state, "screen")
        for argv in (("exec-out", "screencap", "-p"), ("shell", "screencap", "-p")):
            result = run_cli("adb", "-s", "emulator-5554", *argv)
            assert result.returncode == 0
            assert png_dimensions(result.raw_stdout) == (1080, 1920)
            assert b"<fake-adb-shell>" not in result.raw_stdout
        fake_state.update(screenshots={"screen": {"width": 720, "height": 1280}})
        scaled = run_cli("adb", "-s", "emulator-5554", "exec-out", "screencap", "-p")
        assert png_dimensions(scaled.raw_stdout) == (720, 1280)

    def test_script_map_and_exit_codes(self, fake_env: FakeTools, fake_state: Any) -> None:
        _running_avd(fake_state, "scripter")
        fake_env.write_script(
            {
                "echo mapped": {"stdout": "mapped-output", "stderr": "", "exit_code": 0},
                "failing-cmd": {"stdout": "", "stderr": "boom", "exit_code": 7},
            }
        )
        mapped = run_cli("adb", "-s", "emulator-5554", "shell", "sh", "-c", "echo mapped")
        assert mapped.returncode == 0
        assert "mapped-output" in mapped.stdout
        failing = run_cli("adb", "-s", "emulator-5554", "shell", "-c", "failing-cmd")
        assert failing.returncode == 7
        assert "boom" in failing.stderr
        echoed = run_cli("adb", "-s", "emulator-5554", "shell", "echo", "plain", "text")
        assert echoed.stdout.strip() == "plain text"
        assert run_cli("adb", "-s", "emulator-5554", "shell", "exit", "3").returncode == 3
        fallback = run_cli("adb", "-s", "emulator-5554", "shell", "unmapped", "--flag")
        assert fallback.returncode == 0
        assert "<fake-adb-shell>: unmapped --flag" in fallback.stdout

    def test_install_uninstall(self, fake_env: FakeTools, fake_state: Any) -> None:
        fake_state.add_avd("installer")
        install = run_cli("adb", "-s", "emulator-5554", "install", "-r", "/tmp/app.apk")
        assert install.returncode == 0
        assert "Success" in install.stdout
        assert run_cli("adb", "-s", "emulator-5554", "uninstall", "com.example.app").returncode == 0

    def test_injected_install_failure(
        self, fake_env: FakeTools, fake_state: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_state.add_avd("failer")
        monkeypatch.setenv("AVDFLEET_FAKE_FAIL", "adb:install")
        result = run_cli("adb", "-s", "emulator-5554", "install", "/tmp/app.apk")
        assert result.returncode == 1
        assert "Failure" in result.stderr

    def test_forward_reverse(self, fake_env: FakeTools, fake_state: Any) -> None:
        _running_avd(fake_state, "forwarder")
        assert run_cli("adb", "-s", "emulator-5554", "forward", "tcp:8200", "tcp:8200").stdout.strip() == "8200"
        assert fake_state.avd("forwarder")["forwards"] == [{"local": "tcp:8200", "remote": "tcp:8200"}]
        reverse = run_cli("adb", "-s", "emulator-5554", "reverse", "tcp:8300", "tcp:8300")
        assert reverse.stdout.strip() == "8300"
        assert fake_state.avd("forwarder")["reverses"] == [{"local": "tcp:8300", "remote": "tcp:8300"}]
        assert run_cli("adb", "-s", "emulator-5554", "forward", "--remove-all").returncode == 0
        assert fake_state.avd("forwarder")["forwards"] == []

    def test_emu_avd_name_and_kill(self, fake_env: FakeTools, fake_state: Any) -> None:
        fake_state.seed_avds()
        name = run_cli("adb", "-s", "emulator-5554", "emu", "avd", "name")
        assert name.stdout.strip() == "avd-02"
        assert run_cli("adb", "-s", "emulator-5554", "emu", "kill").returncode == 0
        assert fake_state.avd("avd-02")["state"] == "stopped"
        assert fake_state.avd("avd-02")["pid"] is None

    def test_logcat_dump(self, fake_env: FakeTools, fake_state: Any) -> None:
        fake_state.add_avd("logger")
        result = run_cli("adb", "-s", "emulator-5554", "logcat", "-d")
        assert result.returncode == 0
        assert "--------- beginning of main" in result.stdout

    def test_wait_for_device(self, fake_env: FakeTools, fake_state: Any) -> None:
        _running_avd(fake_state, "waiter")
        assert run_cli("adb", "-s", "emulator-5554", "wait-for-device").returncode == 0

    def test_unknown_subcommand_exits_zero_with_diagnostic(self, fake_env: FakeTools) -> None:
        result = run_cli("adb", "definitely-not-a-command")
        assert result.returncode == 0
        assert result.stderr.strip()


# ---------------------------------------------------------------------------
# emulator
# ---------------------------------------------------------------------------


class TestEmulator:
    def test_list_avds(self, fake_env: FakeTools, fake_state: Any) -> None:
        fake_state.add_avd("listed")
        assert run_cli("emulator", "-list-avds").stdout.split() == ["listed"]

    def test_missing_avd_panics(self, fake_env: FakeTools) -> None:
        result = run_cli("emulator", "-avd", "ghost", "-port", "5554")
        assert result.returncode == 1
        assert "PANIC: Unknown AVD name [ghost]" in result.stderr

    def test_already_running_fails(self, fake_env: FakeTools, fake_state: Any) -> None:
        fake_state.add_avd("busy", state="running", pid=424210, console_port=5554, adb_port=5555, boot_completed=True)
        result = run_cli("emulator", "-avd", "busy")
        assert result.returncode == 1
        assert "already running" in result.stderr

    def test_lifecycle_console_and_proxy_isolation(
        self,
        fake_env: FakeTools,
        fake_state: Any,
        emulator_process: Callable[..., tuple[subprocess.Popen[bytes], Path]],
    ) -> None:
        fake_state.add_avd("booted")
        proxy = "http://user1:pass1@127.0.0.1:8081"
        process, log_path = emulator_process(
            "-avd",
            "booted",
            "-port",
            "5554",
            "-no-window",
            "-gpu",
            "swiftshader_indirect",
            "-http-proxy",
            proxy,
        )
        entry = wait_avd(fake_env, "booted", {"booting", "running"})
        assert isinstance(entry["pid"], int) and entry["pid"] > 0
        console_port = int(entry["console_port"])
        assert console_port % 2 == 0
        assert entry["adb_port"] == console_port + 1
        assert entry["proxy"] == proxy
        assert entry["gpu"] == "swiftshader_indirect"
        assert entry["no_window"] is True
        entry = wait_avd(fake_env, "booted", {"running"})
        assert entry["boot_completed"] is True
        grpc_file = fake_env.grpc_dir / f"{console_port}.ini"
        content = grpc_file.read_text(encoding="utf-8")
        assert "grpc.port=" in content and "grpc.token=fake-grpc-token" in content
        with socket.create_connection(("127.0.0.1", console_port), timeout=10) as connection:
            greeting = read_socket_until(connection, "OK")
            assert "Android Console" in greeting
            connection.sendall(b"auth fake-token\n")
            assert "OK" in read_socket_until(connection, "OK")
            connection.sendall(b"avd name\n")
            assert "booted" in read_socket_until(connection, "OK")
            connection.sendall(b"snapshot save avdfleet-golden\n")
            assert "OK" in read_socket_until(connection, "OK")
            connection.sendall(b"kill\n")
            assert "OK" in read_socket_until(connection, "OK")
        process.wait(timeout=30)
        wait_avd(fake_env, "booted", {"stopped"})
        stopped = fake_state.avd("booted")
        assert stopped["pid"] is None
        assert stopped["snapshot"] == "avdfleet-golden"
        assert not grpc_file.exists()
        output = log_path.read_text(encoding="utf-8")
        assert "INFO: QEMU waiting for connection, starting emulator..." in output
        assert "boot completed" in output
        assert "user1" not in output
        assert "pass1" not in output

    def test_wipe_data_resets_oobe(
        self,
        fake_env: FakeTools,
        fake_state: Any,
        emulator_process: Callable[..., tuple[subprocess.Popen[bytes], Path]],
    ) -> None:
        fake_state.add_avd("wiper", console_port=5554, adb_port=5555, oobe="complete", activity="launcher")
        process, _log = emulator_process("-avd", "wiper", "-port", "5554", "-wipe-data", "-no-snapshot")
        entry = wait_avd(fake_env, "wiper", {"booting", "running"})
        assert entry["oobe"] == "setup"
        assert entry["activity"] == "com.google.android.setupwizard/.SetupWizardActivity"
        _stop_emulator(fake_env, "wiper")
        process.wait(timeout=30)
        assert fake_state.avd("wiper")["state"] == "stopped"

    def test_grpc_flag_recorded(
        self,
        fake_env: FakeTools,
        fake_state: Any,
        emulator_process: Callable[..., tuple[subprocess.Popen[bytes], Path]],
    ) -> None:
        fake_state.add_avd("grpc")
        process, _log = emulator_process("-avd", "grpc", "-grpc", "8558")
        entry = wait_avd(fake_env, "grpc", {"booting", "running"})
        assert entry["grpc_port"] == 8558
        grpc_file = fake_env.grpc_dir / f"{entry['console_port']}.ini"
        assert "grpc.port=8558" in grpc_file.read_text(encoding="utf-8")
        _stop_emulator(fake_env, "grpc")
        process.wait(timeout=30)

    def test_multi_instance(
        self,
        fake_env: FakeTools,
        fake_state: Any,
        emulator_process: Callable[..., tuple[subprocess.Popen[bytes], Path]],
    ) -> None:
        fake_state.add_avd("m1")
        fake_state.add_avd("m2")
        p1, _ = emulator_process("-avd", "m1")
        p2, _ = emulator_process("-avd", "m2")
        first = wait_avd(fake_env, "m1", {"running"})
        second = wait_avd(fake_env, "m2", {"running"})
        ports = {int(first["console_port"]), int(second["console_port"])}
        assert len(ports) == 2, "each emulator must get its own console port"
        devices = run_cli("adb", "devices")
        for port in ports:
            assert f"emulator-{port}\tdevice" in devices.stdout
        assert run_cli("adb", "-s", f"emulator-{first['console_port']}", "emu", "kill").returncode == 0
        p1.wait(timeout=30)
        assert p2.poll() is None
        _stop_emulator(fake_env, "m2")
        p2.wait(timeout=30)


# ---------------------------------------------------------------------------
# appium
# ---------------------------------------------------------------------------


class TestAppium:
    def test_version_and_driver_list(self, fake_env: FakeTools) -> None:
        assert run_cli("appium", "--version").stdout.strip() == "4.0.0"
        drivers = run_cli("appium", "driver", "list", "--installed")
        assert drivers.returncode == 0
        assert "uiautomator2@3.0.0" in drivers.stdout
        assert run_cli("appium", "driver", "install", "uiautomator2").returncode == 0

    def test_http_server_lifecycle(self, fake_env: FakeTools, fake_state: Any, tmp_path: Path) -> None:
        port = free_port()
        log_path = tmp_path / "appium.out"
        handle = open(log_path, "wb")  # noqa: SIM115 - closed in finally
        process = subprocess.Popen(
            [str(fake_env.shim("appium")), "--port", str(port), "--base-path", "/wd/hub", "--log-level", "info"],
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        try:
            def _ready() -> bool:
                try:
                    return _http_json(f"http://127.0.0.1:{port}/status")["value"]["ready"] is True
                except (urllib.error.URLError, ConnectionError, OSError):
                    return False

            wait_until(_ready, timeout=60, message="appium /status")
            status = _http_json(f"http://127.0.0.1:{port}/wd/hub/status")
            assert status["value"]["build"]["version"] == "4.0.0"
            session = _http_json(
                f"http://127.0.0.1:{port}/wd/hub/session",
                method="POST",
                payload={
                    "capabilities": {"alwaysMatch": {"platformName": "Android", "appium:udid": "emulator-5554"}}
                },
            )
            session_id = session["value"]["sessionId"]
            assert session_id == "fake-session-1"
            assert session["value"]["capabilities"]["platformName"] == "Android"
            sessions = _http_json(f"http://127.0.0.1:{port}/wd/hub/sessions")
            assert [item["id"] for item in sessions["value"]] == [session_id]
            deleted = _http_json(f"http://127.0.0.1:{port}/wd/hub/session/{session_id}", method="DELETE")
            assert deleted["value"] is None
            assert _http_json(f"http://127.0.0.1:{port}/wd/hub/sessions")["value"] == []
            appium_state = fake_state.read()["appium"]
            assert appium_state["running"] is True
            assert appium_state["port"] == port
            assert isinstance(appium_state["pid"], int)
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                process.kill()
                process.wait(timeout=30)
            handle.close()
            fake_env.cleanup_processes()
        assert "Appium REST http interface listener started" in log_path.read_text(encoding="utf-8")
        stopped = fake_state.read()["appium"]
        assert stopped["running"] is False
        assert stopped["pid"] is None

    def test_port_busy_fails(self, fake_env: FakeTools) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            port = blocker.getsockname()[1]
            result = run_cli("appium", "--port", str(port))
            assert result.returncode == 1
            assert "already in use" in result.stderr


# ---------------------------------------------------------------------------
# android CLI
# ---------------------------------------------------------------------------


class TestAndroidCli:
    def test_version_and_sdk_list(self, fake_env: FakeTools) -> None:
        assert run_cli("android", "--version").stdout.strip() == "android 1.0.0 (fake)"
        listing = run_cli("android", "sdk", "list", "--installed")
        assert listing.returncode == 0
        assert "Installed packages:" in listing.stdout
        assert "platform-tools" in listing.stdout

    def test_sdk_install_and_licenses(self, fake_env: FakeTools, fake_state: Any) -> None:
        package = "system-images;android-35;google_apis;x86_64"
        assert run_cli("android", "sdk", "install", package).returncode == 0
        assert package in fake_state.installed()
        assert run_cli("android", "sdk", "licenses", input="y\n").returncode == 0
        assert "android-sdk-license" in fake_state.read()["licenses_accepted"]

    def test_avd_long_options_through_path(self, fake_env: FakeTools, fake_state: Any) -> None:
        # S1's AndroidCliClient uses the fakes-spec 4.5 long forms; assert the
        # exact command shape at the process boundary.
        package = "system-images;android-35;google_apis_playstore;x86_64"
        result = run_tool(
            "android", "avd", "create", "--name", "avd-int", "--package", package, "--device", "pixel_7", "--force"
        )
        assert result.returncode == 0, result.stderr
        assert fake_state.avd("avd-int")["package"] == package
        assert fake_state.avd("avd-int")["device"] == "pixel_7"
        assert (fake_env.avd_home / "avd-int.ini").is_file()
        deleted = run_tool("android", "avd", "delete", "--name", "avd-int")
        assert deleted.returncode == 0, deleted.stderr
        assert "avd-int" not in fake_state.avds()

    def test_avd_equals_option_forms(self, fake_env: FakeTools, fake_state: Any) -> None:
        package = "system-images;android-35;google_apis_playstore;x86_64"
        result = run_cli(
            "android",
            "avd",
            "create",
            "--name=avd-eq",
            f"--package={package}",
            "--device=pixel_6",
            "--force",
        )
        assert result.returncode == 0, result.stderr
        assert fake_state.avd("avd-eq")["device"] == "pixel_6"
        assert run_cli("android", "avd", "delete", "--name=avd-eq").returncode == 0

    def test_avd_json_and_emulator_list(self, fake_env: FakeTools, fake_state: Any) -> None:
        package = "system-images;android-35;google_apis_playstore;x86_64"
        created = run_cli("android", "avd", "create", "--name", "json-avd", "--package", package, "--device", "pixel_7")
        assert created.returncode == 0
        listing = run_cli("android", "avd", "list", "--json")
        parsed = json.loads(listing.stdout)
        assert isinstance(parsed, list)
        assert parsed[0]["name"] == "json-avd"
        assert parsed[0]["api"] == 35
        assert run_cli("android", "avd", "delete", "--name", "json-avd").returncode == 0
        assert "json-avd" not in fake_state.avds()
        fake_state.add_avd("runner", state="running", boot_completed=True)
        emulators = json.loads(run_cli("android", "emulator", "list").stdout)
        assert emulators and emulators[0]["name"] == "runner"

    def test_unsupported_subcommand(self, fake_env: FakeTools) -> None:
        assert run_cli("android", "bogus").returncode == 2


# ---------------------------------------------------------------------------
# Fixtures and helper objects
# ---------------------------------------------------------------------------


class TestFixtures:
    def test_state_helpers(self, fake_env: FakeTools, fake_state: Any) -> None:
        assert "platform-tools" in fake_state.installed()
        seeded = fake_state.seed_avds()
        assert seeded == ["avd-01", "avd-02", "avd-03"]
        assert fake_state.avd("avd-02")["state"] == "running"
        assert fake_state.avd("avd-03")["state"] == "booting"
        fake_state.set_oobe("avd-02", "complete")
        assert fake_state.avd("avd-02")["oobe"] == "complete"
        added = fake_state.add_avd("custom", device="Nexus 5X")
        assert added["device"] == "Nexus 5X"
        assert (fake_env.avd_home / "custom.ini").is_file()
        image = fake_state.add_image(api=34, tag="google_apis")
        assert image["package"] == "system-images;android-34;google_apis;x86_64"
        assert image["package"] in fake_state.installed()
        assert (fake_env.sdk_root / "system-images" / "android-34" / "google_apis" / "x86_64").is_dir()
        with pytest.raises(KeyError):
            fake_state.avd("missing")
        with pytest.raises(ValueError):
            fake_state.set_oobe("avd-01", "bogus")

    def test_reset_restores_seed(self, fake_env: FakeTools, fake_state: Any) -> None:
        fake_state.seed_avds()
        assert fake_state.avds()
        fake_env.reset()
        assert fake_state.avds() == {}
        assert "platform-tools" in fake_state.installed()
        assert not fake_env.events.exists() or fake_env.events.read_text(encoding="utf-8") == ""

    def test_oobe_matrix_seed(self, fake_env: FakeTools) -> None:
        matrix = json.loads(fake_env.oobe_matrix_path.read_text(encoding="utf-8"))
        assert matrix["host_abi"] in {"x86_64", "arm64-v8a"}
        records = {record["image_id"]: record for record in matrix["records"]}
        playstore_35 = records["system-images;android-35;google_apis_playstore;x86_64"]
        assert playstore_35["status"] == "verified"
        assert playstore_35["activity"] == "com.google.android.setupwizard/.SetupWizardActivity"
        assert records["system-images;android-35;google_apis;x86_64"]["status"] == "unverified"
        assert records["system-images;android-33;google_apis;x86_64"]["status"] == "failed"
        assert default_oobe_matrix(fake_env.host_abi)["version"] == "1"

    def test_fake_sdk(self, fake_env: FakeTools, fake_sdk: Any) -> None:
        assert fake_sdk.sdk_root == str(fake_env.sdk_root)
        assert fake_sdk.platform_tools == str(fake_env.sdk_root / "platform-tools")
        assert fake_sdk.avd_home == str(fake_env.avd_home)
        assert Path(fake_sdk.emulator).exists()
        assert fake_sdk.env["ANDROID_SDK_ROOT"] == str(fake_env.sdk_root)

    def test_fake_installer_context(self, fake_env: FakeTools, fake_installer_context: Any, tmp_path: Path) -> None:
        assert Path(fake_installer_context.paths.data_dir).is_relative_to(tmp_path)
        assert fake_installer_context.config.sdk_root == str(fake_env.sdk_root)
        assert (fake_installer_context.paths.data_dir / "oobe_matrix.json").is_file()
        assert fake_installer_context.env["AVDFLEET_FAKE_HOME"] == str(fake_env.home)

    def test_listener_helper(self, fake_env: FakeTools) -> None:
        listener = fake_env.add_listener(token="token-1")
        try:
            with urllib.request.urlopen(listener.token_url(), timeout=10) as response:
                payload = json.loads(response.read())
            assert payload["token"] == "token-1"
            assert listener.hits
        finally:
            listener.close()

    def test_state_file_read_retries_transient_windows_errors(
        self, fake_env: FakeTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.fakes import runtime as fake_runtime

        original = Path.read_text
        state_path = fake_env.state
        failures = {"count": 2}

        def flaky_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            if self == state_path and failures["count"] > 0:
                failures["count"] -= 1
                raise PermissionError(13, "sharing violation", str(self))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", flaky_read_text)
        state = fake_runtime.load_state(fake_env.home)
        assert state["schema"] == 1
        assert failures["count"] == 0

    def test_build_avd_entry_helper(self, fake_env: FakeTools) -> None:
        entry = build_avd_entry("helper", "system-images;android-35;google_apis_playstore;x86_64")
        assert entry["name"] == "helper"
        assert entry["api"] == 35
        assert entry["oobe"] == "setup"
        assert entry["forwards"] == []
