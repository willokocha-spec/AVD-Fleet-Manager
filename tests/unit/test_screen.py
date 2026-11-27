"""Unit tests for ``avdfleet.screen`` — gRPC discovery, adb fallback, input.

All external boundaries are replaced by in-process fakes: an adb runner, a fake
runtime and an in-process screen transport.  ``grpcio`` is not installed on this
host; the tests assert the service imports and degrades cleanly, and exercise
the gRPC transport through an injected fake ``grpc`` module + stub factory.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from avdfleet.core.errors import NotFoundError, ScreenError
from avdfleet.core.models import AvdInfo, InstancePorts, InstanceState, OperationResult
from avdfleet.core.paths import Paths
from avdfleet.screen import (
    AdbScreencap,
    FrameCache,
    GrpcEmulatorTransport,
    GrpcEndpoint,
    ScrcpyConfig,
    ScrcpyLauncher,
    ScreenService,
    build_key_argv,
    build_scrcpy_command,
    build_swipe_argv,
    build_tap_argv,
    build_text_argv,
    discover_endpoints,
    find_scrcpy,
    grpc_client,
    is_png,
    normalize_keycode,
    parse_discovery_file,
    quote_device_argument,
    repair_shell_png,
    validate_png,
)
from avdfleet.screen.screencap import has_png_ihdr

_IHDR_DATA = b"\x00\x00\x04\x38\x00\x00\x07\x80\x08\x02\x00\x00\x00"


def make_png(tag: bytes = b"") -> bytes:
    """A distinct PNG-looking frame: signature + IHDR chunk + tag bytes."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0d"
        + b"IHDR"
        + _IHDR_DATA
        + b"\x00\x00\x00\x00"
        + tag
    )


PNG_A = make_png(b"frame-a")
PNG_B = make_png(b"frame-b")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class RunResult:
    def __init__(self, returncode: int = 0, stdout: str | bytes = b"", stderr: str | bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeAdb:
    """Process-boundary fake for adb capture/input commands."""

    def __init__(
        self,
        *,
        exec_out: RunResult | None = None,
        shell: RunResult | None = None,
        input_result: RunResult | None = None,
    ) -> None:
        self.exec_out = exec_out if exec_out is not None else RunResult(0, PNG_A)
        self.shell = shell if shell is not None else RunResult(0, PNG_A)
        self.input_result = input_result if input_result is not None else RunResult(0, b"", b"")
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], timeout_s: float | None = None) -> RunResult:
        command = [str(item) for item in argv]
        self.calls.append(command)
        if "exec-out" in command:
            return self.exec_out
        if "shell" in command:
            tail = command[command.index("shell") + 1 :]
            if tail[:2] == ["screencap", "-p"]:
                return self.shell
            return self.input_result
        return RunResult(0, b"", b"")

    @property
    def capture_calls(self) -> list[list[str]]:
        return [call for call in self.calls if "screencap" in call]

    @property
    def input_calls(self) -> list[list[str]]:
        return [call for call in self.calls if "input" in call]


class FakeRuntime:
    def __init__(self) -> None:
        self._infos: dict[str, AvdInfo] = {}

    def define(
        self,
        name: str,
        *,
        running: bool = True,
        console: int = 5554,
        pid: int = 4242,
        pod: Any = None,
        grpc_endpoint: str | None = None,
    ) -> AvdInfo:
        ports = InstancePorts(console=console, adb=console + 1) if running else None
        info = AvdInfo(
            name=name,
            image_id="system-images;android-35;google_apis_playstore;x86_64",
            state=InstanceState.RUNNING if running else InstanceState.STOPPED,
            ports=ports,
            adb_serial=f"emulator-{console}" if running else None,
            pid=pid if running else None,
            boot_completed=running,
            uptime_seconds=30.0 if running else None,
            grpc_endpoint=grpc_endpoint,
        )
        self._infos[name] = info
        return info

    def status(self, name: str) -> AvdInfo:
        if name not in self._infos:
            raise NotFoundError(f"instance {name!r} was not found")
        return self._infos[name]

    def list_running(self) -> list[AvdInfo]:
        return [info for info in self._infos.values() if info.state is InstanceState.RUNNING]


class FakeChannel:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeStub:
    def __init__(self, frame: bytes = PNG_A) -> None:
        self.frame = frame
        self.timeouts: list[float | None] = []

    def get_screenshot(self, timeout: float | None = None) -> Any:
        self.timeouts.append(timeout)
        return SimpleNamespace(image=self.frame)


class FakeTransport:
    """In-process ScreenTransport double."""

    def __init__(
        self,
        *,
        available: bool = True,
        capture_frame: bytes = PNG_A,
        capture_error: Exception | None = None,
        stream_frames: list[bytes] | None = None,
        stream_error_after: int | None = None,
    ) -> None:
        self._available = available
        self.capture_frame = capture_frame
        self.capture_error = capture_error
        self.stream_frames = list(stream_frames if stream_frames is not None else [])
        self.stream_error_after = stream_error_after
        self.capture_calls = 0
        self.stream_calls = 0
        self.closed = False

    def available(self) -> bool:
        return self._available

    def capture(self) -> bytes:
        self.capture_calls += 1
        if self.capture_error is not None:
            raise self.capture_error
        return self.capture_frame

    async def stream(self, *, fps: float = 5.0):  # type: ignore[no-untyped-def]
        self.stream_calls += 1
        for index, frame in enumerate(self.stream_frames):
            yield frame
            if self.stream_error_after is not None and index + 1 >= self.stream_error_after:
                raise ScreenError("grpc stream disconnected")

    def close(self) -> None:
        self.closed = True


def make_paths(tmp_path) -> Paths:  # type: ignore[no-untyped-def]
    return Paths(
        root=tmp_path,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
        sdk_root=tmp_path / "sdk",
        avd_home=tmp_path / "avd",
    )


class ScreenHarness:
    def __init__(
        self,
        tmp_path,  # type: ignore[no-untyped-def]
        *,
        adb: FakeAdb | None = None,
        runtime: FakeRuntime | None = None,
        transport: Any | None = None,
        transport_factory: Any | None = None,
        discovery_dirs: list[Any] | None = None,
        fps: float = 50.0,
    ) -> None:
        self.paths = make_paths(tmp_path)
        self.adb = adb or FakeAdb()
        self.runtime = runtime or FakeRuntime()
        if not self.runtime._infos:  # type: ignore[attr-defined]
            self.runtime.define("avd-02", running=True)
            self.runtime.define("avd-01", running=False)
        self.service = ScreenService(
            self.runtime,
            self.paths,
            adb=self.adb,
            transport=transport,
            transport_factory=transport_factory,
            discovery_dirs=discovery_dirs or [],
            discovery_include_defaults=False,
            fps=fps,
        )


# ---------------------------------------------------------------------------
# gRPC discovery
# ---------------------------------------------------------------------------


def test_parse_legacy_emu_grpc_discovery_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    directory = tmp_path / "android-lab" / "emu-grpc"
    directory.mkdir(parents=True)
    path = directory / "5554.ini"
    path.write_text("grpc.port=8554\ngrpc.token=fake-grpc-token\n", encoding="utf-8")

    endpoint = parse_discovery_file(path)

    assert endpoint is not None
    assert endpoint.port == 8554
    assert endpoint.address == "localhost"
    assert endpoint.token == "fake-grpc-token"
    assert "fake-grpc-token" not in repr(endpoint)
    assert "token" not in endpoint.as_dict()
    assert endpoint.metadata() == (("authorization", "Bearer fake-grpc-token"),)
    assert endpoint.host_port == "localhost:8554"


def test_discover_endpoints_matches_serial(tmp_path) -> None:  # type: ignore[no-untyped-def]
    directory = tmp_path / "emu-grpc"
    directory.mkdir()
    (directory / "5554.ini").write_text("grpc.port=8554\ngrpc.token=t1\n", encoding="utf-8")
    (directory / "5556.ini").write_text("grpc.port=8556\ngrpc.token=t2\n", encoding="utf-8")

    endpoints = discover_endpoints(dirs=[directory], serial="emulator-5556", include_defaults=False)

    assert [endpoint.port for endpoint in endpoints] == [8556]


def test_discover_endpoints_per_avd_grpc_ini(tmp_path) -> None:  # type: ignore[no-untyped-def]
    avd_home = tmp_path / "avd"
    avd_dir = avd_home / "avd-02.avd"
    avd_dir.mkdir(parents=True)
    (avd_home / "avd-02.ini").write_text("target=android-35\n", encoding="utf-8")
    (avd_dir / "grpc.ini").write_text(
        "grpc.address=127.0.0.1:8556\ngrpc.token=abc\nport.serial=5554\n",
        encoding="utf-8",
    )

    endpoints = discover_endpoints(
        avd_home=avd_home,
        avd_name="avd-02",
        serial="emulator-5554",
        include_defaults=False,
    )

    assert len(endpoints) == 1
    assert endpoints[0].port == 8556
    assert endpoints[0].address == "127.0.0.1"


def test_parse_discovery_file_rejects_unusable_files(tmp_path) -> None:  # type: ignore[no-untyped-def]
    bad = tmp_path / "bad.ini"
    bad.write_text("grpc.port=not-a-number\ngrpc.token=x\n", encoding="utf-8")
    assert parse_discovery_file(bad) is None
    assert parse_discovery_file(tmp_path / "missing.ini") is None
    no_port = tmp_path / "no-port.ini"
    no_port.write_text("grpc.token=only-token\n", encoding="utf-8")
    assert parse_discovery_file(no_port) is None


def test_optional_grpc_import_is_not_required() -> None:
    assert grpc_client is not None  # importing the module never raises
    transport = GrpcEmulatorTransport(GrpcEndpoint(address="localhost", port=8554, token="token"))
    if grpc_client.grpc is None:
        assert transport.available() is False
        with pytest.raises(ScreenError, match="grpcio"):
            transport.capture()
    else:  # pragma: no cover - grpcio is optional and absent on this host
        pytest.skip("grpcio is installed in this environment")


def test_grpc_transport_uses_injected_stub_factory(tmp_path) -> None:  # type: ignore[no-untyped-def]
    channel = FakeChannel()
    created: dict[str, Any] = {}

    def fake_insecure_channel(target: str) -> FakeChannel:
        created["target"] = target
        return channel

    stub = FakeStub(PNG_A)

    def factory(opened_channel: FakeChannel, metadata: Any) -> FakeStub:
        created["channel"] = opened_channel
        created["metadata"] = metadata
        return stub

    endpoint = GrpcEndpoint(address="127.0.0.1", port=8556, token="secret-token", serial="emulator-5554")
    transport = GrpcEmulatorTransport(
        endpoint,
        grpc_module=SimpleNamespace(insecure_channel=fake_insecure_channel),
        stub_factory=factory,
        timeout_s=3.5,
    )

    assert transport.available() is True
    assert transport.capture() == PNG_A
    assert created["target"] == "127.0.0.1:8556"
    assert created["metadata"] == (("authorization", "Bearer secret-token"),)
    assert stub.timeouts == [3.5]
    transport.close()
    assert channel.closed is True
    assert "secret-token" not in repr(endpoint)


# ---------------------------------------------------------------------------
# PNG helpers
# ---------------------------------------------------------------------------


def test_png_validation_and_repair() -> None:
    assert is_png(PNG_A)
    assert has_png_ihdr(PNG_A)
    assert validate_png(PNG_A) == PNG_A
    with pytest.raises(ScreenError):
        validate_png(b"<html>not a png</html>")
    assert repair_shell_png(b"garbage") is None
    mangled = PNG_A.replace(b"\n", b"\r\n")
    repaired = repair_shell_png(mangled)
    assert repaired is not None and is_png(repaired)


# ---------------------------------------------------------------------------
# capture()
# ---------------------------------------------------------------------------


def test_capture_uses_grpc_transport_when_available(tmp_path) -> None:  # type: ignore[no-untyped-def]
    transport = FakeTransport(capture_frame=PNG_A)
    harness = ScreenHarness(tmp_path, transport=transport)

    data = harness.service.capture("avd-02")

    assert data == PNG_A
    assert transport.capture_calls == 1
    assert harness.adb.capture_calls == []
    cached = harness.service.cache_path("avd-02")
    assert cached is not None and cached.is_file() and cached.read_bytes() == PNG_A


def test_capture_falls_back_to_adb_exec_out(tmp_path) -> None:  # type: ignore[no-untyped-def]
    harness = ScreenHarness(tmp_path)

    data = harness.service.capture("avd-02")

    assert data == PNG_A
    call = harness.adb.capture_calls[0]
    assert call[:2] == ["adb", "-s"]
    assert call[2] == "emulator-5554"
    assert call[3:6] == ["exec-out", "screencap", "-p"]
    assert harness.service.cache_path("avd-02") is not None


def test_capture_falls_back_when_grpc_fails(tmp_path) -> None:  # type: ignore[no-untyped-def]
    transport = FakeTransport(available=True, capture_error=ScreenError("grpc getScreenshot failed"))
    harness = ScreenHarness(tmp_path, transport=transport)

    data = harness.service.capture("avd-02")

    assert data == PNG_A
    assert harness.adb.capture_calls
    assert any("grpc" in warning.lower() or "gRPC" in warning for warning in harness.service.warnings)


def test_capture_shell_fallback_repairs_crlf(tmp_path) -> None:  # type: ignore[no-untyped-def]
    mangled = PNG_A.replace(b"\n", b"\r\n")
    adb = FakeAdb(
        exec_out=RunResult(1, b"", b"exec-out unsupported"),
        shell=RunResult(0, mangled, b""),
    )
    harness = ScreenHarness(tmp_path, adb=adb)

    data = harness.service.capture("avd-02")

    assert data == PNG_A
    modes = [call[3] for call in adb.capture_calls]
    assert modes == ["exec-out", "shell"]


def test_capture_rejects_non_png_output(tmp_path) -> None:  # type: ignore[no-untyped-def]
    adb = FakeAdb(exec_out=RunResult(0, b"<html>error</html>"), shell=RunResult(0, b"still not a png"))
    harness = ScreenHarness(tmp_path, adb=adb)

    with pytest.raises(ScreenError, match="screen capture failed"):
        harness.service.capture("avd-02")

    assert harness.service.cache_path("avd-02") is None


def test_capture_refuses_stopped_instance(tmp_path) -> None:  # type: ignore[no-untyped-def]
    harness = ScreenHarness(tmp_path)
    with pytest.raises(ScreenError, match="not running"):
        harness.service.capture("avd-01")


def test_capture_unknown_instance_is_a_screen_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    harness = ScreenHarness(tmp_path)
    with pytest.raises(ScreenError, match="not found"):
        harness.service.capture("ghost")


def test_capture_discovers_endpoint_and_calls_factory(tmp_path) -> None:  # type: ignore[no-untyped-def]
    directory = tmp_path / "emu-grpc"
    directory.mkdir()
    (directory / "5554.ini").write_text("grpc.port=8554\ngrpc.token=secret-token\n", encoding="utf-8")
    seen: dict[str, Any] = {}

    def factory(info: AvdInfo, endpoint: GrpcEndpoint | None) -> FakeTransport:
        seen["info"] = info
        seen["endpoint"] = endpoint
        return FakeTransport(capture_frame=PNG_B)

    harness = ScreenHarness(tmp_path, transport_factory=factory, discovery_dirs=[directory])

    data = harness.service.capture("avd-02")

    assert data == PNG_B
    assert seen["endpoint"] is not None
    assert seen["endpoint"].port == 8554
    assert seen["endpoint"].token == "secret-token"
    assert harness.adb.capture_calls == []
    assert "secret-token" not in repr(harness.service.warnings)


def test_available_reports_running_stopped_and_unknown(tmp_path) -> None:  # type: ignore[no-untyped-def]
    harness = ScreenHarness(tmp_path)
    assert harness.service.available("avd-02") is True
    assert harness.service.available("avd-01") is False
    assert harness.service.available("ghost") is False


# ---------------------------------------------------------------------------
# input
# ---------------------------------------------------------------------------


def test_tap_swipe_and_key_commands(tmp_path) -> None:  # type: ignore[no-untyped-def]
    harness = ScreenHarness(tmp_path)

    tap = harness.service.tap("avd-02", 10, 20)
    swipe = harness.service.swipe("avd-02", 1, 2, 3, 4, duration_ms=400)
    key = harness.service.key("avd-02", "home")

    assert tap.ok is True
    assert tap.data["action"] == "tap"
    assert harness.adb.input_calls[0] == [
        "adb",
        "-s",
        "emulator-5554",
        "shell",
        "input",
        "tap",
        "10",
        "20",
    ]
    assert harness.adb.input_calls[1][4:] == ["input", "swipe", "1", "2", "3", "4", "400"]
    assert swipe.ok is True
    assert harness.adb.input_calls[2][4:] == ["input", "keyevent", "KEYCODE_HOME"]
    assert key.ok is True
    assert isinstance(tap, OperationResult)


def test_text_input_is_quoted_for_the_device_shell(tmp_path) -> None:  # type: ignore[no-untyped-def]
    harness = ScreenHarness(tmp_path)

    result = harness.service.text("avd-02", "hello; rm -rf / #")

    assert result.ok is True
    call = harness.adb.input_calls[0]
    assert call[:6] == ["adb", "-s", "emulator-5554", "shell", "input", "text"]
    assert len(call) == 7  # user data never becomes extra argv entries
    assert call[6] == "'hello; rm -rf / #'"


def test_text_quoting_helpers() -> None:
    assert quote_device_argument("hello world") == "'hello world'"
    assert quote_device_argument("it's fine") == "'it'\\''s fine'"
    with pytest.raises(ScreenError):
        quote_device_argument("line1\nline2")
    with pytest.raises(ScreenError):
        quote_device_argument("")
    assert build_text_argv("emulator-5554", "a b")[-1] == "'a b'"
    assert build_tap_argv("emulator-5554", 1, 2)[3:] == ["shell", "input", "tap", "1", "2"]
    assert build_swipe_argv("emulator-5554", 1, 2, 3, 4, duration_ms=250)[-1] == "250"
    assert build_key_argv("emulator-5554", "BACK")[-1] == "KEYCODE_BACK"


def test_keycode_normalization() -> None:
    assert normalize_keycode("HOME") == "KEYCODE_HOME"
    assert normalize_keycode("KEYCODE_BACK") == "KEYCODE_BACK"
    assert normalize_keycode("3") == "3"
    with pytest.raises(ScreenError):
        normalize_keycode("not a key")
    with pytest.raises(ScreenError):
        normalize_keycode("")


def test_input_refused_when_instance_not_running(tmp_path) -> None:  # type: ignore[no-untyped-def]
    harness = ScreenHarness(tmp_path)

    result = harness.service.tap("avd-01", 1, 1)

    assert result.ok is False
    assert "not running" in result.message
    assert harness.adb.calls == []


def test_input_reports_runner_failure(tmp_path) -> None:  # type: ignore[no-untyped-def]
    adb = FakeAdb(input_result=RunResult(1, b"", b"adb: device offline"))
    harness = ScreenHarness(tmp_path, adb=adb)

    result = harness.service.tap("avd-02", 1, 1)

    assert result.ok is False
    assert "device offline" in result.message


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


async def test_stream_uses_grpc_frames_until_normal_end(tmp_path) -> None:  # type: ignore[no-untyped-def]
    transport = FakeTransport(stream_frames=[PNG_A, PNG_B])
    harness = ScreenHarness(tmp_path, transport=transport)

    frames: list[bytes] = []
    async for frame in harness.service.stream("avd-02", fps=100):
        frames.append(frame)

    assert frames == [PNG_A, PNG_B]
    assert harness.adb.capture_calls == []
    assert harness.service.cache_path("avd-02") is not None


async def test_stream_yields_frames_and_stops_on_cancellation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    harness = ScreenHarness(tmp_path, adb=FakeAdb())  # adb fallback: no gRPC transport

    frames: list[bytes] = []

    async def consume() -> None:
        async for frame in harness.service.stream("avd-02", fps=200):
            frames.append(frame)

    task = asyncio.create_task(consume())
    for _ in range(200):
        await asyncio.sleep(0.005)
        if frames:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert frames
    assert frames[0] == PNG_A


async def test_stream_aclose_stops_iteration(tmp_path) -> None:  # type: ignore[no-untyped-def]
    harness = ScreenHarness(tmp_path)
    stream = harness.service.stream("avd-02", fps=200)

    first = await anext(stream)
    assert first == PNG_A
    await stream.aclose()


async def test_stream_falls_back_to_adb_after_grpc_disconnect(tmp_path) -> None:  # type: ignore[no-untyped-def]
    transport = FakeTransport(stream_frames=[PNG_A], stream_error_after=1)
    harness = ScreenHarness(tmp_path, transport=transport, adb=FakeAdb())

    frames: list[bytes] = []
    stream = harness.service.stream("avd-02", fps=500)
    try:
        while len(frames) < 2:
            frames.append(await anext(stream))
    finally:
        await stream.aclose()

    assert frames[0] == PNG_A
    assert frames[1] == PNG_A  # adb fallback frame (same fake image)
    assert harness.adb.capture_calls  # the adb path was used after the disconnect
    assert any("disconnected" in warning for warning in harness.service.warnings)


async def test_stream_refuses_stopped_instance(tmp_path) -> None:  # type: ignore[no-untyped-def]
    harness = ScreenHarness(tmp_path)
    stream = harness.service.stream("avd-01")
    with pytest.raises(ScreenError, match="not running"):
        await anext(stream)


# ---------------------------------------------------------------------------
# scrcpy fallback
# ---------------------------------------------------------------------------


def test_scrcpy_command_builder() -> None:
    argv = build_scrcpy_command(
        "emulator-5554",
        window_title="AVD Fleet — avd-02",
        config=ScrcpyConfig(max_size=1080, max_fps=30, no_audio=True),
    )
    assert argv[0] == "scrcpy"
    assert argv[argv.index("--serial") + 1] == "emulator-5554"
    assert argv[argv.index("--window-title") + 1] == "AVD Fleet — avd-02"
    assert argv[argv.index("--max-size") + 1] == "1080"
    assert argv[argv.index("--max-fps") + 1] == "30"
    assert "--no-audio" in argv
    with pytest.raises(ScreenError):
        build_scrcpy_command("")


def test_scrcpy_detection_and_launch_command() -> None:
    assert find_scrcpy(which=lambda name: None) is None
    missing = ScrcpyLauncher(which=lambda name: None)
    assert missing.available() is False
    assert missing.launch("emulator-5554", spawn=False).ok is False

    launcher = ScrcpyLauncher(which=lambda name: "/usr/bin/scrcpy")
    assert launcher.available() is True
    result = launcher.launch("emulator-5554", avd_name="avd-02", spawn=False)
    assert result.ok is True
    assert result.data["argv"][0] == "/usr/bin/scrcpy"
    assert result.data["argv"][2] == "emulator-5554"


def test_screen_service_scrcpy_helpers(tmp_path) -> None:  # type: ignore[no-untyped-def]
    harness = ScreenHarness(tmp_path)
    assert harness.service.scrcpy_available() is False or isinstance(
        harness.service.scrcpy_available(), bool
    )
    result = harness.service.launch_scrcpy("avd-02", spawn=False)
    assert isinstance(result, OperationResult)
    with pytest.raises(ScreenError, match="not running"):
        harness.service.scrcpy_command("avd-01")


def test_screen_service_without_injected_runner_uses_subprocess_default(
    tmp_path,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    """Container-style wiring: only adb_executable is provided, no runner."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append([str(item) for item in argv])
        if "exec-out" in argv:
            return SimpleNamespace(returncode=0, stdout=PNG_A, stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    from avdfleet.screen import screencap as screencap_module

    monkeypatch.setattr(screencap_module.subprocess, "run", fake_run)
    runtime = FakeRuntime()
    runtime.define("avd-02", running=True)
    service = ScreenService(runtime, make_paths(tmp_path), adb=None, adb_executable="adb")

    assert service.available("avd-02") is True
    data = service.capture("avd-02")
    assert data == PNG_A
    assert calls and calls[0][:4] == ["adb", "-s", "emulator-5554", "exec-out"]
    tap = service.tap("avd-02", 5, 6)
    assert tap.ok is True
    assert calls[-1][4:] == ["input", "tap", "5", "6"]


def test_screen_service_satisfies_core_protocol(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from avdfleet.core.interfaces import ScreenClient

    harness = ScreenHarness(tmp_path)
    assert isinstance(harness.service, ScreenClient)


# ---------------------------------------------------------------------------
# cache + adb screencap helper
# ---------------------------------------------------------------------------


def test_adb_screencap_requires_a_serial() -> None:
    capture = AdbScreencap(FakeAdb())
    with pytest.raises(ScreenError):
        capture.capture("")


def test_frame_cache_round_trip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cache = FrameCache(tmp_path / "frames")
    path = cache.store("avd-02", PNG_A)
    assert path is not None and path.is_file()
    assert cache.get("avd-02") == PNG_A
    assert cache.get_path("avd-02") == path
    assert cache.age("avd-02") is not None
    cache.clear("avd-02")
    assert cache.get("avd-02") is None

    disabled = FrameCache(tmp_path / "disabled", enabled=False)
    assert disabled.store("avd-02", PNG_A) is None
