"""Detached per-instance bridge worker.

Why this process exists
-----------------------
The emulator probes its configured proxy **once, early in boot**, and then keeps
that path for the whole run: if nothing is listening on the loopback URL it
prints ``Could not connect to proxy ... Proxy will be ignored !`` and the guest
silently egresses on the *host* network instead of the assigned proxy.  A bridge
that lives inside the ``avdfleet instances start`` CLI process therefore dies
before the guest ever needs it (the CLI exits as soon as the emulator is
spawned), which breaks the product's central claim.

This module is the out-of-process owner of one instance's listener pair.  It is
spawned detached by :class:`avdfleet.proxy.bridge.detached.DetachedBridgeManager`
and outlives the CLI/daemon that asked for it, exactly like the emulator process
it serves.  It is stopped explicitly (``DetachedBridgeManager.stop``) when the
instance stops.

Contract with the manager
-------------------------
The worker owns its state file ``<data_dir>/bridge/<instance>.json``::

    {"pid": 1234, "instance": "avd-01", "proxy_id": "px_...",
     "listen_host": "127.0.0.1", "listen_port": 7800, "socks_port": null,
     "mode": "http", "upstream": "http://gate.example.com:8080",
     "token": "<loopback listener Basic-auth token>", "created_at": 1.0}

* written atomically **after** the listener is bound, so its presence means the
  bridge is usable;
* the loopback listener token is generated *here* and only ever travels through
  this file: never argv (``ps``) and never the emulator log;
* deleting the state file, writing ``<instance>.json.stop`` next to it, or
  SIGTERM all ask the worker to shut down; it then removes its own files.

Security notes: the upstream password is read from the vault in this process and
never appears in argv, the state file or the log.  ``BridgeConfig.upstream`` is
the credential-free ``scheme://host:port`` display form.  The listener token is a
local-only defence-in-depth secret, stored 0600 in the data directory.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from avdfleet.core.config import Config
from avdfleet.core.logging import get_logger, redact_text, setup_logging
from avdfleet.core.models import BridgeConfig, ProxyConfig
from avdfleet.core.paths import Paths

from .manager import AsyncBridgeManager

_LOGGER = get_logger("proxy.bridge.worker")

_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

#: How often the worker checks whether it was asked to stop.
_POLL_INTERVAL_S = 0.5


def bridge_state_dir(paths: Paths) -> Path:
    """Directory holding the per-instance bridge state files."""
    return Path(str(paths.data_dir)) / "bridge"


def safe_instance_stem(instance: str) -> str:
    """Stable, filesystem-safe stem for one instance name.

    The suffix **must not** come from the built-in ``hash()``: Python randomises
    string hashes per interpreter, so a second CLI/daemon process would compute a
    different file name, miss the live worker and report a stop that never
    happened (and a restart could spawn a second worker).  A truncated SHA-256
    digest is deterministic across processes and keeps the name short.
    """
    safe = _UNSAFE_FILENAME_RE.sub("_", instance).strip("._") or "instance"
    digest = hashlib.sha256(instance.encode("utf-8")).hexdigest()[:6]
    return f"{safe[:80]}-{digest}"


def state_path(paths: Paths, instance: str) -> Path:
    return bridge_state_dir(paths) / f"{safe_instance_stem(instance)}.json"


def stop_path(paths: Paths, instance: str) -> Path:
    return bridge_state_dir(paths) / f"{safe_instance_stem(instance)}.json.stop"


def state_files(paths: Paths) -> list[Path]:
    """Every published worker state file, whatever its naming scheme.

    Legacy workers used a ``hash()``-suffixed stem, which differs per process;
    callers that need to find one must match the file's ``"instance"`` field
    instead of trusting the computed path.
    """
    directory = bridge_state_dir(paths)
    try:
        return sorted(path for path in directory.glob("*.json") if path.is_file())
    except OSError:  # pragma: no cover - unreadable/deleted directory
        return []


def stop_file_for(state_file: Path) -> Path:
    """The sentinel a worker owning *state_file* polls (``<stem>.json.stop``)."""
    return state_file.with_name(f"{state_file.name}.stop")


def read_state(path: Path) -> dict[str, Any] | None:
    """Read one state file; ``None`` when absent or unparsable."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def write_state(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write a state file with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:  # best effort on Windows (POSIX semantics only)
        os.chmod(tmp, 0o600)
    except OSError:  # pragma: no cover - platform dependent
        pass
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Worker body
# ---------------------------------------------------------------------------


@dataclass
class _WorkerArgs:
    instance: str
    state_file: Path
    proxy_id: str | None
    direct: bool
    socks: bool
    stop_file: Path


def _parse_args(argv: list[str] | None = None) -> _WorkerArgs:
    parser = argparse.ArgumentParser(
        prog="python -m avdfleet.proxy.bridge.worker",
        description="Own one AVD instance's loopback proxy bridge (detached).",
    )
    parser.add_argument("--instance", required=True, help="Instance name served by this bridge.")
    parser.add_argument("--state-file", required=True, help="Where to publish the bound ports.")
    parser.add_argument("--stop-file", required=True, help="Sentinel file that asks the worker to exit.")
    parser.add_argument("--proxy-id", default=None, help="Proxy id resolved from the vault in this process.")
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Run in direct mode (no upstream; used by tests and by explicit direct mode).",
    )
    parser.add_argument("--socks", action="store_true", help="Also bind the SOCKS5 listener.")
    namespace = parser.parse_args(argv)
    if not namespace.direct and not namespace.proxy_id:
        parser.error("pass --proxy-id or --direct")
    return _WorkerArgs(
        instance=str(namespace.instance),
        state_file=Path(namespace.state_file),
        proxy_id=str(namespace.proxy_id) if namespace.proxy_id else None,
        direct=bool(namespace.direct),
        socks=bool(namespace.socks),
        stop_file=Path(namespace.stop_file),
    )


def _resolve_proxy(config: Config, paths: Paths, proxy_id: str) -> ProxyConfig:
    """Load one proxy *with* its vaulted password, inside this process."""
    from ...store.db import Store
    from .. import create_vault
    from ..pool import ProxyPoolService

    store = Store(paths.db_path)
    store.initialize()
    try:
        vault = create_vault(paths, backend=getattr(config, "vault_backend", "auto"))
        pool = ProxyPoolService(store, vault, config)
        return pool.get_with_credentials(proxy_id)
    finally:
        try:
            store.close()
        except Exception:  # pragma: no cover - close must not mask the real error
            pass


async def _run(args: _WorkerArgs, config: Config, paths: Paths) -> int:
    proxy: ProxyConfig | None = None
    if not args.direct:
        assert args.proxy_id is not None
        proxy = _resolve_proxy(config, paths, args.proxy_id)

    token = secrets.token_urlsafe(32)
    manager = AsyncBridgeManager(config)
    await manager.start()
    stopping = asyncio.Event()

    def _request_stop(*_signal: object) -> None:
        stopping.set()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _request_stop)
        except (OSError, ValueError, RuntimeError):  # pragma: no cover - platform dependent
            continue

    try:
        bridge: BridgeConfig = await manager.ensure(
            args.instance,
            proxy,
            local_auth_token=token,
        )
        socks_port = await manager.socks_port(args.instance)
        write_state(
            args.state_file,
            {
                "pid": os.getpid(),
                "instance": args.instance,
                "proxy_id": args.proxy_id,
                "listen_host": bridge.listen_host,
                "listen_port": bridge.listen_port,
                "socks_port": socks_port,
                "mode": bridge.mode,
                "upstream": bridge.upstream,
                "token": token,
                "created_at": time.time(),
            },
        )
        _LOGGER.info(
            "bridge worker ready instance=%s listen=%s:%s socks=%s mode=%s upstream=%s",
            args.instance,
            bridge.listen_host,
            bridge.listen_port,
            socks_port,
            bridge.mode,
            bridge.upstream or "direct",
        )
        while not stopping.is_set():
            if args.stop_file.exists() or not args.state_file.exists():
                _LOGGER.info("bridge worker stop requested instance=%s", args.instance)
                break
            await asyncio.sleep(_POLL_INTERVAL_S)
        return 0
    finally:
        await manager.close()
        for path in (args.state_file, args.stop_file):
            try:
                path.unlink()
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = Config.load()
    paths = config.resolved_paths()
    log_file = Path(paths.logs_dir) / f"bridge-{safe_instance_stem(args.instance)}.log"
    setup_logging(level=getattr(config, "log_level", "INFO"), console=False, log_file=log_file)
    try:
        return asyncio.run(_run(args, config, paths))
    except Exception as exc:  # noqa: BLE001 - the worker must always clean up
        _LOGGER.error("bridge worker failed instance=%s: %s", args.instance, redact_text(str(exc)))
        for path in (args.state_file, args.stop_file):
            try:
                path.unlink()
            except OSError:
                pass
        return 1


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
