"""Cross-process port isolation for detached proxy bridges.

Regression
----------
Every detached worker derived its first candidate port from
``Config.bridge_port_start`` (7800) and the listener socket was bound with
``SO_REUSEADDR``.  On Windows that option lets a second socket bind an address
a live listener already owns, so two instances started from two separate CLI
processes both published ``127.0.0.1:7800``; the later binder silently received
the first instance's emulator traffic (the guest then got the listener's 407 and
the bridge failed closed, but per-instance isolation was gone).  A parent
process cannot reserve a port for a child it has not started yet, so the worker
must own the bind: let the kernel choose the port (``port 0``) or bind with
exclusive semantics, publish the real value in the state file, and let the
parent read it back.

These tests drive real interpreters with different ``PYTHONHASHSEED`` values and
real ``avdfleet.proxy.bridge.worker`` processes in direct mode.  No emulator,
proxy, vault or outbound network is involved: the workers only bind loopback
listeners and publish their state files.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil
import pytest

from avdfleet.core.paths import Paths
from avdfleet.proxy.bridge.worker import bridge_state_dir, read_state, state_path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR = _REPO_ROOT / "src"

#: Runs one detached start/stop behind a real interpreter.  ``PYTHONHASHSEED``
#: differs per invocation, exactly like two unrelated CLI processes.
_DRIVER = r"""
import json
import os
import sys
from pathlib import Path

from avdfleet.core.paths import Paths
from avdfleet.proxy.bridge.detached import DetachedBridgeManager
from avdfleet.proxy.bridge.worker import read_state, state_path


def _paths() -> Paths:
    base = Path(os.environ["AVDFLEET_TEST_ROOT"])
    return Paths(
        root=base / "root",
        data_dir=base / "data",
        config_dir=base / "config",
        cache_dir=base / "cache",
        log_dir=base / "logs",
        sdk_root=base / "sdk",
        avd_home=base / "avd",
    ).ensure()


def main() -> int:
    action = sys.argv[1]
    instance = sys.argv[2]
    paths = _paths()
    result = {
        "action": action,
        "instance": instance,
        "state": str(state_path(paths, instance)),
        "pid": None,
        "port": None,
    }
    if action == "start":
        config = DetachedBridgeManager(paths=paths, ready_timeout_s=120.0).ensure(instance, None)
        state = read_state(Path(result["state"])) or {}
        result["pid"] = state.get("pid")
        result["port"] = config.listen_port
    elif action == "stop":
        DetachedBridgeManager(paths=paths).stop(instance)
    else:
        raise SystemExit(f"unknown action: {action}")
    print(json.dumps(result))
    return 0


raise SystemExit(main())
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_paths(tmp_path: Path) -> Paths:
    return Paths(
        root=tmp_path,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
        sdk_root=tmp_path / "sdk",
        avd_home=tmp_path / "avd",
    ).ensure()


def _driver_environment(root: Path, seed: int) -> dict[str, str]:
    env = dict(os.environ)
    pythonpath = [str(_SRC_DIR)]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env.update(
        {
            "PYTHONHASHSEED": str(seed),
            "PYTHONPATH": os.pathsep.join(pythonpath),
            "AVDFLEET_TEST_ROOT": str(root),
            "AVDFLEET_DATA_DIR": str(root / "data"),
            "AVDFLEET_CONFIG_DIR": str(root / "config"),
            "AVDFLEET_CACHE_DIR": str(root / "cache"),
            "AVDFLEET_LOG_DIR": str(root / "logs"),
        }
    )
    env.pop("AVDFLEET_CONFIG_FILE", None)
    env.pop("AVDFLEET_VAULT_PASSPHRASE", None)
    return env


def _spawn_driver(root: Path, *, seed: int, action: str, instance: str) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", _DRIVER, action, instance],
        env=_driver_environment(root, seed),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _driver_payload(process: subprocess.Popen[str]) -> dict[str, Any]:
    stdout, stderr = process.communicate(timeout=240)
    assert process.returncode == 0, (
        f"driver failed ({process.returncode}):\nstdout:\n{stdout}\nstderr:\n{stderr}"
    )
    payloads = [line for line in stdout.splitlines() if line.strip().startswith("{")]
    assert payloads, f"driver printed no result:\n{stdout}\n{stderr}"
    return json.loads(payloads[-1])


def _run_driver(root: Path, *, seed: int, action: str, instance: str) -> dict[str, Any]:
    return _driver_payload(_spawn_driver(root, seed=seed, action=action, instance=instance))


def _state(paths: Paths, instance: str) -> dict[str, Any]:
    data = read_state(state_path(paths, instance))
    assert data is not None, f"{instance} did not publish a state file"
    assert data.get("instance") == instance
    return data


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        return psutil.Process(pid).is_running()
    except psutil.Error:
        return False


def _port_bindable(port: int) -> bool:
    """Whether a plain (non-``SO_REUSEADDR``) bind can take *port*."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _worker_launches(paths: Paths) -> dict[str, set[int]]:
    """Map each worker's ``--state-file`` argument to the PIDs launched for it.

    Scanning processes (rather than only reading state files) is what proves a
    duplicate same-instance start did not leave a second, invisible listener
    behind on a port no state file names.  A venv ``python.exe`` trampoline can
    appear as two processes with an identical argv, so group by the exact
    ``--state-file`` argument instead of counting processes.
    """
    marker = str(paths.data_dir)
    launches: dict[str, set[int]] = {}
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            parts = [str(part) for part in (process.info.get("cmdline") or [])]
            if "avdfleet.proxy.bridge.worker" not in parts or "--state-file" not in parts:
                continue
            index = parts.index("--state-file")
            state_file = parts[index + 1] if index + 1 < len(parts) else ""
            if not state_file or marker not in state_file:
                continue
            launches.setdefault(state_file, set()).add(int(process.info["pid"]))
        except (psutil.Error, TypeError, ValueError):
            continue
    return launches


def _wait_for(predicate: Any, *, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _kill_pid(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    try:
        process = psutil.Process(pid)
        process.kill()
        process.wait(timeout=10)
    except psutil.Error:
        pass


def _kill_recorded_workers(paths: Paths) -> None:
    """Safety net: never leave a bridge worker (or its state file) behind."""
    launches = _worker_launches(paths)
    pids = {pid for group in launches.values() for pid in group}
    for path in sorted(bridge_state_dir(paths).glob("*.json")):
        try:
            pid = int((read_state(path) or {}).get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid:
            pids.add(pid)
    for pid in pids:
        _kill_pid(pid)
    _wait_for(lambda: not any(_pid_running(pid) for pid in pids), timeout=15.0)
    for path in sorted(bridge_state_dir(paths).glob("*")):
        try:
            path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------


def test_two_instances_never_share_a_listener_port(tmp_path: Path) -> None:
    """Two detached starts from two processes must publish distinct ports."""
    paths = _make_paths(tmp_path)
    first_instance, second_instance = "avd-port-a", "avd-port-b"
    try:
        first = _run_driver(tmp_path, seed=1, action="start", instance=first_instance)
        second = _run_driver(tmp_path, seed=777_777, action="start", instance=second_instance)

        state_a = _state(paths, first_instance)
        state_b = _state(paths, second_instance)
        port_a = int(state_a["listen_port"])
        port_b = int(state_b["listen_port"])
        pid_a = int(state_a["pid"])
        pid_b = int(state_b["pid"])

        assert first["state"] == str(state_path(paths, first_instance))
        assert first["port"] == port_a
        assert second["port"] == port_b
        assert port_a > 0 and port_b > 0
        assert port_a != port_b, (
            "both detached bridges published 127.0.0.1:"
            f"{port_a}; a second binder on the same port steals the first "
            "instance's emulator traffic, so per-instance isolation is not real"
        )
        assert pid_a != pid_b, "two instances must be served by two workers"
        assert _pid_running(pid_a) and _pid_running(pid_b)
        assert not _port_bindable(port_a), f"port {port_a} is not held by its worker"
        assert not _port_bindable(port_b), f"port {port_b} is not held by its worker"
        state_file_a = str(state_path(paths, first_instance))
        state_file_b = str(state_path(paths, second_instance))
        launches_before = _worker_launches(paths)
        assert set(launches_before) == {state_file_a, state_file_b}
        assert pid_a in launches_before[state_file_a]
        assert pid_b in launches_before[state_file_b]

        # A third start for an already-running instance reuses its worker: no
        # second listener, no second state file, no new port.
        third = _run_driver(tmp_path, seed=42_424, action="start", instance=first_instance)
        assert third["pid"] == pid_a, "the third start spawned a second worker for a live instance"
        assert third["port"] == port_a
        assert int(_state(paths, first_instance)["pid"]) == pid_a
        assert _worker_launches(paths) == launches_before, (
            "the third start created another listener for an already-running instance"
        )
        assert {path.name for path in bridge_state_dir(paths).glob("*.json")} == {
            state_path(paths, first_instance).name,
            state_path(paths, second_instance).name,
        }
        assert not _port_bindable(port_a)
        assert not _port_bindable(port_b)
    finally:
        _kill_recorded_workers(paths)


def test_concurrent_starts_get_race_free_ports(tmp_path: Path) -> None:
    """The allocation must also hold when both processes start at once."""
    paths = _make_paths(tmp_path)
    first_instance, second_instance = "avd-race-a", "avd-race-b"
    try:
        first = _spawn_driver(tmp_path, seed=11, action="start", instance=first_instance)
        second = _spawn_driver(tmp_path, seed=222_222, action="start", instance=second_instance)
        payload_a = _driver_payload(first)
        payload_b = _driver_payload(second)

        state_a = _state(paths, first_instance)
        state_b = _state(paths, second_instance)
        port_a = int(state_a["listen_port"])
        port_b = int(state_b["listen_port"])
        pid_a = int(state_a["pid"])
        pid_b = int(state_b["pid"])

        assert payload_a["port"] == port_a
        assert payload_b["port"] == port_b
        assert port_a != port_b, (
            f"concurrent starts both published 127.0.0.1:{port_a}: "
            "the allocation is not race-free across processes"
        )
        assert pid_a != pid_b
        state_file_a = str(state_path(paths, first_instance))
        state_file_b = str(state_path(paths, second_instance))
        launches = _worker_launches(paths)
        assert set(launches) == {state_file_a, state_file_b}
        assert pid_a in launches[state_file_a]
        assert pid_b in launches[state_file_b]
        assert _pid_running(pid_a) and _pid_running(pid_b)
        assert not _port_bindable(port_a)
        assert not _port_bindable(port_b)
    finally:
        _kill_recorded_workers(paths)


if __name__ == "__main__":  # pragma: no cover - direct invocation convenience
    pytest.main([__file__])
