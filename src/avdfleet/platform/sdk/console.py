"""Emulator console TCP client.

The Android emulator exposes a line-oriented telnet-ish console on an even
port (``console_port``).  Modern emulators require an authentication handshake
with the token from ``~/.emulator_console_auth_token`` before most commands
are accepted.  avdfleet uses the console for exactly the things adb cannot do
reliably: graceful ``kill``, the running AVD's name, and snapshot save/load.

The implementation is deliberately small and dependency free:

* connect timeout and per-command timeout are both enforced;
* the token never reaches a log line (``redact`` + masked ``repr``);
* ``KO:`` responses raise :class:`EmulatorConsoleError` instead of being
  returned as data;
* snapshot/AVD names are validated so a caller can never inject extra console
  commands through a newline;
* the object is a context manager and always closes its socket.

``relocate``/N1 can construct it without knowing the home directory: the token
is discovered from ``AVDFLEET_FAKE_HOME``, ``ANDROID_EMULATOR_HOME``,
``HOME``/``USERPROFILE`` and, only for the real process environment,
``Path.home()`` (in that order), matching both the fakes contract
(``<fake_home>/.emulator_console_auth_token``) and a real host
(``~/.emulator_console_auth_token``).

A caller-supplied ``environ`` is authoritative: it is never merged with (or
completed from) the real process environment, so a hermetic mapping with empty
``HOME``/``USERPROFILE`` cannot silently fall back to the host user's token
file.
"""

from __future__ import annotations

import os
import socket
import time
from collections.abc import Mapping
from pathlib import Path

from avdfleet.core.errors import ToolError
from avdfleet.core.logging import get_logger, redact

logger = get_logger("platform.sdk.console")

DEFAULT_CONSOLE_HOST = "127.0.0.1"
DEFAULT_CONNECT_TIMEOUT_S = 5.0
DEFAULT_COMMAND_TIMEOUT_S = 10.0
TOKEN_FILENAME = ".emulator_console_auth_token"

#: Environment variables that may carry the token directly (kept for tests and
#: for callers that manage the emulator's home directory themselves).
TOKEN_ENV_VARS = ("ANDROID_EMULATOR_CONSOLE_AUTH_TOKEN", "EMULATOR_CONSOLE_AUTH_TOKEN")

#: Environment variables whose value is a directory that may hold the token
#: file, in discovery order.
TOKEN_HOMES = (
    "AVDFLEET_FAKE_HOME",
    "ANDROID_EMULATOR_HOME",
    "ANDROID_USER_HOME",
    "ANDROID_SDK_HOME",
    "EMULATOR_HOME",
)

_MAX_RESPONSE_BYTES = 1 << 20  # 1 MiB safety valve for a runaway console


class EmulatorConsoleError(ToolError):
    """The emulator console could not be reached or returned an error."""

    code = "emulator_console_error"
    http_status = 503


class EmulatorConsoleAuthError(EmulatorConsoleError):
    """The emulator console authentication handshake failed."""

    code = "emulator_console_auth_error"
    http_status = 503


class _ConsoleReadTimeout(EmulatorConsoleError):
    """Internal: a read exceeded its deadline."""


class _ConsoleEOF(EmulatorConsoleError):
    """Internal: the peer closed the connection."""


# ---------------------------------------------------------------------------
# Token discovery
# ---------------------------------------------------------------------------


def _token_directory(
    value: str | Path,
    env: Mapping[str, str],
    *,
    allow_real_home: bool,
) -> Path | None:
    """Resolve one candidate directory without escaping *env*.

    ``~`` is expanded from the supplied environment.  When that environment
    declares no home directory the candidate is dropped instead of quietly
    falling back to the real user profile (``Path.home()``), which would leak
    the host's token file into a hermetic call.
    """
    raw = str(value)
    if raw.startswith("~"):
        suffix = raw[1:]
        if suffix and suffix[0] not in "/\\":
            # ``~user`` needs the real account database; only the real
            # process environment is allowed to resolve it.
            if not allow_real_home:
                return None
            try:
                return Path(raw).expanduser()
            except (RuntimeError, KeyError):  # pragma: no cover - exotic sandboxes
                return None
        declared_home = env.get("HOME") or env.get("USERPROFILE")
        if declared_home:
            raw = declared_home.rstrip("/\\") + suffix
        elif allow_real_home:
            try:
                return Path(raw).expanduser()
            except (RuntimeError, KeyError):  # pragma: no cover - exotic sandboxes
                return None
        else:
            return None
    return Path(raw)


def console_token_paths(
    home: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> list[Path]:
    """Candidate token-file paths, most specific first (deterministic order).

    When *environ* is supplied it is the sole source of environment facts: the
    real process environment and ``Path.home()`` are never consulted.
    """
    real_environment = environ is None
    env = dict(os.environ if environ is None else environ)
    directories: list[str | Path] = []
    if home:
        directories.append(home)
    for variable in TOKEN_HOMES:
        value = env.get(variable)
        if value:
            directories.append(value)
    user_home = env.get("HOME") or env.get("USERPROFILE")
    if user_home:
        directories.append(user_home)
    candidates: list[Path] = []
    for directory in directories:
        resolved = _token_directory(directory, env, allow_real_home=real_environment)
        if resolved is not None:
            candidates.append(resolved / TOKEN_FILENAME)
    if real_environment:
        try:
            candidates.append(Path.home() / TOKEN_FILENAME)
        except (RuntimeError, KeyError):  # pragma: no cover - exotic sandboxes
            pass
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def resolve_console_token(
    home: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Read the console auth token, or ``None`` when authentication is disabled.

    Precedence: token environment variables, then token files (the explicit
    *home* first, then directories declared by *environ*).  A supplied
    *environ* is authoritative: no real-host value is used as a fallback, so
    the returned token is ``None`` whenever the caller's environment has none.
    """
    env = dict(os.environ if environ is None else environ)
    for variable in TOKEN_ENV_VARS:
        value = env.get(variable)
        if value:
            token = value.strip()
            if token:
                return token
    for path in console_token_paths(home, environ=environ):
        try:
            token = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if token:
            return token
    return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class EmulatorConsole:
    """Line-protocol client for one emulator console port.

    Typical use::

        with EmulatorConsole() as console:
            console.connect(5554)
            console.kill()

    ``token`` and ``home`` are optional overrides for tests and portable
    deployments; otherwise discovery is automatic (see module docstring).  An
    empty ``token`` disables the ``auth`` handshake, and a supplied ``environ``
    replaces (never extends) the process environment used for discovery.
    """

    def __init__(
        self,
        *,
        host: str = DEFAULT_CONSOLE_HOST,
        token: str | None = None,
        home: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
    ) -> None:
        self.host = host
        self._explicit_token = token
        self.home = Path(home) if home is not None else None
        # ``None`` keeps discovery on the real process environment; a
        # caller-supplied mapping is authoritative and never completed from
        # ``os.environ``.
        self._environ: Mapping[str, str] | None = None if environ is None else dict(environ)
        self.connect_timeout_s = float(connect_timeout_s)
        self.command_timeout_s = float(command_timeout_s)
        self.port: int | None = None
        self._token: str | None = None
        self._authenticated = False
        self._sock: socket.socket | None = None
        self._buffer = b""

    # -- lifecycle ------------------------------------------------------
    def connect(self, port: int, *, timeout_s: float | None = None) -> None:
        """Open the TCP connection and run the auth handshake."""
        self.close()
        self.port = int(port)
        if not 1 <= self.port <= 65535:
            raise EmulatorConsoleError(f"invalid emulator console port: {port}")
        timeout = self.connect_timeout_s if timeout_s is None else float(timeout_s)
        self._token = (
            resolve_console_token(self.home, environ=self._environ)
            if self._explicit_token is None
            else self._explicit_token.strip() or None
        )
        try:
            sock = socket.create_connection((self.host, self.port), timeout=timeout)
        except OSError as exc:
            raise EmulatorConsoleError(
                f"could not connect to emulator console at {self.host}:{self.port}: "
                f"{redact(str(exc))}",
                hint="Check that the emulator is running and that the console port is correct.",
            ) from exc
        sock.settimeout(timeout)
        self._sock = sock
        self._buffer = b""
        try:
            self._handshake(timeout)
        except Exception:
            self.close()
            raise
        self._authenticated = True
        logger.debug("connected to emulator console at %s:%s", self.host, self.port)

    def close(self) -> None:
        """Close the socket and reset per-connection state (idempotent)."""
        sock, self._sock = self._sock, None
        self._buffer = b""
        self._authenticated = False
        self._token = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self) -> EmulatorConsole:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"EmulatorConsole(host={self.host!r}, port={self.port!r}, "
            f"token={'***' if self._token else ''}, authenticated={self._authenticated})"
        )

    # -- commands -------------------------------------------------------
    def command(self, command: str) -> str:
        """Send one console command and return the response text (without ``OK``).

        Raises :class:`EmulatorConsoleError` on ``KO:`` responses, timeouts or a
        closed connection.
        """
        self._require_connected()
        line = command.strip()
        if not line:
            raise EmulatorConsoleError("empty emulator console command")
        if "\n" in line or "\r" in line:
            raise EmulatorConsoleError("emulator console commands must be a single line")
        self._send_line(line)
        return self._read_response(command=line)

    def kill(self) -> None:
        """Kill the emulator via the console and close the connection."""
        try:
            self.command("kill")
        finally:
            self.close()

    def avd_name(self) -> str:
        """Return the AVD name attached to this console port."""
        output = self.command("avd name")
        for line in output.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
        return ""

    def save_snapshot(self, name: str) -> str:
        """Save a named snapshot (``snapshot save <name>``)."""
        return self.command(f"snapshot save {_validate_name(name, 'snapshot')}")

    def load_snapshot(self, name: str) -> str:
        """Load a named snapshot (``snapshot load <name>``)."""
        return self.command(f"snapshot load {_validate_name(name, 'snapshot')}")

    def delete_snapshot(self, name: str) -> str:
        """Delete a named snapshot (``snapshot delete <name>``)."""
        return self.command(f"snapshot delete {_validate_name(name, 'snapshot')}")

    def list_snapshots(self) -> list[str]:
        """List snapshot names reported by the console."""
        output = self.command("snapshot list")
        names: list[str] = []
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped or stripped.lower().startswith("snapshot"):
                continue
            names.append(stripped)
        return names

    def help(self) -> str:
        """Return the console command help text."""
        return self.command("help")

    # -- socket helpers -------------------------------------------------
    def _require_connected(self) -> None:
        if self._sock is None:
            raise EmulatorConsoleError(
                "emulator console is not connected",
                hint="Call connect(port) first (or use the object as a context manager).",
            )

    def _send_line(self, line: str) -> None:
        sock = self._sock
        if sock is None:
            raise EmulatorConsoleError("emulator console is not connected")
        try:
            sock.sendall((line + "\n").encode("utf-8", errors="replace"))
        except OSError as exc:
            raise EmulatorConsoleError(
                f"could not write to emulator console: {redact(str(exc))}"
            ) from exc

    def _handshake(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        banner_deadline = min(deadline, time.monotonic() + max(0.05, timeout / 4))
        saw_banner_line = False
        got_ok = False
        while True:
            try:
                line = self._read_line(banner_deadline)
            except _ConsoleReadTimeout:
                break
            except _ConsoleEOF as exc:
                raise EmulatorConsoleError(
                    "emulator console closed the connection during the auth handshake",
                    hint="The emulator may have crashed during boot.",
                ) from exc
            saw_banner_line = True
            stripped = line.strip()
            if stripped == "OK":
                got_ok = True
                break
            if stripped.startswith("KO"):
                raise EmulatorConsoleError(
                    f"emulator console rejected the connection: {redact(stripped)}"
                )
        if self._token:
            # No token means authentication is disabled or nothing was
            # discoverable: never send an ``auth`` line in that case.
            self._send_line(f"auth {self._token}")
            auth_deadline = time.monotonic() + timeout
            while True:
                try:
                    line = self._read_line(auth_deadline)
                except _ConsoleEOF as exc:
                    raise EmulatorConsoleAuthError(
                        "emulator console closed the connection during authentication"
                    ) from exc
                stripped = line.strip()
                if stripped == "OK":
                    got_ok = True
                    break
                if stripped.startswith("KO"):
                    raise EmulatorConsoleAuthError(
                        f"emulator console authentication failed: {redact(stripped)}",
                        hint="Delete the stale ~/.emulator_console_auth_token and restart the AVD.",
                    )
        if not got_ok and not saw_banner_line:
            raise EmulatorConsoleError(
                "emulator console did not send a banner",
                hint="Verify the port belongs to the emulator console, not adb.",
            )

    def _read_response(self, *, command: str) -> str:
        deadline = time.monotonic() + self.command_timeout_s
        lines: list[str] = []
        while True:
            try:
                line = self._read_line(deadline)
            except _ConsoleEOF:
                # ``kill`` legitimately tears the connection down; any other
                # command losing the socket before OK is an error.
                if command.strip().lower() == "kill":
                    return "\n".join(lines).strip()
                raise EmulatorConsoleError(
                    f"emulator console closed the connection before answering {command!r}"
                ) from None
            stripped = line.strip()
            if stripped == "OK":
                return "\n".join(lines).strip()
            if stripped.startswith("KO"):
                message = stripped[2:].strip().lstrip(": ").strip()
                raise EmulatorConsoleError(
                    f"emulator console command {command!r} failed: {redact(message) or 'unknown error'}"
                )
            lines.append(line)

    def _read_line(self, deadline: float) -> str:
        sock = self._sock
        if sock is None:
            raise _ConsoleEOF("emulator console is not connected")
        while b"\n" not in self._buffer:
            if len(self._buffer) > _MAX_RESPONSE_BYTES:
                raise EmulatorConsoleError("emulator console response exceeded the size limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ConsoleReadTimeout(
                    "timed out waiting for the emulator console to respond"
                )
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(4096)
            except TimeoutError as exc:
                raise _ConsoleReadTimeout(
                    "timed out waiting for the emulator console to respond"
                ) from exc
            except OSError as exc:
                raise _ConsoleEOF(f"emulator console connection failed: {redact(str(exc))}") from exc
            if not chunk:
                raise _ConsoleEOF("emulator console connection closed")
            self._buffer += chunk
        raw_line, _, self._buffer = self._buffer.partition(b"\n")
        return raw_line.decode("utf-8", errors="replace").rstrip("\r")


def _validate_name(name: str, kind: str) -> str:
    candidate = (name or "").strip()
    if not candidate:
        raise EmulatorConsoleError(f"{kind} name must not be empty")
    if any(character.isspace() for character in candidate):
        raise EmulatorConsoleError(f"{kind} name must not contain whitespace: {name!r}")
    return candidate


__all__ = [
    "DEFAULT_COMMAND_TIMEOUT_S",
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_CONSOLE_HOST",
    "EmulatorConsole",
    "EmulatorConsoleAuthError",
    "EmulatorConsoleError",
    "TOKEN_ENV_VARS",
    "TOKEN_FILENAME",
    "TOKEN_HOMES",
    "console_token_paths",
    "resolve_console_token",
]
