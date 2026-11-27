"""Regression tests for the detached per-instance bridge.

Background: a bridge owned by the short-lived ``avdfleet instances start`` CLI
process dies before the emulator probes its proxy, and the emulator then prints
"Proxy will be ignored !" and lets the guest egress on the *host* network -- a
G1 isolation failure.  :class:`DetachedBridgeManager` keeps the listener alive in
a worker process for the lifetime of the instance and publishes it in a state
file.  These tests pin that lifecycle, the file-based handoff and the listener
token handling.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from avdfleet.core.paths import Paths
from avdfleet.proxy.bridge import detached
from avdfleet.proxy.bridge.detached import DetachedBridgeManager
from avdfleet.proxy.bridge.worker import read_state, state_path, stop_path, write_state


@pytest.fixture()
def paths(tmp_path: Path) -> Paths:
    return Paths(
        root=tmp_path,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
        sdk_root=tmp_path / "sdk",
        avd_home=tmp_path / "avd",
    ).ensure()


def _sleeping_child() -> subprocess.Popen[bytes]:
    """A live process that is not this test process (safe target for stop())."""
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# State-file handoff
# ---------------------------------------------------------------------------


def test_state_file_is_the_source_of_truth(paths: Paths) -> None:
    manager = DetachedBridgeManager(paths=paths)
    write_state(
        state_path(paths, "avd-01"),
        {
            "pid": os.getpid(),
            "instance": "avd-01",
            "proxy_id": "px_demo",
            "listen_host": "127.0.0.1",
            "listen_port": 7811,
            "socks_port": 7812,
            "mode": "http",
            "upstream": "http://gate.example.com:8080",
            "token": "tok_abc",
            "created_at": time.time(),
        },
    )

    config = manager.get("avd-01")
    assert config is not None
    assert config.listen_port == 7811
    assert config.upstream == "http://gate.example.com:8080"
    assert config.mode == "http"
    assert manager.http_port("avd-01") == 7811
    assert manager.socks_port("avd-01") == 7812
    assert manager.bridge_url("avd-01") == "http://127.0.0.1:7811"
    assert manager.socks_url("avd-01") == "socks5://127.0.0.1:7812"
    assert manager.local_proxy_credentials("avd-01") == ("avd-01", "tok_abc")
    assert manager.local_proxy_url("avd-01") == "http://avd-01:tok_abc@127.0.0.1:7811"
    assert [item.instance for item in manager.list_active()] == ["avd-01"]
    assert manager.active_ports() == {"avd-01": (7811, 7812)}


def test_state_file_permissions_are_owner_only(paths: Paths) -> None:
    if os.name == "nt":  # pragma: no cover - Windows has no POSIX mode bits
        pytest.skip("POSIX permissions are not modelled on Windows")
    target = state_path(paths, "avd-01")
    write_state(target, {"pid": os.getpid(), "instance": "avd-01"})
    assert target.stat().st_mode & 0o077 == 0


def test_stale_state_is_ignored_and_cleaned_up(paths: Paths) -> None:
    manager = DetachedBridgeManager(paths=paths)
    target = state_path(paths, "avd-dead")
    # A pid that cannot be alive: the manager must not report a dead bridge.
    write_state(target, {"pid": 999_999_999, "instance": "avd-dead", "listen_port": 7999})

    assert manager.get("avd-dead") is None
    assert manager.list_active() == []
    assert not target.exists()


def test_stop_removes_state_and_terminates_the_worker(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detached, "_STOP_GRACE_S", 0.2)
    child = _sleeping_child()
    try:
        manager = DetachedBridgeManager(paths=paths)
        target = state_path(paths, "avd-stop")
        write_state(target, {"pid": child.pid, "instance": "avd-stop", "listen_port": 7998})
        assert manager.get("avd-stop") is not None

        manager.stop("avd-stop")

        assert not target.exists()
        assert not stop_path(paths, "avd-stop").exists()
        assert child.wait(timeout=20) is not None
    finally:
        if child.poll() is None:  # pragma: no cover - safety net
            child.kill()


def test_stop_is_idempotent_for_an_unknown_instance(paths: Paths) -> None:
    DetachedBridgeManager(paths=paths).stop("avd-never-started")


def test_close_leaves_running_workers_alone(paths: Paths) -> None:
    """close() must not tear the bridge down: that is the whole point."""
    manager = DetachedBridgeManager(paths=paths)
    write_state(
        state_path(paths, "avd-keep"),
        {"pid": os.getpid(), "instance": "avd-keep", "listen_port": 7997},
    )
    manager.close()
    assert state_path(paths, "avd-keep").exists()


def test_ensure_rejects_a_disabled_proxy(paths: Paths) -> None:
    from avdfleet.core.errors import ValidationError
    from avdfleet.core.models import ProxyConfig

    manager = DetachedBridgeManager(paths=paths)
    proxy = ProxyConfig(id="px_off", name="off", host="gate.example.com", port=8080, enabled=False)
    with pytest.raises(ValidationError):
        manager.ensure("avd-01", proxy)


# ---------------------------------------------------------------------------
# Real worker process (direct upstream, fully hermetic)
# ---------------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        body = b"bridge-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _proxy_get(port: int, target: str, *, token: tuple[str, str] | None) -> tuple[int, bytes]:
    request = [f"GET {target} HTTP/1.1", f"Host: {target.split('/')[2]}"]
    if token is not None:
        raw = base64.b64encode(f"{token[0]}:{token[1]}".encode()).decode()
        request.append(f"Proxy-Authorization: Basic {raw}")
    request.extend(["Connection: close", "", ""])
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall("\r\n".join(request).encode("latin-1"))
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    payload = b"".join(chunks)
    head, _, body = payload.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1]) if head.startswith(b"HTTP/") else 0
    return status, body


def test_worker_process_serves_and_stops(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: spawn the real worker, use the bridge, then stop it."""
    for name in ("AVDFLEET_CONFIG_FILE", "AVDFLEET_VAULT_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AVDFLEET_DATA_DIR", str(paths.data_dir))
    monkeypatch.setenv("AVDFLEET_CONFIG_DIR", str(paths.config_dir))
    monkeypatch.setenv("AVDFLEET_CACHE_DIR", str(paths.cache_dir))
    monkeypatch.setenv("AVDFLEET_LOG_DIR", str(paths.log_dir))
    monkeypatch.setattr(detached, "_STOP_GRACE_S", 5.0)

    origin_port = _free_port()
    origin = http.server.ThreadingHTTPServer(("127.0.0.1", origin_port), _Handler)
    thread = threading.Thread(target=origin.serve_forever, daemon=True)
    thread.start()
    manager = DetachedBridgeManager(paths=paths, ready_timeout_s=60.0)
    try:
        config = manager.ensure("avd-direct", None)
        assert config.mode == "direct"
        assert config.listen_port > 0

        target = f"http://127.0.0.1:{origin_port}/probe"
        status, _ = _proxy_get(config.listen_port, target, token=None)
        assert status == 407, "the loopback listener must demand its token"

        credentials = manager.local_proxy_credentials("avd-direct")
        assert credentials is not None
        status, body = _proxy_get(config.listen_port, target, token=credentials)
        assert status == 200
        assert body == b"bridge-ok"

        assert [item.instance for item in manager.list_active()] == ["avd-direct"]
        state = read_state(state_path(paths, "avd-direct"))
        assert state is not None and state["pid"] != os.getpid()
    finally:
        manager.stop("avd-direct")
        origin.shutdown()
        origin.server_close()

    assert manager.get("avd-direct") is None
    assert not state_path(paths, "avd-direct").exists()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", config.listen_port), timeout=2).close()


def test_worker_state_never_contains_the_upstream_password(paths: Paths) -> None:
    """The state file carries ports and the loopback token only."""
    secret = "s3cret-upstream-password"
    target = state_path(paths, "avd-01")
    write_state(
        target,
        {
            "pid": os.getpid(),
            "instance": "avd-01",
            "proxy_id": "px_demo",
            "listen_port": 7811,
            "upstream": "http://gate.example.com:8080",
            "token": "tok_abc",
        },
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert secret not in target.read_text(encoding="utf-8")
    assert "password" not in payload
    assert payload["upstream"] == "http://gate.example.com:8080"
