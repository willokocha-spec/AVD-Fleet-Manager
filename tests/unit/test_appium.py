"""Unit tests for the Appium lifecycle manager, port planning and capabilities.

A stdlib HTTP server script stands in for Appium: the manager is given an
injected executable resolver pointing at ``(python, fake_appium.py)`` and talks
to it exactly as it would to the real Appium 4 server (HTTP ``/status`` under
``/wd/hub``).  No Appium, Node, Android SDK or emulator is required.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import httpx
import psutil
import pytest

from avdfleet.appium import AppiumManager, AppiumPortPlanner, AppiumPorts
from avdfleet.core.config import Config
from avdfleet.core.errors import AppiumError, InstanceError
from avdfleet.core.models import AvdInfo, InstancePorts, InstanceState
from avdfleet.core.paths import Paths
from avdfleet.core.ports import PortAllocator, is_port_free

PYTHON = sys.executable

FAKE_APPIUM_SOURCE = dedent(
    """
    import json
    import os
    import socketserver
    import sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Server(ThreadingHTTPServer):
        # HTTPServer.server_bind() calls socket.getfqdn(), which can block for a
        # long time on CI runners with no reverse DNS.  The fake Appium server
        # only needs the numeric endpoint.
        def server_bind(self):
            socketserver.TCPServer.server_bind(self)
            host, port = self.server_address[:2]
            self.server_name = host
            self.server_port = port

    state_dir = os.environ.get("FAKE_APPIUM_STATE") or "."
    events_path = os.path.join(state_dir, "events.jsonl")
    args = sys.argv[1:]
    with open(events_path, "a", encoding="utf-8") as handle:
        print(json.dumps({"argv": args, "pid": os.getpid()}), file=handle)

    if "driver" in args:
        print("uiautomator2@3.0.0")
        sys.exit(0)
    if "--version" in args:
        print("4.0.0")
        sys.exit(0)

    port = None
    base_path = "/wd/hub"
    index = 0
    while index < len(args):
        if args[index] == "--port" and index + 1 < len(args):
            port = int(args[index + 1])
            index += 2
            continue
        if args[index] == "--base-path" and index + 1 < len(args):
            base_path = args[index + 1]
            index += 2
            continue
        index += 1
    if port is None:
        port = int(os.environ.get("FAKE_APPIUM_PORT", "4723"))
    sessions = int(os.environ.get("FAKE_APPIUM_SESSIONS", "2"))

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            if path in ("/status", base_path.rstrip("/") + "/status"):
                self._send(200, {"value": {"ready": True, "build": {"version": "4.0.0"}}})
            elif path == base_path.rstrip("/") + "/sessions":
                self._send(200, {"value": [{"id": "session-%d" % number} for number in range(sessions)]})
            else:
                self._send(404, {"value": {"error": "not found", "path": path}})

        def do_POST(self):
            self._send(200, {"value": {"sessionId": "fake-session", "capabilities": {}}})

        def do_DELETE(self):
            self._send(200, {"value": None})

        def log_message(self, *args):
            return

    server = _Server(("127.0.0.1", port), Handler)
    with open(os.path.join(state_dir, "server-state.json"), "w", encoding="utf-8") as handle:
        json.dump({"running": True, "port": port, "pid": os.getpid()}, handle)
    print("fake appium listening on %d" % port, flush=True)
    server.serve_forever()
    """
).strip()


def _free_port_start() -> int:
    """Pick a start for an Appium port block that is currently free-ish."""
    for candidate in range(40000, 64000, 500):
        if all(is_port_free(port) for port in (candidate, candidate + 1, candidate + 2)):
            return candidate
    return 40000  # pragma: no cover - port space exhausted


def _wait_until(predicate, *, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture()
def appium_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state_dir = tmp_path / "fake-appium"
    state_dir.mkdir()
    script = tmp_path / "fake_appium.py"
    script.write_text(FAKE_APPIUM_SOURCE, encoding="utf-8")
    monkeypatch.setenv("FAKE_APPIUM_STATE", str(state_dir))
    monkeypatch.setenv("FAKE_APPIUM_SESSIONS", "2")
    paths = Paths.for_portable(tmp_path / "bundle")
    config = Config(appium_port_start=_free_port_start())

    def events() -> list[dict]:
        path = state_dir / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def make_manager(**kwargs) -> AppiumManager:
        kwargs.setdefault("config", config)
        kwargs.setdefault("paths", paths)
        kwargs.setdefault("executable_resolver", lambda: [PYTHON, str(script)])
        kwargs.setdefault("readiness_timeout_s", 30.0)
        kwargs.setdefault("poll_interval_s", 0.1)
        kwargs.setdefault(
            "http_client_factory",
            lambda: httpx.Client(timeout=httpx.Timeout(2.0), trust_env=False),
        )
        return AppiumManager(**kwargs)

    return SimpleNamespace(
        tmp_path=tmp_path,
        state_dir=state_dir,
        script=script,
        paths=paths,
        config=config,
        make_manager=make_manager,
        events=events,
    )


def _running_avd(name: str = "avd-01", *, serial: str = "emulator-5554") -> AvdInfo:
    return AvdInfo(
        name=name,
        state=InstanceState.RUNNING,
        adb_serial=serial,
        boot_completed=True,
        ports=InstancePorts(
            console=5554,
            adb=5555,
            grpc=8554,
            appium_server=4723,
            system_port=8200,
            mjpeg=9300,
        ),
    )


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


def test_start_status_stop_lifecycle(appium_env: SimpleNamespace) -> None:
    manager = appium_env.make_manager()
    try:
        status = manager.start()
    except AppiumError as exc:
        logs = "\n".join(
            f"{path.name}: {path.read_text(encoding='utf-8', errors='replace')}"
            for path in sorted((appium_env.paths.logs_dir / "appium").glob("*.log"))
        )
        pytest.fail(f"{exc}\n--- appium logs ---\n{logs}")
    try:
        assert status.running is True
        assert status.port is not None
        assert status.pid is not None and psutil.pid_exists(status.pid)
        assert status.version == "4.0.0"
        assert status.url == f"http://127.0.0.1:{status.port}/wd/hub"
        assert status.drivers == ["uiautomator2@3.0.0"]
        assert status.sessions == 2
        assert status.message == ""

        spawn_events = [event for event in appium_env.events() if "--port" in event["argv"]]
        assert len(spawn_events) == 1
        argv = spawn_events[0]["argv"]
        assert argv[argv.index("--address") + 1] == "127.0.0.1"
        assert argv[argv.index("--port") + 1] == str(status.port)
        assert argv[argv.index("--base-path") + 1] == "/wd/hub"

        log_path = appium_env.paths.logs_dir / "appium" / f"appium-{status.port}.log"
        assert log_path.exists()
        assert "fake appium listening" in log_path.read_text(encoding="utf-8")
        state_file = appium_env.paths.data_dir / "appium" / "server.json"
        state_payload = json.loads(state_file.read_text(encoding="utf-8"))
        assert state_payload["port"] == status.port
        assert state_payload["pid"] == status.pid

        second = manager.start()
        assert second.running is True
        assert second.port == status.port
        assert len([event for event in appium_env.events() if "--port" in event["argv"]]) == 1

        pid = status.pid
        stopped = manager.stop()
        assert stopped.running is False
        assert state_file.exists() is False
        assert _wait_until(lambda: is_port_free(status.port), timeout=10.0)
        assert not psutil.pid_exists(pid)
        assert manager.status().running is False

        again = manager.stop()
        assert again.running is False
        assert "not running" in again.message
    finally:
        manager.close()


def test_no_appium_installed_degrades_gracefully(appium_env: SimpleNamespace) -> None:
    manager = appium_env.make_manager(executable_resolver=lambda: None)
    status = manager.status()
    assert status.running is False
    assert "not installed" in status.message
    assert manager.is_installed() is False
    started = manager.start()
    assert started.running is False
    assert "not installed" in started.message
    assert manager.stop().running is False


def test_start_errors_when_requested_port_is_busy(appium_env: SimpleNamespace) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        busy_port = listener.getsockname()[1]
        manager = appium_env.make_manager()
        try:
            with pytest.raises(AppiumError) as excinfo:
                manager.start(port=busy_port)
            assert "already in use" in str(excinfo.value)
            assert excinfo.value.http_status == 500
            assert manager.status().running is False
        finally:
            manager.close()


def test_close_stops_a_running_server(appium_env: SimpleNamespace) -> None:
    manager = appium_env.make_manager()
    status = manager.start()
    assert status.running
    manager.close()
    assert manager.status().running is False
    assert _wait_until(lambda: is_port_free(status.port), timeout=10.0)


def test_manager_implements_core_appium_protocol(appium_env: SimpleNamespace) -> None:
    from avdfleet.core.interfaces import AppiumManager as AppiumManagerProtocol

    manager = appium_env.make_manager()
    try:
        assert isinstance(manager, AppiumManagerProtocol)
    finally:
        manager.close()


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def test_build_capabilities_for_running_instance(appium_env: SimpleNamespace) -> None:
    manager = appium_env.make_manager(chromedriver_executable="/opt/chromedriver")
    avd = _running_avd()
    capabilities = manager.build_capabilities(avd)
    assert capabilities["platformName"] == "Android"
    assert capabilities["appium:automationName"] == "UiAutomator2"
    assert capabilities["appium:deviceName"] == "avd-01"
    assert capabilities["appium:udid"] == "emulator-5554"
    assert capabilities["appium:systemPort"] == 8200
    assert capabilities["appium:mjpegServerPort"] == 9300
    assert capabilities["appium:noReset"] is False
    assert capabilities["appium:autoGrantPermissions"] is True
    assert capabilities["appium:newCommandTimeout"] == 120
    assert capabilities["appium:adbExecTimeout"] == 20000
    assert capabilities["appium:suppressKillServer"] is True
    assert capabilities["appium:chromedriverExecutable"] == "/opt/chromedriver"


def test_build_capabilities_derives_udid_from_console_port(appium_env: SimpleNamespace) -> None:
    manager = appium_env.make_manager()
    avd = _running_avd()
    avd.adb_serial = None
    capabilities = manager.build_capabilities(avd)
    assert capabilities["appium:udid"] == "emulator-5554"


def test_build_capabilities_rejects_non_running_instance(appium_env: SimpleNamespace) -> None:
    manager = appium_env.make_manager()
    for state in (InstanceState.STOPPED, InstanceState.BOOTING, InstanceState.DEFINED):
        avd = _running_avd()
        avd.state = state
        with pytest.raises(InstanceError) as excinfo:
            manager.build_capabilities(avd)
        assert "not running" in str(excinfo.value)


def test_build_capabilities_allocates_ports_when_missing(appium_env: SimpleNamespace) -> None:
    manager = appium_env.make_manager()
    avd = AvdInfo(name="avd-02", state=InstanceState.RUNNING, adb_serial="emulator-5556")
    capabilities = manager.build_capabilities(avd)
    assert isinstance(capabilities["appium:systemPort"], int)
    assert capabilities["appium:systemPort"] != capabilities["appium:mjpegServerPort"]


# ---------------------------------------------------------------------------
# Port planning
# ---------------------------------------------------------------------------


def test_port_planner_allocates_unique_ports(appium_env: SimpleNamespace) -> None:
    manager = appium_env.make_manager()
    try:
        avds = [AvdInfo(name=f"avd-{index:02d}", state=InstanceState.RUNNING) for index in range(3)]
        plans = [manager.plan_ports(avd) for avd in avds]
        every_port = [port for plan in plans for port in plan.all_ports]
        assert len(every_port) == len(set(every_port)) == 9
        for plan in plans:
            assert plan.system_port != plan.mjpeg_port
            assert plan.server != plan.system_port
            assert plan.server != plan.mjpeg_port
        assert manager.plan_ports(avds[0]) == plans[0]
        assert manager.plan_ports(avds[0], ports=avds[0].ports) == plans[0]
    finally:
        manager.close()


def test_port_planner_honours_runtime_ports() -> None:
    planner = AppiumPortPlanner(appium_start=_free_port_start())
    runtime_ports = InstancePorts(
        console=5554,
        adb=5555,
        appium_server=4723,
        system_port=8200,
        mjpeg=9300,
    )
    planned = planner.plan("avd-01", ports=runtime_ports)
    assert planned == AppiumPorts(server=4723, system_port=8200, mjpeg_port=9300)


def test_port_planner_uses_injected_core_allocator() -> None:
    allocator = PortAllocator(start=6000, end=6100, appium_start=_free_port_start())
    planner = AppiumPortPlanner(allocator)
    first = planner.plan("avd-01")
    second = planner.plan("avd-02")
    assert len(set(first.all_ports + second.all_ports)) == 6
    leased = allocator.get("avd-01")
    assert leased is not None
    assert leased.appium_server == first.server
    assert leased.system_port == first.system_port
    assert leased.mjpeg == first.mjpeg_port
    planner.release("avd-01")
    assert allocator.get("avd-01") is None


def test_port_planner_server_port_busy_error() -> None:
    planner = AppiumPortPlanner(appium_start=_free_port_start())
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        busy = listener.getsockname()[1]
        with pytest.raises(AppiumError):
            planner.allocate_server(preferred=busy)
        assert is_port_free(busy) is False


# ---------------------------------------------------------------------------
# adb forward cleanup
# ---------------------------------------------------------------------------


def _adb_recorder(fail: bool = False):
    calls: list[list[str]] = []

    def runner(argv):
        calls.append([str(part) for part in argv])
        return subprocess.CompletedProcess(
            argv,
            1 if fail else 0,
            stdout="",
            stderr="error: something failed" if fail else "",
        )

    return calls, runner


def _resolver_for_serial(serial: str):
    def resolve(name: str):
        if name == "avd-01":
            return AvdInfo(
                name=name,
                state=InstanceState.RUNNING,
                adb_serial=serial,
                ports=InstancePorts(console=5554, adb=5555),
            )
        return None

    return resolve


def test_cleanup_forwards_remove_all_and_reverse(appium_env: SimpleNamespace) -> None:
    calls, runner = _adb_recorder()
    manager = appium_env.make_manager(adb_runner=runner, adb_command="adb")
    try:
        result = manager.cleanup_forwards("avd-01", serial="emulator-5554")
        assert result.ok is True
        assert len(calls) == 2
        assert calls[0] == ["adb", "-s", "emulator-5554", "forward", "--remove-all"]
        assert calls[1] == ["adb", "-s", "emulator-5554", "reverse", "--remove-all"]
        assert result.data["serial"] == "emulator-5554"
        assert result.data["remove_all"] is True
        assert len(result.data["commands"]) == 2
    finally:
        manager.close()


def test_cleanup_forwards_targeted_ports(appium_env: SimpleNamespace) -> None:
    calls, runner = _adb_recorder()
    manager = appium_env.make_manager(
        adb_runner=runner,
        adb_command="adb",
        instance_resolver=_resolver_for_serial("emulator-5554"),
    )
    try:
        avd = AvdInfo(
            name="avd-01",
            state=InstanceState.RUNNING,
            adb_serial="emulator-5554",
            ports=InstancePorts(
                console=5554,
                adb=5555,
                appium_server=4723,
                system_port=8200,
                mjpeg=9300,
            ),
        )
        manager.plan_ports(avd)
        result = manager.cleanup_forwards("avd-01", remove_all=False)
        assert result.ok is True
        flattened = [" ".join(call) for call in calls]
        assert "adb -s emulator-5554 forward --remove tcp:8200" in flattened
        assert "adb -s emulator-5554 forward --remove tcp:9300" in flattened
        assert "adb -s emulator-5554 reverse --remove tcp:8200" in flattened
        assert "adb -s emulator-5554 forward --list" in flattened
    finally:
        manager.close()


def test_cleanup_forwards_reports_adb_failure(appium_env: SimpleNamespace) -> None:
    _calls, runner = _adb_recorder(fail=True)
    manager = appium_env.make_manager(adb_runner=runner, adb_command="adb")
    try:
        result = manager.cleanup_forwards("avd-01", serial="emulator-5554")
        assert result.ok is False
        assert "failures" in result.message
    finally:
        manager.close()


def test_cleanup_forwards_unknown_instance(appium_env: SimpleNamespace) -> None:
    calls, runner = _adb_recorder()
    manager = appium_env.make_manager(
        adb_runner=runner,
        adb_command="adb",
        instance_resolver=_resolver_for_serial("emulator-5554"),
    )
    try:
        result = manager.cleanup_forwards("avd-99")
        assert result.ok is False
        assert "not found" in result.message
        assert calls == []
    finally:
        manager.close()


def test_release_instance_cleans_and_releases_ports(appium_env: SimpleNamespace) -> None:
    calls, runner = _adb_recorder()
    manager = appium_env.make_manager(adb_runner=runner, adb_command="adb")
    try:
        avd = AvdInfo(
            name="avd-01",
            state=InstanceState.RUNNING,
            adb_serial="emulator-5554",
            ports=InstancePorts(
                console=5554,
                adb=5555,
                appium_server=4723,
                system_port=8200,
                mjpeg=9300,
            ),
        )
        manager.plan_ports(avd)
        result = manager.release_instance("avd-01")
        assert result.ok is True
        assert calls
        replanned = manager.plan_ports(
            AvdInfo(name="avd-01", state=InstanceState.RUNNING, adb_serial="emulator-5554")
        )
        assert isinstance(replanned, AppiumPorts)
    finally:
        manager.close()
