"""Portable environment and launcher tests (root marker, env dict, templates, launch flow)."""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path

import pytest

from avdfleet.core.errors import PortableError
from avdfleet.core.models import CheckStatus
from avdfleet.portable.checks import CheckResult
from avdfleet.portable.env import (
    build_environment,
    detect_root_change,
    discover_root,
    ensure_environment_dirs,
    isolation_notes,
    load_root_state,
    read_root_marker,
    recorded_old_roots,
    save_root_state,
    validate_root,
)
from avdfleet.portable.env.model import MARKER_NAME, host_os_name
from avdfleet.portable.launchers import (
    EXIT_MISSING_RUNTIME,
    EXIT_PREFLIGHT,
    EXIT_SERVER,
    EXIT_USAGE,
    LauncherSpec,
    LauncherUsageError,
    LaunchOptions,
    all_launchers,
    build_launch_plan,
    choose_port,
    linux_launcher,
    macos_launcher,
    main,
    open_ui,
    parse_launcher_args,
    report_failure,
    run_launcher,
    serve_command,
    show_error_dialog,
    validate_launcher,
    wait_for_health,
    windows_launcher,
    write_launchers,
)

try:  # tests are a package in the repo layout
    from .conftest import HOST_ARCH, HOST_OS, make_file, populate_runtime_dirs, write_marker
except ImportError:  # pragma: no cover - collected without package __init__
    from conftest import HOST_ARCH, HOST_OS, make_file, populate_runtime_dirs, write_marker  # type: ignore[no-redef]

pytestmark = pytest.mark.portable

IS_WINDOWS = host_os_name() == "windows"


# ---------------------------------------------------------------------------
# Bundle root discovery / validation
# ---------------------------------------------------------------------------


def test_discover_root_walks_up(portable_root: Path, tmp_path: Path) -> None:
    assert discover_root(portable_root / "sdk") == portable_root.resolve()
    assert discover_root(portable_root) == portable_root.resolve()
    assert discover_root(
        tmp_path / "elsewhere", env={"AVDFLEET_PORTABLE_ROOT": str(portable_root)}
    ) == portable_root.resolve()
    assert discover_root(tmp_path / "nope") is None


def test_validate_root_reports_move(portable_root: Path) -> None:
    marker = json.loads((portable_root / MARKER_NAME).read_text(encoding="utf-8"))
    marker["root"] = "/old/location"
    (portable_root / MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")
    validation = validate_root(portable_root)
    assert validation.root == portable_root.resolve()
    assert any("moved" in warning for warning in validation.warnings)
    assert read_root_marker(portable_root).format_version == "1"


def test_validate_root_errors(tmp_path: Path) -> None:
    with pytest.raises(PortableError, match="missing"):
        validate_root(tmp_path)
    invalid = tmp_path / "invalid"
    invalid.mkdir()
    (invalid / MARKER_NAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(PortableError, match="not valid JSON"):
        validate_root(invalid)
    bad_format = tmp_path / "bad-format"
    write_marker(bad_format, format_version="99")
    with pytest.raises(PortableError, match="format_version"):
        validate_root(bad_format)
    other_os = "macos" if HOST_OS != "macos" else "linux"
    wrong_host = tmp_path / "wrong-host"
    write_marker(wrong_host, host_os=other_os)
    with pytest.raises(PortableError, match="built for"):
        validate_root(wrong_host, host_os=HOST_OS)
    wrong_arch = tmp_path / "wrong-arch"
    write_marker(wrong_arch, host_arch="x86_64")
    with pytest.raises(PortableError, match="built for"):
        validate_root(wrong_arch, host_os=HOST_OS, host_arch="arm64")


def test_read_root_marker_keeps_optional_fields(portable_root: Path) -> None:
    write_marker(portable_root, offline=True, python_version="3.12.14", warnings=["w1"])
    marker = read_root_marker(portable_root)
    assert marker.offline is True
    assert marker.python_version == "3.12.14"
    assert marker.warnings == ["w1"]


# ---------------------------------------------------------------------------
# Environment construction
# ---------------------------------------------------------------------------


def test_build_environment_forces_paths_inside_root(portable_root: Path) -> None:
    separator = ";" if HOST_OS == "windows" else ":"
    env = build_environment(
        portable_root, host_os=HOST_OS, host_arch=HOST_ARCH, base_env={"PATH": f"/usr/bin{separator}/bin"}
    )
    values = env.env
    root = portable_root.resolve()
    assert values["AVDFLEET_PORTABLE_ROOT"] == str(root)
    assert values["AVDFLEET_ROOT"] == str(root)
    assert values["AVDFLEET_EDITION"] == "portable"
    for variable, expected in {
        "AVDFLEET_DATA_DIR": root / "data",
        "AVDFLEET_CONFIG_DIR": root / "config",
        "AVDFLEET_CACHE_DIR": root / "cache",
        "AVDFLEET_LOG_DIR": root / "logs",
        "AVDFLEET_SDK_ROOT": root / "sdk",
        "AVDFLEET_AVD_HOME": root / "avd",
        "ANDROID_HOME": root / "sdk",
        "ANDROID_SDK_ROOT": root / "sdk",
        "ANDROID_USER_HOME": root / "android",
        "ANDROID_PREFS_ROOT": root / "android",
        "ANDROID_EMULATOR_HOME": root / "android",
        "ANDROID_AVD_HOME": root / "avd",
        "ANDROID_SDK_HOME": root,
        "APPIUM_HOME": root / "data" / "appium",
        "TMP": root / "tmp",
        "TEMP": root / "tmp",
        "TMPDIR": root / "tmp",
        "PIP_CACHE_DIR": root / "data" / "cache" / "pip",
        "UV_CACHE_DIR": root / "data" / "cache" / "uv",
        "UV_PYTHON_INSTALL_DIR": root / "data" / "uv" / "python",
        "UV_TOOL_DIR": root / "data" / "uv" / "tools",
        "UV_TOOL_BIN_DIR": root / "data" / "uv" / "bin",
        "NPM_CONFIG_CACHE": root / "data" / "cache" / "npm",
        "GRADLE_USER_HOME": root / "data" / "cache" / "gradle",
    }.items():
        assert values[variable] == str(expected), variable
    assert values["PYTHONNOUSERSITE"] == "1"
    assert values["PYTHONDONTWRITEBYTECODE"] == "1"
    assert values["PYTHONUTF8"] == "1"
    assert values.get("VIRTUAL_ENV") is None
    assert values["PYTHONHOME"] == str(root / "python")
    assert values["PYTHONPATH"].startswith(str(root / "lib"))
    path_entries = values["PATH"].split(separator)
    assert path_entries[0] == str(root / "bin")
    python_bin = root / ("python" if HOST_OS == "windows" else "python/bin")
    assert str(python_bin) in path_entries
    assert str(root / "sdk" / "platform-tools") in path_entries
    assert values["PATH"].count(str(root / "bin")) == 1
    assert env.warnings == []
    assert env.notes
    payload = env.to_dict(include_env=False)
    assert payload["edition"] == "portable"
    assert payload["portable"] is True
    assert payload["relocatable"] is True
    assert payload["host_os"] == HOST_OS
    assert payload["paths"]["root"] == str(root)
    assert "env" not in payload
    assert env.to_dict()["env"]["AVDFLEET_PORTABLE_ROOT"] == str(root)


def test_build_environment_uses_venv(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    populate_runtime_dirs(root, venv=True, prep_root=str(root / "old"))
    write_marker(root)
    env = build_environment(root, host_os=HOST_OS, host_arch=HOST_ARCH, base_env={"PATH": ""})
    separator = ";" if HOST_OS == "windows" else ":"
    assert env.env["VIRTUAL_ENV"] == str(root.resolve() / "venv")
    assert "PYTHONHOME" not in env.env
    python_paths = env.env["PYTHONPATH"].split(separator)
    assert any("site-packages" in entry for entry in python_paths)
    venv_bin = root.resolve() / "venv" / ("Scripts" if HOST_OS == "windows" else "bin")
    assert env.env["PATH"].split(separator)[0] == str(venv_bin)


def test_build_environment_linux_separator_and_studio(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    populate_runtime_dirs(root)
    write_marker(root, host_os="linux")
    from avdfleet.portable.studio import write_studio_config

    write_studio_config(root)
    env = build_environment(
        root, host_os="linux", host_arch=HOST_ARCH, base_env={"PATH": "/usr/bin:/bin"}
    )
    assert ":" in env.env["PATH"] and ";" not in env.env["PATH"]
    assert env.env["STUDIO_PROPERTIES"] == str(root.resolve() / "studio" / "idea.properties")
    assert env.env["STUDIO_VM_OPTIONS"] == str(root.resolve() / "studio" / "studio.vmoptions")
    for variable in ("ANDROID_USER_HOME", "ANDROID_PREFS_ROOT"):
        assert env.env[variable] == str(root.resolve() / "android")


def test_environment_exports_bundled_jdk(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    populate_runtime_dirs(root)
    jdk_home = root / "jdk" / "Contents" / "Home" if HOST_OS == "macos" else root / "jdk"
    make_file(jdk_home / "bin" / ("java.exe" if IS_WINDOWS else "java"), "binary")
    write_marker(root)
    environment = build_environment(root, host_os=HOST_OS, host_arch=HOST_ARCH, base_env={})
    assert environment.env["JAVA_HOME"] == str(jdk_home.resolve())
    assert environment.env["STUDIO_JDK"] == str(jdk_home.resolve())
    assert environment.env["STUDIO_GRADLE_JDK"] == str(jdk_home.resolve())


def test_ensure_environment_dirs(portable_root: Path) -> None:
    env = build_environment(portable_root, host_os=HOST_OS, host_arch=HOST_ARCH, base_env={})
    created = ensure_environment_dirs(env)
    assert any(path.name == "tmp" for path in created)
    for directory in created:
        assert directory.is_dir()


def test_isolation_documentation_complete() -> None:
    for host in ("windows", "macos", "linux"):
        notes = isolation_notes(host)
        assert len(notes) >= 4
        assert all(note.host_os in {"all", host} for note in notes)
        assert all(note.target and note.reason for note in notes)
    windows_targets = " ".join(note.target for note in isolation_notes("windows"))
    macos_targets = " ".join(note.target for note in isolation_notes("macos"))
    linux_targets = " ".join(note.target for note in isolation_notes("linux"))
    assert "HKCU" in windows_targets
    assert "LaunchServices" in macos_targets
    assert "XDG_RUNTIME_DIR" in linux_targets


# ---------------------------------------------------------------------------
# Root-change detection
# ---------------------------------------------------------------------------


def test_root_change_detection(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    populate_runtime_dirs(root)
    old_root = str(tmp_path / "old-place")
    write_marker(root, recorded_root=old_root)
    env = build_environment(root, host_os=HOST_OS, host_arch=HOST_ARCH, base_env={})
    change = detect_root_change(env)
    assert change.relocated is True
    assert change.recorded_root == old_root
    assert change.warnings and "relocation" in change.warnings[0].lower()
    assert recorded_old_roots(env) == [old_root]
    assert change.to_dict()["relocated"] is True


def test_save_and_load_root_state(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    populate_runtime_dirs(root)
    write_marker(root, recorded_root=str(tmp_path / "old-place"))
    env = build_environment(root, host_os=HOST_OS, host_arch=HOST_ARCH, base_env={})
    state_path = save_root_state(env)
    assert state_path.is_file()
    state = load_root_state(root)
    assert state is not None and state.root == str(root.resolve())
    change = detect_root_change(env, state=state)
    assert change.relocated is False
    assert change.changed_paths == {}
    assert load_root_state(tmp_path / "other") is None


# ---------------------------------------------------------------------------
# Launcher templates
# ---------------------------------------------------------------------------


def test_all_launchers_are_valid() -> None:
    specs = all_launchers(version="0.1.0")
    assert [spec.platform for spec in specs] == ["windows", "macos", "linux"]
    for spec in specs:
        assert validate_launcher(spec) == [], spec.filename
        assert spec.content.endswith("\n")
    windows = next(spec for spec in specs if spec.platform == "windows")
    assert windows.encoding == "ascii"
    assert windows.executable is False
    assert windows.content.count("\r\n") > 10
    macos = next(spec for spec in specs if spec.platform == "macos")
    linux = next(spec for spec in specs if spec.platform == "linux")
    assert macos.filename == "AVD Fleet.command" and macos.executable is True
    assert linux.filename == "avdfleet.sh" and linux.executable is True
    assert not macos.has_bom and not linux.has_bom


def test_windows_launcher_content() -> None:
    spec = windows_launcher(version="9.9.9")
    content = spec.content
    assert content.startswith("@echo off\r\n")
    assert "Unblock-File" in content
    assert "Zone.Identifier" in content
    assert "PowerShell 5.1" in content
    assert "%~dp0" in content
    assert "--no-browser" in content and "--port" in content
    assert "serve --portable" in content
    assert "avdfleet.portable.launchers.generator start" in content
    assert "9.9.9" in content
    content.encode("ascii")  # must be pure ASCII


def test_macos_launcher_content() -> None:
    spec = macos_launcher()
    assert spec.content.startswith("#!/bin/bash\n")
    assert "xattr -dr com.apple.quarantine" in spec.content
    assert "AVD Fleet.command" in spec.content
    assert '"$@"' in spec.content
    assert "serve --portable" in spec.content


def test_linux_launcher_content() -> None:
    spec = linux_launcher()
    assert spec.content.startswith("#!/bin/sh\n")
    assert "chmod +x" in spec.content
    assert '"$ROOT"' in spec.content
    assert '"$@"' in spec.content
    assert "avdfleet.portable.launchers.generator start" in spec.content
    assert "\r" not in spec.content


def test_launcher_validation_catches_problems() -> None:
    crlf_posix = LauncherSpec(
        platform="linux",
        filename="bad.sh",
        content="#!/bin/sh\r\necho hi\r\n",
        encoding="utf-8",
        executable=True,
    )
    assert any("LF" in problem or "shebang" in problem for problem in validate_launcher(crlf_posix))
    bom_posix = LauncherSpec(
        platform="macos",
        filename="bad.command",
        content="\ufeff#!/bin/bash\necho hi\n",
        encoding="utf-8",
        executable=True,
    )
    assert any("BOM" in problem for problem in validate_launcher(bom_posix))
    not_executable = LauncherSpec(
        platform="linux", filename="x.sh", content="#!/bin/sh\necho hi\n", encoding="utf-8", executable=False
    )
    assert any("executable" in problem for problem in validate_launcher(not_executable))


def test_write_launchers(tmp_path: Path) -> None:
    written = write_launchers(tmp_path, host_os=HOST_OS)
    names = [path.name for path in written]
    expected = {"windows": "AVD Fleet.cmd", "macos": "AVD Fleet.command", "linux": "avdfleet.sh"}[HOST_OS]
    assert expected in names
    root_copy = tmp_path / expected
    launchers_copy = tmp_path / "launchers" / expected
    assert root_copy.is_file() and launchers_copy.is_file()
    assert root_copy.read_bytes() == launchers_copy.read_bytes()
    spec = next(spec for spec in all_launchers() if spec.platform == HOST_OS)
    assert root_copy.read_bytes() == spec.bytes
    if not IS_WINDOWS:
        assert root_copy.stat().st_mode & 0o111

    dry = tmp_path / "dry"
    write_launchers(dry, host_os=HOST_OS, dry_run=True)
    assert not dry.exists()


# ---------------------------------------------------------------------------
# Launch planning
# ---------------------------------------------------------------------------


def test_parse_launcher_args() -> None:
    options = parse_launcher_args(
        ["start", "--port", "9000", "--no-browser", "--timeout=30", "--no-relocate", "--no-preflight", "extra"]
    )
    assert options.port == 9000
    assert options.no_browser is True
    assert options.timeout_s == 30.0
    assert options.relocate is False
    assert options.preflight is False
    assert options.extra_args == ("extra",)
    assert parse_launcher_args(["--port=9100"]).port == 9100
    assert parse_launcher_args(["--", "--log-level", "debug"]).extra_args == ("--log-level", "debug")
    assert parse_launcher_args(["-h"]).help is True


@pytest.mark.parametrize(
    "argv",
    [["--port", "0"], ["--port", "70000"], ["--port", "abc"], ["--port"], ["--timeout"], ["--wat"], ["--timeout", "-1"]],
)
def test_parse_launcher_args_errors(argv: list[str]) -> None:
    with pytest.raises(LauncherUsageError):
        parse_launcher_args(argv)


def test_choose_port() -> None:
    assert choose_port(8765, is_free=lambda port: True) == 8765
    busy = {8765, 8766}
    assert choose_port(8765, is_free=lambda port: port not in busy) == 8767
    with pytest.raises(PortableError, match="No free TCP port"):
        choose_port(65535, is_free=lambda port: False)


def test_serve_command_prefers_console_script(portable_root: Path) -> None:
    env = build_environment(portable_root, host_os=HOST_OS, host_arch=HOST_ARCH, base_env={})
    script = portable_root / "venv" / ("Scripts" if IS_WINDOWS else "bin") / (
        "avdfleet.exe" if IS_WINDOWS else "avdfleet"
    )
    make_file(script, "binary", executable=True)
    command = serve_command(env, host_os=HOST_OS)
    assert command == [str(script)]
    script.unlink()
    fallback = serve_command(env, host_os=HOST_OS, python_executable="C:/x/python.exe")
    assert fallback == ["C:/x/python.exe", "-m", "avdfleet"]


def test_build_launch_plan(portable_root: Path) -> None:
    plan = build_launch_plan(
        portable_root,
        port=8800,
        no_browser=True,
        extra_args=("--log-level", "debug"),
        host_os=HOST_OS,
        base_env={"PATH": ""},
        is_free=lambda port: True,
    )
    assert plan.port == 8800
    assert plan.argv[-4:] == ["--port", "8800", "--log-level", "debug"]
    assert "serve" in plan.argv and "--portable" in plan.argv
    assert plan.health_url == "http://127.0.0.1:8800/health"
    assert plan.browser_url == "http://127.0.0.1:8800/"
    assert plan.env["AVDFLEET_PORTABLE_ROOT"] == str(portable_root.resolve())
    assert plan.no_browser is True


def test_wait_for_health_success() -> None:
    calls = {"count": 0}

    def probe(url: str) -> tuple[bool, str]:
        calls["count"] += 1
        return (calls["count"] >= 3, "" if calls["count"] >= 3 else "starting")

    result = wait_for_health(
        "http://127.0.0.1:1/health", timeout_s=10, interval_s=0.01, probe=probe, sleep=lambda s: None
    )
    assert result.healthy is True
    assert result.attempts == 3
    assert calls["count"] == 3


def test_wait_for_health_timeout() -> None:
    ticks = itertools.count(0, 0.5)
    result = wait_for_health(
        "http://127.0.0.1:1/health",
        timeout_s=1.0,
        interval_s=0.5,
        probe=lambda url: (False, "connection refused"),
        sleep=lambda s: None,
        clock=lambda: next(ticks),
    )
    assert result.healthy is False
    assert result.attempts <= 4
    assert result.last_error == "connection refused"


# ---------------------------------------------------------------------------
# Full launch flow with injected fakes
# ---------------------------------------------------------------------------


class FakeProcess:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.terminated = False
        self.waited = False

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:  # pragma: no cover - only on stubborn children
        pass


class OkReport:
    refuse = False
    warnings: list[object] = []
    failures: list[object] = []


class RefuseReport:
    refuse = True
    warnings: list[object] = []

    def __init__(self) -> None:
        self.failures = [CheckResult("filesystem", "Bundle filesystem", CheckStatus.FAIL, "FAT32 refused", critical=True)]


def run_with(
    root: Path,
    *,
    report: object | None = None,
    probe: object | None = None,
    opener: object | None = None,
    options: LaunchOptions | None = None,
    process: FakeProcess | None = None,
) -> tuple[int, dict[str, object]]:
    recorder: dict[str, object] = {"opened": [], "messages": [], "dialogs": [], "pauses": 0, "plans": []}
    proc = process or FakeProcess()
    spawned = {"count": 0}

    def spawn(plan: object) -> FakeProcess:
        spawned["count"] += 1
        recorder["plans"].append(plan)
        return proc

    chosen_opener = opener or (lambda url: True)

    def recorded_opener(url: str) -> bool:
        recorder["opened"].append(url)  # type: ignore[union-attr]
        return bool(chosen_opener(url))

    def dialog(message: str) -> None:
        recorder["dialogs"].append(message)  # type: ignore[union-attr]

    def pause() -> None:
        recorder["pauses"] = int(recorder["pauses"]) + 1

    code = run_launcher(
        options or LaunchOptions(root=root, port=8899),
        spawn=spawn,
        probe=probe or (lambda url: (True, "")),
        opener=recorded_opener,  # type: ignore[arg-type]
        out=recorder["messages"].append,  # type: ignore[arg-type]
        err=recorder["messages"].append,  # type: ignore[arg-type]
        dialog=dialog,
        pause=pause,
        sleeper=lambda seconds: None,
        clock=lambda: 0.0,
        preflight_runner=(lambda path: report or OkReport()),
        is_free=lambda port: True,
        environ={"PATH": ""},
        host_os=HOST_OS,
        python_executable="C:/fake/python.exe",
    )
    recorder["spawned"] = spawned["count"]
    recorder["process"] = proc
    return code, recorder


def test_run_launcher_success(portable_root: Path) -> None:
    code, recorder = run_with(
        portable_root, probe=lambda url: (True, ""), opener=lambda url: True
    )
    assert code == 0
    assert recorder["spawned"] == 1
    assert recorder["opened"] == ["http://127.0.0.1:8899/"]
    plan = recorder["plans"][0]  # type: ignore[index]
    assert "serve" in plan.argv and "--portable" in plan.argv
    assert (portable_root / "config" / "portable-state.json").is_file()
    process = recorder["process"]
    assert isinstance(process, FakeProcess) and process.waited is True
    assert any("Daemon healthy" in str(message) for message in recorder["messages"])  # type: ignore[union-attr]


def test_run_launcher_no_browser(portable_root: Path) -> None:
    code, recorder = run_with(
        portable_root,
        options=LaunchOptions(root=portable_root, port=8899, no_browser=True),
    )
    assert code == 0
    assert recorder["opened"] == []


def test_run_launcher_health_failure(portable_root: Path) -> None:
    ticks = itertools.count(0, 0.5)
    recorder: dict[str, object] = {"dialogs": [], "pauses": 0, "messages": []}
    proc = FakeProcess()
    code = run_launcher(
        LaunchOptions(root=portable_root, port=8899, timeout_s=1.0),
        spawn=lambda plan: proc,
        probe=lambda url: (False, "connection refused"),
        opener=lambda url: True,
        out=recorder["messages"].append,  # type: ignore[arg-type]
        err=recorder["messages"].append,  # type: ignore[arg-type]
        dialog=lambda message: recorder["dialogs"].append(message),  # type: ignore[arg-type]
        pause=lambda: recorder.update(pauses=1),  # type: ignore[arg-type]
        sleeper=lambda seconds: None,
        clock=lambda: next(ticks),
        preflight_runner=lambda path: OkReport(),
        is_free=lambda port: True,
        environ={"PATH": ""},
        host_os=HOST_OS,
        python_executable="C:/fake/python.exe",
    )
    assert code == EXIT_SERVER
    assert proc.terminated is True
    assert recorder["dialogs"]


def test_run_launcher_preflight_refusal(portable_root: Path) -> None:
    code, recorder = run_with(portable_root, report=RefuseReport())
    assert code == EXIT_PREFLIGHT
    assert recorder["spawned"] == 0
    assert recorder["dialogs"]


def test_run_launcher_missing_root(tmp_path: Path) -> None:
    code, recorder = run_with(tmp_path / "missing")
    assert code == EXIT_MISSING_RUNTIME
    assert recorder["spawned"] == 0
    assert recorder["dialogs"]


def test_run_launcher_relocates_moved_bundle(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    populate_runtime_dirs(root)
    old_root = str(tmp_path / "old-place")
    write_marker(root, recorded_root=old_root)
    sep = "\\" if os.name == "nt" else "/"
    make_file(root / "avd" / "Pixel.ini", f"[AVD]\npath={old_root}{sep}avd{sep}Pixel.avd\n")
    code, recorder = run_with(root)
    assert code == 0
    ini = (root / "avd" / "Pixel.ini").read_text(encoding="utf-8")
    assert old_root not in ini
    assert str(root.resolve()) in ini
    assert any("Relocated" in str(message) for message in recorder["messages"])  # type: ignore[union-attr]


def test_main_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--port", "0"]) == EXIT_USAGE
    captured = capsys.readouterr()
    assert "Usage" in captured.err or "port" in captured.err


def test_main_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    assert "AVD Fleet portable launcher" in capsys.readouterr().out


def test_report_failure_helpers() -> None:
    calls = {"dialogs": [], "pauses": 0, "errors": []}
    report_failure(
        "boom",
        err=calls["errors"].append,  # type: ignore[arg-type]
        dialog=lambda message: calls["dialogs"].append(message),  # type: ignore[arg-type]
        pause=lambda: calls.update(pauses=1),  # type: ignore[arg-type]
        interactive=True,
    )
    assert calls["errors"] == ["boom"]
    assert calls["dialogs"] == ["boom"]
    assert calls["pauses"] == 1
    assert show_error_dialog("x", dialog=lambda message: None) is True
    assert open_ui("http://127.0.0.1/", opener=lambda url: True) is True
    assert open_ui("http://127.0.0.1/", opener=lambda url: False) is False


def test_launchers_have_no_baked_root(tmp_path: Path) -> None:
    for spec in all_launchers():
        assert str(tmp_path) not in spec.content
        assert "C:\\Users\\" not in spec.content


@pytest.mark.skipif(not IS_WINDOWS, reason="the PowerShell Unblock-File line only runs on Windows")
def test_windows_unblock_one_liner_removes_motw(tmp_path: Path) -> None:
    import os
    import subprocess

    from avdfleet.portable.checks.quarantine import has_mark_of_the_web

    spec = windows_launcher()
    line = next(
        item
        for item in spec.content.splitlines()
        if item.lstrip().startswith("powershell ") and "-Command" in item
    )
    script = line.split('-Command "', 1)[1]
    if script.endswith('"'):
        script = script[:-1]
    target = make_file(tmp_path / "tool.exe", "MZ")
    untouched = make_file(tmp_path / "notes.txt", "text")
    for path in (target, untouched):
        with open(str(path) + ":Zone.Identifier", "w", encoding="utf-8") as stream:
            stream.write("[ZoneTransfer]\nZoneId=3\n")
    environment = dict(os.environ)
    environment["ROOT"] = str(tmp_path)
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=environment,
        )
    except FileNotFoundError:  # pragma: no cover - Windows always has PowerShell 5.1
        pytest.skip("powershell is not installed")
    assert completed.returncode == 0, completed.stderr
    assert "Removed Mark-of-the-Web" in completed.stdout
    assert has_mark_of_the_web(target) is False
    # Non-executable files are intentionally not touched (fast first run).
    assert has_mark_of_the_web(untouched) is True
