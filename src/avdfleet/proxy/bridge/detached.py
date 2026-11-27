"""Out-of-process proxy bridge: one detached worker per instance.

The in-process :class:`~avdfleet.proxy.bridge.manager.BridgeManager` is the right
engine for a long-lived owner (the API daemon, tests).  The ``avdfleet instances
start`` CLI, however, exits seconds after spawning the emulator, and the emulator
probes its proxy early in boot and **silently falls back to direct host egress**
when nothing is listening ("Proxy will be ignored !").  The guest's traffic would
then bypass its assigned proxy entirely -- a G1 isolation failure rather than a
mere outage.

:class:`DetachedBridgeManager` keeps the bridge alive for exactly as long as the
instance: it spawns :mod:`avdfleet.proxy.bridge.worker` as a detached process,
publishes the bound ports in ``<data_dir>/bridge/<instance>.json`` and stops the
worker on :meth:`stop`.  Any later CLI/daemon invocation sees the same bridge
through that state file, so ``avdfleet proxies bridges`` is truthful across
processes and ``instances stop`` really tears the listener down.

Design notes
------------
* **Secrets.** The worker resolves the upstream password from the vault itself;
  argv carries only the proxy *id*.  The loopback listener token is generated
  inside the worker and published (0600) in the state file, so it never appears
  in ``ps`` output or in the emulator log.  ``local_proxy_url`` is the only
  accessor that exposes it.
* **Idempotence.** ``ensure`` on an instance whose worker is already alive and
  pointing at the same proxy returns the existing :class:`BridgeConfig`.
  A different proxy id restarts the worker (a rebind is the cross-process
  equivalent of the in-process hot swap).
* **Failure.** If the worker cannot publish its state within ``ready_timeout_s``
  the spawn fails loudly with a :class:`ProxyError` instead of leaving an
  instance pointed at a dead port.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from avdfleet.core.childenv import child_environment
from avdfleet.core.config import Config
from avdfleet.core.errors import ProxyError, ValidationError
from avdfleet.core.logging import get_logger
from avdfleet.core.models import BridgeConfig, ProxyConfig
from avdfleet.core.paths import Paths

from .server import BridgeOptions
from .worker import (
    bridge_state_dir,
    read_state,
    safe_instance_stem,
    state_path,
    stop_path,
    write_state,
)

_LOGGER = get_logger("proxy.bridge.detached")

DEFAULT_READY_TIMEOUT_S = 45.0
_STOP_GRACE_S = 6.0


def _pid_alive(pid: int) -> bool:
    """Return True when *pid* is a live process this user can see."""
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised on Windows hosts
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:  # pragma: no cover - POSIX hosts
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class DetachedBridgeManager:
    """``ProxyBridge`` facade over one detached worker process per instance."""

    def __init__(
        self,
        config: Config | None = None,
        paths: Paths | None = None,
        *,
        options: BridgeOptions | None = None,
        python: str | None = None,
        ready_timeout_s: float = DEFAULT_READY_TIMEOUT_S,
        popen_factory: Any | None = None,
    ) -> None:
        self._config = config or Config()
        self._paths = paths or self._config.resolved_paths()
        self._options = options or BridgeOptions(port_start=self._config.bridge_port_start)
        self._python = python or sys.executable
        self._ready_timeout_s = float(ready_timeout_s)
        self._popen = popen_factory or subprocess.Popen
        self._spawned: dict[str, Any] = {}

    # -- reads -----------------------------------------------------------
    @property
    def state_dir(self) -> Path:
        return bridge_state_dir(self._paths)

    def _state_file(self, instance: str) -> Path:
        return state_path(self._paths, instance)

    def _read(self, instance: str) -> dict[str, Any] | None:
        data = read_state(self._state_file(instance))
        if data is None:
            return None
        try:
            pid = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            return None
        if not _pid_alive(pid):
            # Self-heal: a crashed/terminated worker must not look active.
            try:
                self._state_file(instance).unlink()
            except OSError:
                pass
            return None
        return data

    def _bridge_config(self, data: dict[str, Any]) -> BridgeConfig:
        return BridgeConfig(
            instance=str(data.get("instance") or ""),
            listen_host=str(data.get("listen_host") or "127.0.0.1"),
            listen_port=int(data.get("listen_port") or 0),
            upstream=str(data.get("upstream") or ""),
            mode=str(data.get("mode") or "http"),  # type: ignore[arg-type]
            active=True,
        )

    def get(self, instance: str) -> BridgeConfig | None:
        data = self._read(instance)
        return self._bridge_config(data) if data is not None else None

    def list_active(self) -> list[BridgeConfig]:
        directory = self.state_dir
        if not directory.is_dir():
            return []
        bridges: list[BridgeConfig] = []
        for path in sorted(directory.glob("*.json")):
            if path.name.endswith(".json.stop"):
                continue
            data = read_state(path)
            if data is None:
                continue
            try:
                pid = int(data.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            if not _pid_alive(pid):
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            bridges.append(self._bridge_config(data))
        bridges.sort(key=lambda item: item.instance)
        return bridges

    def http_port(self, instance: str) -> int | None:
        config = self.get(instance)
        return config.listen_port if config is not None else None

    def socks_port(self, instance: str) -> int | None:
        data = self._read(instance)
        if data is None or data.get("socks_port") is None:
            return None
        return int(data["socks_port"])

    def bridge_url(self, instance: str) -> str | None:
        config = self.get(instance)
        if config is None:
            return None
        return f"http://{config.listen_host}:{config.listen_port}"

    def socks_url(self, instance: str) -> str | None:
        port = self.socks_port(instance)
        if port is None:
            return None
        data = self._read(instance)
        host = str((data or {}).get("listen_host") or "127.0.0.1")
        return f"socks5://{host}:{port}"

    def local_proxy_credentials(self, instance: str) -> tuple[str, str] | None:
        data = self._read(instance)
        if data is None:
            return None
        token = data.get("token")
        if not isinstance(token, str) or not token:
            return None
        return instance, token

    def local_proxy_url(self, instance: str) -> str | None:
        """Listener URL including the loopback Basic credentials.

        The token is a loopback-only defence-in-depth secret: pass it to the
        emulator environment for this instance and never log or serialize it.
        """
        data = self._read(instance)
        if data is None:
            return None
        from urllib.parse import quote

        host = str(data.get("listen_host") or "127.0.0.1")
        port = int(data.get("listen_port") or 0)
        token = data.get("token")
        if not isinstance(token, str) or not token:
            return f"http://{host}:{port}"
        return f"http://{quote(instance, safe='')}:{quote(token, safe='')}@{host}:{port}"

    def active_ports(self) -> dict[str, tuple[int, int | None]]:
        return {
            config.instance: (config.listen_port, self.socks_port(config.instance))
            for config in self.list_active()
        }

    # -- writes ----------------------------------------------------------
    def ensure(
        self,
        instance: str,
        proxy: ProxyConfig | None = None,
        *,
        listen_port: int | None = None,
        socks_port: int | None = None,
        local_auth_token: str | None = None,
    ) -> BridgeConfig:
        instance = _validate_instance(instance)
        proxy_id = getattr(proxy, "id", None)
        existing = self._read(instance)
        if existing is not None:
            if str(existing.get("proxy_id") or "") == str(proxy_id or ""):
                if local_auth_token and local_auth_token != existing.get("token"):
                    _LOGGER.debug(
                        "detached bridge keeps its own listener token for instance=%s",
                        instance,
                    )
                return self._bridge_config(existing)
            # A different upstream for a live bridge: restart the worker on a
            # fresh port (cross-process equivalent of the in-process hot swap).
            _LOGGER.info("detached bridge restarting instance=%s for a new upstream", instance)
            self.stop(instance)
        elif local_auth_token is not None:
            _LOGGER.debug(
                "detached bridge generates its own listener token for instance=%s "
                "(caller tokens never leave this process)",
                instance,
            )
        if proxy is not None and not getattr(proxy, "enabled", True):
            raise ValidationError(f"proxy {proxy_id!r} is disabled")

        state_file = self._state_file(instance)
        sentinel = stop_path(self._paths, instance)
        for path in (state_file, sentinel):
            try:
                path.unlink()
            except OSError:
                pass
        state_file.parent.mkdir(parents=True, exist_ok=True)

        argv = [
            self._python,
            "-m",
            "avdfleet.proxy.bridge.worker",
            "--instance",
            instance,
            "--state-file",
            str(state_file),
            "--stop-file",
            str(sentinel),
        ]
        if proxy is None:
            argv.append("--direct")
        else:
            argv.extend(["--proxy-id", str(proxy_id)])
        if self._options.enable_socks or socks_port is not None:
            argv.append("--socks")

        env = child_environment()
        env["PYTHONUNBUFFERED"] = "1"
        kwargs: dict[str, Any] = {
            "args": argv,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "env": env,
            "close_fds": True,
            "cwd": str(Path(str(self._paths.data_dir))),
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:  # pragma: no cover - POSIX hosts
            kwargs["start_new_session"] = True

        try:
            process = self._popen(**kwargs)
        except OSError as exc:
            raise ProxyError(
                f"could not start the detached proxy bridge for {instance!r}: {exc}",
                hint="Check that the Python interpreter and the bridge module are available.",
            ) from exc
        self._spawned[instance] = process

        deadline = time.monotonic() + self._ready_timeout_s
        while time.monotonic() < deadline:
            data = read_state(state_file)
            if data is not None:
                try:
                    pid = int(data.get("pid") or 0)
                except (TypeError, ValueError):
                    pid = 0
                if pid and _pid_alive(pid):
                    _LOGGER.info(
                        "detached bridge ready instance=%s pid=%s port=%s upstream=%s",
                        instance,
                        pid,
                        data.get("listen_port"),
                        data.get("upstream") or "direct",
                    )
                    return self._bridge_config(data)
            code = process.poll() if hasattr(process, "poll") else None
            if code is not None:
                break
            time.sleep(0.05)
        self._terminate(instance)
        raise ProxyError(
            f"the detached proxy bridge for {instance!r} did not become ready in time",
            hint=(
                "Check the bridge log in the data directory "
                f"(bridge-{safe_instance_stem(instance)}.log)."
            ),
        )

    def swap(self, instance: str, proxy: ProxyConfig | None = None) -> BridgeConfig:
        return self.ensure(instance, proxy)

    def stop(self, instance: str) -> None:
        instance = _validate_instance(instance)
        state_file = self._state_file(instance)
        data = read_state(state_file)
        sentinel = stop_path(self._paths, instance)
        if data is None and instance not in self._spawned:
            try:
                sentinel.unlink()
            except OSError:
                pass
            return
        try:
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            write_state(sentinel, {"stop": instance, "requested_at": time.time()})
            deadline = time.monotonic() + _STOP_GRACE_S
            while time.monotonic() < deadline:
                if not state_file.exists():
                    break
                time.sleep(0.05)
        finally:
            pass
        self._terminate(instance)
        for path in (state_file, sentinel):
            try:
                path.unlink()
            except OSError:
                pass
        _LOGGER.debug("detached bridge stopped instance=%s", instance)

    def stop_all(self) -> None:
        for config in self.list_active():
            self.stop(config.instance)

    def close(self) -> None:
        """Detach without stopping anything.

        The whole point of this manager is that the bridge outlives the process
        that asked for it; ``stop``/``stop_all`` are the shutdown paths.
        """
        self._spawned.clear()

    # -- internals -------------------------------------------------------
    def _terminate(self, instance: str) -> None:
        data = read_state(self._state_file(instance))
        process = self._spawned.pop(instance, None)
        pid = 0
        if data is not None:
            try:
                pid = int(data.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
        if callable(getattr(process, "poll", None)) and process.poll() is None:
            try:
                process.kill()
            except Exception:  # pragma: no cover - already gone
                pass
        if pid and _pid_alive(pid):
            if os.name == "nt":  # pragma: no cover - Windows hosts
                taskkill = shutil.which("taskkill")
                if taskkill:
                    try:
                        subprocess.run(  # noqa: S603 - fixed argv, no shell
                            [taskkill, "/PID", str(pid), "/T", "/F"],
                            capture_output=True,
                            check=False,
                            timeout=15,
                        )
                    except (OSError, subprocess.SubprocessError):
                        pass
            else:  # pragma: no cover - POSIX hosts
                try:
                    os.kill(pid, 15)
                except OSError:
                    pass


def _validate_instance(instance: str) -> str:
    if not isinstance(instance, str) or not instance.strip():
        raise ValidationError("bridge instance name must be a non-empty string")
    if len(instance) > 128:
        raise ValidationError("bridge instance name must be at most 128 characters")
    if any(ord(char) < 0x20 for char in instance):
        raise ValidationError("bridge instance name must not contain control characters")
    return instance


__all__ = ["DEFAULT_READY_TIMEOUT_S", "DetachedBridgeManager"]
