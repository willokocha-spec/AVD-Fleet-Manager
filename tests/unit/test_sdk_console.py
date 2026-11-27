"""Unit tests for the emulator console TCP client (S1b).

A tiny threaded socket server (stdlib only) reproduces the real emulator
console line protocol: banner + ``OK``, ``auth <token>`` handshake, ``KO:``
errors, ``avd name``, snapshots and ``kill`` (which tears the connection
down).  This keeps the tests hermetic -- no emulator, no adb, no network.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from avdfleet.core.interfaces import EmulatorConsole as EmulatorConsoleProtocol
from avdfleet.platform.sdk.console import (
    TOKEN_FILENAME,
    EmulatorConsole,
    EmulatorConsoleAuthError,
    EmulatorConsoleError,
    console_token_paths,
    resolve_console_token,
)

BANNER = b"Android Console: type 'help' for a list of commands\r\n"


class FakeConsoleServer:
    """Minimal line-protocol console server for one port."""

    def __init__(self, *, token: str | None = "fake-token", silent: bool = False) -> None:
        self.token = token
        self.silent = silent
        self.commands: list[str] = []
        self._closing = threading.Event()
        self._connections: list[socket.socket] = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self._listener.settimeout(0.2)
        self.port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # -- lifecycle ------------------------------------------------------
    def _serve(self) -> None:
        while not self._closing.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            self._connections.append(connection)
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection: socket.socket) -> None:
        try:
            stream = connection.makefile("rwb")
            stream.write(BANNER)
            stream.write(b"OK\r\n")
            stream.flush()
            while True:
                raw = stream.readline()
                if not raw:
                    break
                command = raw.decode("utf-8", errors="replace").strip()
                if not command:
                    continue
                self.commands.append(command)
                if command.startswith("auth "):
                    supplied = command.split(" ", 1)[1].strip()
                    if self.token is None or supplied == self.token:
                        stream.write(b"OK\r\n")
                        stream.flush()
                    else:
                        stream.write(b"KO: authentication failed\r\n")
                        stream.flush()
                        break
                    continue
                if self.silent:
                    continue
                if command == "kill":
                    stream.write(b"OK\r\n")
                    stream.flush()
                    break
                if command == "avd name":
                    stream.write(b"avd-01\r\nOK\r\n")
                    stream.flush()
                    continue
                if command.startswith("snapshot save"):
                    stream.write(b"OK\r\n")
                    stream.flush()
                    continue
                if command.startswith("snapshot load"):
                    stream.write(b"OK\r\n")
                    stream.flush()
                    continue
                if command.startswith("snapshot delete"):
                    stream.write(b"OK\r\n")
                    stream.flush()
                    continue
                if command == "snapshot list":
                    stream.write(b"avdfleet-golden\r\nOK\r\n")
                    stream.flush()
                    continue
                if command == "help":
                    stream.write(b"avd name\r\nkill\r\nOK\r\n")
                    stream.flush()
                    continue
                stream.write(b"KO: unknown command\r\n")
                stream.flush()
        except OSError:
            pass
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def close(self) -> None:
        self._closing.set()
        try:
            self._listener.close()
        except OSError:
            pass
        for connection in self._connections:
            try:
                connection.close()
            except OSError:
                pass
        self._thread.join(timeout=2.0)


@pytest.fixture()
def console_servers():
    """Factory fixture that closes every created server at teardown."""
    servers: list[FakeConsoleServer] = []

    def make(**kwargs: object) -> FakeConsoleServer:
        server = FakeConsoleServer(**kwargs)  # type: ignore[arg-type]
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def _token_home(tmp_path: Path, token: str = "fake-token") -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / TOKEN_FILENAME).write_text(token + "\n", encoding="utf-8")
    return home


def _console(**kwargs: object) -> EmulatorConsole:
    options: dict[str, object] = {
        "connect_timeout_s": 2.0,
        "command_timeout_s": 2.0,
        "home": None,
        "environ": {"HOME": "", "USERPROFILE": ""},
    }
    options.update(kwargs)
    return EmulatorConsole(**options)  # type: ignore[arg-type]


def test_connect_authenticates_and_avd_name(console_servers, tmp_path: Path) -> None:
    server = console_servers(token="fake-token")
    console = EmulatorConsole(home=_token_home(tmp_path))
    console.connect(server.port, timeout_s=2.0)
    try:
        assert server.commands and server.commands[0] == "auth fake-token"
        assert console.avd_name() == "avd-01"
        assert isinstance(console, EmulatorConsoleProtocol)
    finally:
        console.close()
    assert console._sock is None


def test_token_discovered_from_fake_home_environment(console_servers, tmp_path: Path) -> None:
    server = console_servers(token="fake-token")
    home = _token_home(tmp_path)
    console = _console(environ={"AVDFLEET_FAKE_HOME": str(home), "HOME": "", "USERPROFILE": ""})
    console.connect(server.port, timeout_s=2.0)
    try:
        assert "auth fake-token" in server.commands
        assert console.avd_name() == "avd-01"
    finally:
        console.close()


def test_token_resolution_precedence_and_env_override(tmp_path: Path) -> None:
    home = _token_home(tmp_path, "home-token")
    other_home = tmp_path / "other"
    other_home.mkdir()
    paths = console_token_paths(home, environ={"AVDFLEET_FAKE_HOME": str(other_home), "HOME": str(other_home)})
    assert paths[0] == home / TOKEN_FILENAME
    assert resolve_console_token(home, environ={}) == "home-token"
    assert (
        resolve_console_token(
            environ={"ANDROID_EMULATOR_CONSOLE_AUTH_TOKEN": "env-token", "HOME": ""}
        )
        == "env-token"
    )
    assert resolve_console_token(tmp_path / "missing", environ={"HOME": "", "USERPROFILE": ""}) is None


def test_wrong_token_raises_auth_error(console_servers) -> None:
    server = console_servers(token="fake-token")
    console = _console(token="wrong-token")
    with pytest.raises(EmulatorConsoleAuthError) as excinfo:
        console.connect(server.port, timeout_s=2.0)
    assert "authentication failed" in str(excinfo.value).lower()
    assert console._sock is None


def test_unauthenticated_console_when_auth_disabled(console_servers) -> None:
    server = console_servers(token=None)
    console = _console(token=None, environ={"HOME": "", "USERPROFILE": ""})
    console.connect(server.port, timeout_s=2.0)
    try:
        assert console.avd_name() == "avd-01"
        assert not any(command.startswith("auth ") for command in server.commands)
    finally:
        console.close()


def test_ko_response_raises_console_error(console_servers) -> None:
    server = console_servers(token="fake-token")
    console = _console(token="fake-token")
    console.connect(server.port, timeout_s=2.0)
    try:
        with pytest.raises(EmulatorConsoleError) as excinfo:
            console.command("bogus")
        assert "unknown command" in str(excinfo.value).lower()
    finally:
        console.close()


def test_snapshot_save_load_and_list(console_servers) -> None:
    server = console_servers(token="fake-token")
    console = _console(token="fake-token")
    console.connect(server.port, timeout_s=2.0)
    try:
        assert console.save_snapshot("avdfleet-golden") == ""
        assert console.load_snapshot("avdfleet-golden") == ""
        assert console.list_snapshots() == ["avdfleet-golden"]
        assert "snapshot save avdfleet-golden" in server.commands
        assert "snapshot load avdfleet-golden" in server.commands
    finally:
        console.close()


def test_kill_closes_connection(console_servers) -> None:
    server = console_servers(token="fake-token")
    console = _console(token="fake-token")
    console.connect(server.port, timeout_s=2.0)
    console.kill()
    assert "kill" in server.commands
    assert console._sock is None


def test_context_manager_closes_socket(console_servers) -> None:
    server = console_servers(token="fake-token")
    with EmulatorConsole(token="fake-token") as console:
        console.connect(server.port, timeout_s=2.0)
        assert console.avd_name() == "avd-01"
        assert console._sock is not None
    assert console._sock is None


def test_connect_refused_raises_console_error(tmp_path: Path) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    console = _console(token="whatever")
    with pytest.raises(EmulatorConsoleError):
        console.connect(port, timeout_s=0.5)


def test_command_timeout_raises(console_servers) -> None:
    server = console_servers(token="fake-token", silent=True)
    console = _console(token="fake-token", command_timeout_s=0.2)
    console.connect(server.port, timeout_s=2.0)
    try:
        with pytest.raises(EmulatorConsoleError) as excinfo:
            console.command("avd name")
        assert "timed out" in str(excinfo.value).lower()
    finally:
        console.close()


def test_rejects_injection_and_invalid_names(console_servers) -> None:
    server = console_servers(token="fake-token")
    console = _console(token="fake-token")
    console.connect(server.port, timeout_s=2.0)
    try:
        with pytest.raises(EmulatorConsoleError):
            console.command("kill\nsnapshot save x")
        with pytest.raises(EmulatorConsoleError):
            console.command("   ")
        with pytest.raises(EmulatorConsoleError):
            console.save_snapshot("bad name")
        with pytest.raises(EmulatorConsoleError):
            console.load_snapshot("")
        assert "kill" not in server.commands
    finally:
        console.close()


def test_command_before_connect_raises() -> None:
    console = _console()
    with pytest.raises(EmulatorConsoleError):
        console.command("help")
    console.close()
    console.close()  # idempotent


def test_repr_masks_token(console_servers) -> None:
    server = console_servers(token="fake-token")
    console = _console(token="fake-token")
    console.connect(server.port, timeout_s=2.0)
    try:
        rendered = repr(console)
        assert "fake-token" not in rendered
        assert "***" in rendered
    finally:
        console.close()
