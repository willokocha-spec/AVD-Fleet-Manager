"""Cross-process state-file tests for the detached per-instance bridge.

The worker publishes its bound ports in ``<data>/bridge/<stem>.json``; every
later CLI invocation (``instances stop``, ``stop_all``, ``proxies bridges``)
finds the live worker through that file.  The stem must therefore be **stable
across processes**: Python randomises ``hash(str)`` per interpreter, so a
hash-based suffix makes a second process compute a different filename, miss the
worker, report success and leave the listener running (and a restart spawns yet
another worker).

These tests drive two real interpreters with different ``PYTHONHASHSEED``
values.  They also pin the fallback discovery of state files written under the
old hash-suffixed naming scheme and the orphan cleanup used by the daemon/doctor.
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
from avdfleet.proxy.bridge import detached
from avdfleet.proxy.bridge.detached import DetachedBridgeManager
from avdfleet.proxy.bridge.manager import BridgeManager
from avdfleet.proxy.bridge.worker import (
    bridge_state_dir,
    read_state,
    safe_instance_stem,
    state_path,
    write_state,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR = _REPO_ROOT / "src"

#: A tiny protocol-test driver run in its own interpreter.  ``DetachedBridgeManager``
#: spawns the real ``avdfleet.proxy.bridge.worker`` in direct mode, so no proxy,
#: vault or Android SDK is involved; the only variable is the interpreter's hash
#: seed.
_DRIVER = r"""
import json
import os
import sys
from pathlib import Path

from avdfleet.core.paths import Paths
from avdfleet.proxy.bridge.detached import DetachedBridgeManager
from avdfleet.proxy.bridge.worker import state_path


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
    port = None
    if action == "start":
        manager = DetachedBridgeManager(paths=paths, ready_timeout_s=120.0)
        port = manager.ensure(instance, None).listen_port
    elif action == "stop":
        DetachedBridgeManager(paths=paths).stop(instance)
    else:
        raise SystemExit(f"unknown action: {action}")
    print(
        json.dumps(
            {
                "action": action,
                "instance": instance,
                "state": str(state_path(paths, instance)),
                "port": port,
            }
        )
    )
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
    """Environment for a driver interpreter; ``PYTHONHASHSEED`` is the variable."""
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


def _run_driver(root: Path, *, seed: int, action: str, instance: str) -> dict[str, Any]:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", _DRIVER, action, instance],
        env=_driver_environment(root, seed),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, (
        f"driver {action!r} failed ({completed.returncode}):\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    payloads = [line for line in completed.stdout.splitlines() if line.strip().startswith("{")]
    assert payloads, f"driver {action!r} printed no result:\n{completed.stdout}\n{completed.stderr}"
    return json.loads(payloads[-1])


def _sleeping_child() -> subprocess.Popen[bytes]:
    """A live process that is not this test process (safe target for stop())."""
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        return psutil.Process(pid).is_running()
    except psutil.Error:
        return False


def _wait_for(predicate: Any, *, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _port_bindable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _kill_pid(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    try:
        process = psutil.Process(pid)
    except psutil.Error:
        return
    try:
        process.kill()
        process.wait(timeout=10)
    except psutil.Error:
        pass


def _kill_recorded_workers(paths: Paths) -> None:
    """Safety net: never leave a bridge worker (or its state file) behind."""
    directory = bridge_state_dir(paths)
    pids: set[int] = set()
    for path in sorted(directory.glob("*.json")):
        data = read_state(path) or {}
        try:
            pid = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid and _pid_running(pid):
            pids.add(pid)
            _kill_pid(pid)
    _wait_for(lambda: not any(_pid_running(pid) for pid in pids), timeout=10.0)
    for path in sorted(directory.glob("*")):
        try:
            path.unlink()
        except OSError:
            pass


def _legacy_state_file(paths: Paths, instance: str, suffix: str = "abcdef") -> Path:
    """A state file named exactly like the old ``hash()``-suffixed scheme."""
    expected = state_path(paths, instance)
    legacy = bridge_state_dir(paths) / f"{instance}-{suffix}.json"
    if legacy == expected:  # astronomically unlikely; keep the fallback exercised
        legacy = bridge_state_dir(paths) / f"{instance}-fedcba.json"
    return legacy


# ---------------------------------------------------------------------------
# Stable naming across processes
# ---------------------------------------------------------------------------


def test_stem_is_stable_across_python_hash_seeds(tmp_path: Path) -> None:
    """The digest must not depend on the interpreter's randomised ``hash()``."""
    stems = set()
    for seed in (1, 2, 987654321):
        env = _driver_environment(tmp_path, seed)
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable,
                "-c",
                (
                    "from avdfleet.proxy.bridge.worker import safe_instance_stem;"
                    "print(safe_instance_stem('avd-stable'))"
                ),
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        stems.add(completed.stdout.strip())
    assert stems == {safe_instance_stem("avd-stable")}


def test_worker_started_by_one_hash_seed_is_stopped_by_another(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    instance = "avd-hash-seed"
    state_file = state_path(paths, instance)
    pid = 0
    try:
        started = _run_driver(tmp_path, seed=1, action="start", instance=instance)
        assert started["state"] == str(state_file)

        state = read_state(state_file)
        assert state is not None, "the started worker did not publish its state file"
        pid = int(state["pid"])
        port = int(state["listen_port"])
        assert _pid_running(pid)
        assert not _port_bindable(port), "the worker should hold its published port"

        stopped = _run_driver(tmp_path, seed=987654, action="stop", instance=instance)

        assert _wait_for(lambda: not _pid_running(pid)), (
            "the second interpreter did not stop the worker: "
            f"it computed {stopped['state']!r} but the live worker published {state_file.name!r}"
        )
        assert read_state(state_file) is None
        assert _wait_for(lambda: _port_bindable(port), timeout=15.0), (
            f"port {port} is still held after the worker stopped"
        )
    finally:
        _kill_recorded_workers(paths)


def test_two_processes_starting_leave_exactly_one_worker(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    instance = "avd-single-worker"
    try:
        first = _run_driver(tmp_path, seed=1, action="start", instance=instance)
        second = _run_driver(tmp_path, seed=777777, action="start", instance=instance)

        assert second["state"] == first["state"], "both interpreters must name one state file"
        assert second["port"] == first["port"], "the second start must reuse the live listener"

        files = sorted(bridge_state_dir(paths).glob("*.json"))
        assert [path.name for path in files] == [Path(first["state"]).name]

        pids = set()
        for path in files:
            data = read_state(path) or {}
            if data.get("instance") == instance:
                pids.add(int(data["pid"]))
        assert len(pids) == 1, f"expected one worker pid, found {sorted(pids)}"
        assert _pid_running(pids.pop())
    finally:
        _kill_recorded_workers(paths)


# ---------------------------------------------------------------------------
# Fallback discovery of legacy state files
# ---------------------------------------------------------------------------


def test_legacy_hashed_state_file_is_found_and_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(detached, "_STOP_GRACE_S", 0.2)
    paths = _make_paths(tmp_path)
    legacy = _legacy_state_file(paths, "avd-legacy")
    assert legacy != state_path(paths, "avd-legacy")
    child = _sleeping_child()
    try:
        write_state(
            legacy,
            {
                "pid": child.pid,
                "instance": "avd-legacy",
                "listen_host": "127.0.0.1",
                "listen_port": 7990,
                "created_at": time.time(),
            },
        )
        manager = DetachedBridgeManager(paths=paths)

        # The computed (stable) path does not exist; the scan by instance does.
        assert manager.get("avd-legacy") is not None
        assert manager.http_port("avd-legacy") == 7990

        manager.stop("avd-legacy")

        assert child.wait(timeout=20) is not None
        assert not legacy.exists()
    finally:
        if child.poll() is None:  # pragma: no cover - safety net
            child.kill()


# ---------------------------------------------------------------------------
# Orphan cleanup
# ---------------------------------------------------------------------------


def test_orphan_cleanup_removes_dead_state_and_keeps_running_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(detached, "_STOP_GRACE_S", 0.2)
    paths = _make_paths(tmp_path)
    dead = state_path(paths, "avd-dead")
    write_state(dead, {"pid": 999_999_999, "instance": "avd-dead", "created_at": 1.0})

    running_child = _sleeping_child()
    orphan_child = _sleeping_child()
    try:
        write_state(
            state_path(paths, "avd-live"),
            {"pid": running_child.pid, "instance": "avd-live", "created_at": 1.0},
        )
        write_state(
            state_path(paths, "avd-orphan"),
            {"pid": orphan_child.pid, "instance": "avd-orphan", "created_at": 1.0},
        )

        manager = DetachedBridgeManager(paths=paths)
        cleaned = manager.cleanup_orphans(lambda name: name == "avd-live")

        assert "avd-dead" in cleaned
        assert not dead.exists()
        assert "avd-orphan" in cleaned
        assert orphan_child.wait(timeout=20) is not None
        assert not state_path(paths, "avd-orphan").exists()

        # A live worker whose instance is running is never killed or removed.
        assert "avd-live" not in cleaned
        assert state_path(paths, "avd-live").exists()
        assert running_child.poll() is None
    finally:
        for child in (running_child, orphan_child):
            if child.poll() is None:  # pragma: no cover - safety net
                child.kill()


def test_in_process_manager_lists_and_stops_detached_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(detached, "_STOP_GRACE_S", 0.2)
    paths = _make_paths(tmp_path)
    first = _sleeping_child()
    second = _sleeping_child()
    try:
        write_state(
            state_path(paths, "avd-daemon-list"),
            {"pid": first.pid, "instance": "avd-daemon-list", "created_at": time.time()},
        )
        write_state(
            state_path(paths, "avd-daemon-stop"),
            {"pid": second.pid, "instance": "avd-daemon-stop", "created_at": time.time()},
        )

        manager = BridgeManager(detached=DetachedBridgeManager(paths=paths))
        try:
            listed = [config.instance for config in manager.list_detached()]
            assert listed == ["avd-daemon-list", "avd-daemon-stop"]

            manager.stop_detached("avd-daemon-list")
            assert first.wait(timeout=20) is not None
            assert not state_path(paths, "avd-daemon-list").exists()

            # ``instances stop`` goes through ``bridge.stop()``: it must also
            # reach a worker that was started by a different CLI process.
            manager.stop("avd-daemon-stop")
            assert second.wait(timeout=20) is not None
            assert not state_path(paths, "avd-daemon-stop").exists()
        finally:
            manager.close()
    finally:
        for child in (first, second):
            if child.poll() is None:  # pragma: no cover - safety net
                child.kill()
