"""Per-instance local proxy bridge for AVD egress isolation.

Why a bridge?
-------------
The Android emulator's own ``-http-proxy`` only covers part of the guest's
network stack (see ``research/R4-networking.md``) and it cannot express
authenticated SOCKS5 upstreams or hot-swap credentials.  The bridge is a
loopback-only TCP proxy owned by AVDFleet:

* one **listener pair per instance** - an HTTP proxy (``CONNECT`` tunnels plus
  absolute-URI forwarding) and a SOCKS5 listener - bound to ``127.0.0.1`` on
  auto-allocated ports starting at ``Config.bridge_port_start``.  A caller that
  cannot share the process's port table (the detached worker) instead asks the
  kernel for a free port and publishes it (``ephemeral_ports``);
* exactly **one upstream per instance** - HTTP/HTTPS forward proxy with Basic
  auth, SOCKS5 proxy with username/password auth, or direct (tests/failover);
* upstream credentials live only in the in-memory :class:`Upstream` object and
  are injected into ``Proxy-Authorization`` / SOCKS5 frames per connection.
  :class:`~avdfleet.core.models.BridgeConfig` exposes only
  ``scheme://host:port`` and the bridge never logs credentials;
* ``swap`` replaces the upstream atomically without closing the listener, so a
  proxy rotation is invisible to the already-configured guest.  Existing
  tunnels keep their old upstream; new connections use the new one.

Public API
----------
:class:`BridgeManager` is the synchronous facade implementing
:class:`avdfleet.core.interfaces.ProxyBridge`.  It owns a background asyncio
event-loop thread so the synchronous runtime (N1) can call it directly::

    bridge = BridgeManager(config)
    with bridge:                                  # start/stop the manager
        cfg = bridge.ensure("avd-01", proxy)      # loopback auto-port listener
        bridge_url(cfg)                           # -> http://127.0.0.1:<port>
        bridge.socks_url("avd-01")                # -> socks5://127.0.0.1:<port>
        bridge.swap("avd-01", other_proxy)        # same port, new upstream
        bridge.stop("avd-01")                     # releasable at any time
    # ports are free again after close()

:class:`AsyncBridgeManager` is the same engine without the background thread;
use it inside an existing event loop (API server, tests).  All mutation is
guarded by one :class:`asyncio.Lock`, so concurrent ``ensure``/``swap``/``stop``
calls are safe.  ``ensure`` is idempotent: the same instance+proxy returns the
same :class:`BridgeConfig`; a different proxy for a live instance performs a
hot swap (never a rebind).

Integration wiring
------------------
**N1 (runtime).**  After resolving the instance's assignment (X1) and starting
the emulator's TCP path::

    cfg = bridge.ensure(instance, assignment_proxy)
    emulator_args += ["-http-proxy", f"http://{cfg.listen_host}:{cfg.listen_port}"]

Pass ``cfg.listen_host``/``listen_port`` (never the upstream) to the emulator;
the guest must not know upstream credentials.  On stop/restart call
``bridge.stop(instance)``; on a proxy reassignment call
``bridge.swap(instance, new_proxy)`` and keep the already-configured emulator
proxy argument.  A runtime ``PortAllocator`` can be passed to ``BridgeManager``
so bridge ports are reserved/released through the same allocator.

**X1 (pool/assign/health).**  ``ProxyBridge.list_active()`` returns the bridge
inventory for ``GET /api/v1/bridges``; ``BridgeConfig.upstream`` is safe to
serialize (no userinfo).  Health checks use
:class:`avdfleet.proxy.verify.verifier.GuestEgressVerifier`: ``verify_proxy``
for host-side reachability and ``verify_guest`` for an in-guest HTTP request
(``adb shell curl ...``) whose URL carries the guest token; the listener-side
token evidence is collected separately by the G1 harness.

_Test hooks._  ``ensure(instance, None)`` runs in direct mode, and
``local_auth_token=...`` (on ``ensure``) enables per-instance Basic/SOCKS
authentication for the loopback listeners.  Tokens are caller-owned, never
stored in :class:`BridgeConfig`, and returned again only by
:meth:`BridgeManager.local_proxy_credentials`.

Limitations
-----------
* **UDP is not proxied** (no SOCKS5 UDP ASSOCIATE, no DNS-over-UDP): QUIC/UDP
  and guest DNS queries bypass the bridge unless the emulator stack is
  configured to route them through the proxy.  HTTP/HTTPS upstreams resolve
  guest hostnames at the upstream proxy for CONNECT; SOCKS5 upstreams default
  to SOCKS5h semantics so guest hostnames never reach the host resolver.
* HTTP forwarding is one request per connection (``Connection: close``);
  chunked request bodies are rejected with ``501``.  CONNECT tunnels are
  full-duplex.
* Existing tunnels are not re-routed by ``swap``; they end when the guest or
  upstream closes them.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar
from urllib.parse import quote

from avdfleet.core.config import Config
from avdfleet.core.errors import ProxyError, ValidationError
from avdfleet.core.logging import get_logger, redact_text
from avdfleet.core.models import BridgeConfig, ProxyConfig
from avdfleet.core.ports import PortAllocator

from .http_proxy import HttpProxyServer
from .server import DEFAULT_BRIDGE_PORT_START, BridgeOptions
from .socks import Socks5ProxyServer
from .upstream import Upstream, parse_upstream, upstream_mode

_T = TypeVar("_T")

_LOGGER = get_logger("proxy.bridge")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class _UpstreamRef:
    """Mutable box shared with the listeners so ``swap`` needs no rebinding."""

    upstream: Upstream


@dataclass
class _InstanceState:
    instance: str
    ref: _UpstreamRef
    http: HttpProxyServer
    socks: Socks5ProxyServer | None
    local_auth_token: str | None
    created_at: float = field(default_factory=time.monotonic)

    def bridge_config(self, *, listen_host: str) -> BridgeConfig:
        return BridgeConfig(
            instance=self.instance,
            listen_host=listen_host,
            listen_port=self.http.port or 0,
            upstream=self.ref.upstream.display,
            mode=upstream_mode(self.ref.upstream),  # type: ignore[arg-type]
            active=True,
        )


def _validate_instance(instance: str) -> str:
    if not isinstance(instance, str) or not instance.strip():
        raise ValidationError("bridge instance name must be a non-empty string")
    if len(instance) > 128:
        raise ValidationError("bridge instance name must be at most 128 characters")
    if any(ord(char) < 0x20 for char in instance):
        raise ValidationError("bridge instance name must not contain control characters")
    return instance


def _validate_local_token(token: str | None) -> str | None:
    if token is None:
        return None
    if not isinstance(token, str) or not token or len(token) > 255:
        raise ValidationError("bridge local auth token must be a 1..255 character string")
    if any(ord(char) < 0x20 for char in token):
        raise ValidationError("bridge local auth token must not contain control characters")
    return token


def _validate_explicit_port(port: int | None, what: str) -> int | None:
    if port is None:
        return None
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValidationError(f"{what} must be 1..65535")
    return port


# ---------------------------------------------------------------------------
# Async engine
# ---------------------------------------------------------------------------


class AsyncBridgeManager:
    """Asyncio engine: one HTTP listener (+ optional SOCKS5) per instance."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        options: BridgeOptions | None = None,
        port_allocator: PortAllocator | None = None,
        ephemeral_ports: bool = False,
    ) -> None:
        """Create the engine.

        ``ephemeral_ports`` binds every new listener on port 0 and reports the
        kernel-assigned port instead of scanning ``port_start..port_end``.  The
        detached worker uses it because a parent process cannot reserve a port
        for a child it has not spawned yet: only the process that actually binds
        the socket can know the port is truly free across processes.
        """
        opts = options or BridgeOptions()
        if opts.port_start is None:
            if port_allocator is not None:
                start = port_allocator.bridge_start
            elif config is not None:
                start = config.bridge_port_start
            else:
                start = DEFAULT_BRIDGE_PORT_START
            opts = replace(opts, port_start=start)
        self._options = opts
        self._config = config
        self._port_allocator = port_allocator
        self._ephemeral_ports = ephemeral_ports
        self._instances: dict[str, _InstanceState] = {}
        self._used_ports: set[int] = set()
        self._lock = asyncio.Lock()
        self._started = False
        self._closed = False

    # -- properties ------------------------------------------------------
    @property
    def options(self) -> BridgeOptions:
        return self._options

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def started(self) -> bool:
        return self._started

    @property
    def listen_host(self) -> str:
        return self._options.listen_host

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> AsyncBridgeManager:
        async with self._lock:
            self._ensure_open()
            self._started = True
        return self

    async def close(self) -> None:
        """Stop every instance listener and release every port (idempotent)."""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            states = list(self._instances.values())
            self._instances.clear()
            for state in states:
                await self._stop_state(state)
            self._started = False

    async def __aenter__(self) -> AsyncBridgeManager:
        return await self.start()

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    # -- ProxyBridge API -------------------------------------------------
    async def ensure(
        self,
        instance: str,
        proxy: ProxyConfig | None = None,
        *,
        listen_port: int | None = None,
        socks_port: int | None = None,
        local_auth_token: str | None = None,
    ) -> BridgeConfig:
        """Create the per-instance listener pair, or reconcile an existing one.

        ``proxy=None`` selects direct mode.  A live instance whose upstream
        changed is hot-swapped in place; its listener port never changes.
        Explicit ``listen_port``/``socks_port``/``local_auth_token`` values are
        honoured only when the instance is first created: the bound listener
        socket and its token are sticky for the instance's bridge lifetime
        (call :meth:`stop` + :meth:`ensure` to rebind).
        """
        listen_port = _validate_explicit_port(listen_port, "bridge listen_port")
        socks_port = _validate_explicit_port(socks_port, "bridge socks_port")
        async with self._lock:
            self._ensure_open()
            self._started = True
            return await self._ensure_locked(
                instance,
                proxy,
                listen_port=listen_port,
                socks_port=socks_port,
                local_auth_token=local_auth_token,
            )

    async def swap(self, instance: str, proxy: ProxyConfig | None = None) -> BridgeConfig:
        """Point an existing instance at a new upstream without closing listeners."""
        async with self._lock:
            self._ensure_open()
            return await self._swap_locked(instance, proxy)

    async def stop(self, instance: str) -> None:
        """Stop one instance's listeners and release its ports (idempotent)."""
        async with self._lock:
            state = self._instances.pop(instance, None)
            if state is None:
                return
            await self._stop_state(state)
            _LOGGER.debug("bridge stopped instance=%s", instance)

    async def get(self, instance: str) -> BridgeConfig | None:
        async with self._lock:
            state = self._instances.get(instance)
            return state.bridge_config(listen_host=self._options.listen_host) if state else None

    async def list_active(self) -> list[BridgeConfig]:
        async with self._lock:
            ordered = sorted(self._instances.values(), key=lambda state: (state.created_at, state.instance))
            return [state.bridge_config(listen_host=self._options.listen_host) for state in ordered]

    async def stop_all(self) -> None:
        async with self._lock:
            states = list(self._instances.values())
            self._instances.clear()
            for state in states:
                await self._stop_state(state)

    # -- extras ----------------------------------------------------------
    async def socks_port(self, instance: str) -> int | None:
        async with self._lock:
            state = self._instances.get(instance)
            if state is None or state.socks is None:
                return None
            return state.socks.port

    async def local_proxy_credentials(self, instance: str) -> tuple[str, str] | None:
        """Return ``(username, token)`` for an instance's loopback listeners."""
        async with self._lock:
            state = self._instances.get(instance)
            if state is None or state.local_auth_token is None:
                return None
            return state.instance, state.local_auth_token

    async def active_ports(self) -> dict[str, tuple[int, int | None]]:
        async with self._lock:
            return {
                name: (state.http.port or 0, state.socks.port if state.socks else None)
                for name, state in self._instances.items()
            }

    # -- internals -------------------------------------------------------
    def _ensure_open(self) -> None:
        if self._closed:
            raise ProxyError("bridge manager is closed")

    async def _ensure_locked(
        self,
        instance: str,
        proxy: ProxyConfig | None,
        *,
        listen_port: int | None,
        socks_port: int | None,
        local_auth_token: str | None,
    ) -> BridgeConfig:
        instance = _validate_instance(instance)
        token = _validate_local_token(local_auth_token)
        existing = self._instances.get(instance)
        if existing is not None:
            if token is not None and token != existing.local_auth_token:
                _LOGGER.debug(
                    "bridge ensure ignored a new local_auth_token for live instance=%s "
                    "(stop/ensure to change it)",
                    instance,
                )
            return await self._swap_locked(instance, proxy)

        upstream = self._build_upstream(proxy)
        ref = _UpstreamRef(upstream=upstream)
        http = HttpProxyServer(
            instance,
            lambda: ref.upstream,
            self._options,
            local_auth_token=token,
        )
        socks: Socks5ProxyServer | None = None
        http_port = await self._bind_listener(http, listen_port, kind="http")
        try:
            if self._options.enable_socks or socks_port is not None:
                socks = Socks5ProxyServer(
                    instance,
                    lambda: ref.upstream,
                    self._options,
                    local_auth_token=token,
                )
                await self._bind_listener(socks, socks_port, kind="socks5")
        except BaseException:
            if socks is not None:
                await socks.stop(grace_s=0)
            await http.stop(grace_s=0)
            self._release_port(http_port)
            raise

        state = _InstanceState(
            instance=instance,
            ref=ref,
            http=http,
            socks=socks,
            local_auth_token=token,
        )
        self._instances[instance] = state
        _LOGGER.info(
            "bridge started instance=%s http_port=%s socks_port=%s upstream=%s",
            instance,
            http_port,
            socks.port if socks else None,
            upstream.display or "direct",
        )
        return state.bridge_config(listen_host=self._options.listen_host)

    async def _swap_locked(self, instance: str, proxy: ProxyConfig | None) -> BridgeConfig:
        instance = _validate_instance(instance)
        state = self._instances.get(instance)
        if state is None:
            return await self._ensure_locked(
                instance,
                proxy,
                listen_port=None,
                socks_port=None,
                local_auth_token=None,
            )
        upstream = self._build_upstream(proxy)
        if upstream.identity() == state.ref.upstream.identity():
            return state.bridge_config(listen_host=self._options.listen_host)
        previous = state.ref.upstream.display or "direct"
        state.ref.upstream = upstream
        _LOGGER.info(
            "bridge upstream swapped instance=%s upstream=%s previous=%s",
            instance,
            upstream.display or "direct",
            previous,
        )
        return state.bridge_config(listen_host=self._options.listen_host)

    async def _stop_state(self, state: _InstanceState) -> None:
        http_port = state.http.port
        socks_port = state.socks.port if state.socks is not None else None
        await state.http.stop()
        if state.socks is not None:
            await state.socks.stop()
        if http_port is not None:
            self._release_port(http_port)
        if socks_port is not None:
            self._release_port(socks_port)

    def _build_upstream(self, proxy: ProxyConfig | None) -> Upstream:
        try:
            return parse_upstream(
                proxy,
                remote_dns=self._options.remote_dns,
                tls_verify=self._options.tls_verify,
            )
        except ValueError as exc:
            raise ProxyError(
                f"invalid upstream proxy configuration: {redact_text(str(exc))}",
                hint="Use scheme://host:port without userinfo; credentials belong in ProxyConfig.",
            ) from exc

    # -- port allocation -------------------------------------------------
    async def _bind_listener(self, server: Any, preferred: int | None, *, kind: str) -> int:
        if preferred is not None:
            if preferred in self._used_ports:
                raise ProxyError(f"bridge {kind} port {preferred} is already in use by another instance")
            try:
                bound = await server.start(preferred)
            except OSError as exc:
                raise ProxyError(
                    f"could not bind bridge {kind} listener on "
                    f"{self._options.listen_host}:{preferred}: {exc.strerror or exc}"
                ) from exc
            self._reserve_port(bound)
            return bound
        if not self._ephemeral_ports:
            for candidate in range(self._options.effective_port_start, self._options.effective_port_end + 1):
                if candidate in self._used_ports:
                    continue
                try:
                    bound = await server.start(candidate)
                except OSError:
                    continue
                self._reserve_port(bound)
                return bound
        # Either the detached worker asked the kernel for a port (the only
        # allocation that cannot collide across processes) or the configured
        # range is exhausted.  Binding port 0 is atomic in the OS, so a second
        # process is never handed a port a live listener already owns.
        try:
            bound = await server.start(0)
        except OSError as exc:  # pragma: no cover - host socket exhaustion
            raise ProxyError(f"could not bind bridge {kind} listener: {exc}") from exc
        self._reserve_port(bound)
        return bound

    def _reserve_port(self, port: int) -> None:
        self._used_ports.add(port)
        if self._port_allocator is not None:
            self._port_allocator.reserve(port)

    def _release_port(self, port: int) -> None:
        self._used_ports.discard(port)
        if self._port_allocator is not None:
            self._port_allocator.release_reserved(port)


# ---------------------------------------------------------------------------
# Sync facade
# ---------------------------------------------------------------------------


class BridgeManager:
    """Synchronous :class:`ProxyBridge` implementation with a background loop.

    Lifecycle: :meth:`start` (lazy), :meth:`close` / context manager.  Instance
    listeners are created by :meth:`ensure` and torn down by :meth:`stop` or
    :meth:`close`.  The manager must not be used after :meth:`close`.
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        options: BridgeOptions | None = None,
        port_allocator: PortAllocator | None = None,
        call_timeout_s: float = 120.0,
        loop_thread_name: str = "avdfleet-bridge",
        detached: Any | None = None,
    ) -> None:
        self._core = AsyncBridgeManager(config, options=options, port_allocator=port_allocator)
        self._call_timeout_s = call_timeout_s
        self._loop_thread_name = loop_thread_name
        #: Optional out-of-process worker manager.  The daemon's own instances
        #: stay in-process; this only lets it *see and stop* workers spawned by a
        #: one-shot ``avdfleet instances start`` CLI process.
        self._detached = detached
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._loop_lock = threading.Lock()
        self._closed = False
        self._manager_id = uuid.uuid4().hex[:8]

    # -- lifecycle -------------------------------------------------------
    def start(self) -> BridgeManager:
        """Start the manager (idempotent).  ``ensure`` also starts it lazily."""
        self._submit(self._core.start())
        return self

    def close(self) -> None:
        """Stop all listeners, release all ports and stop the loop thread."""
        already_closed = self._closed
        self._closed = True
        loop, thread = self._loop, self._thread
        self._loop = None
        self._thread = None
        if thread is None or loop is None:
            return
        if not already_closed and thread.is_alive():
            try:
                future = asyncio.run_coroutine_threadsafe(self._core.close(), loop)
                future.result(self._call_timeout_s)
            except Exception as exc:  # pragma: no cover - defensive shutdown
                _LOGGER.warning("bridge shutdown failed: %s", redact_text(str(exc)))
            loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=max(self._call_timeout_s, 5.0))
        if thread.is_alive():  # pragma: no cover - defensive shutdown
            _LOGGER.warning("bridge event-loop thread did not stop within the timeout")

    def stop_all(self) -> None:
        """Stop every instance listener but keep the manager usable."""
        self._submit(self._core.stop_all())
        if self._detached is not None:
            try:
                self._detached.stop_all()
            except Exception as exc:  # noqa: BLE001 - detached cleanup is best effort
                _LOGGER.warning("detached bridge shutdown failed: %s", redact_text(str(exc)))

    def __enter__(self) -> BridgeManager:
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- ProxyBridge API (sync) -----------------------------------------
    def ensure(
        self,
        instance: str,
        proxy: ProxyConfig | None = None,
        *,
        listen_port: int | None = None,
        socks_port: int | None = None,
        local_auth_token: str | None = None,
    ) -> BridgeConfig:
        return self._submit(
            self._core.ensure(
                instance,
                proxy,
                listen_port=listen_port,
                socks_port=socks_port,
                local_auth_token=local_auth_token,
            )
        )

    def swap(self, instance: str, proxy: ProxyConfig | None = None) -> BridgeConfig:
        return self._submit(self._core.swap(instance, proxy))

    def stop(self, instance: str) -> None:
        self._submit(self._core.stop(instance))
        # ``instances stop`` must also reach a worker started by a different
        # CLI process; the in-process listener above remains the daemon default.
        self.stop_detached(instance)

    def get(self, instance: str) -> BridgeConfig | None:
        return self._submit(self._core.get(instance))

    def list_active(self) -> list[BridgeConfig]:
        return self._submit(self._core.list_active())

    # -- out-of-process (detached) workers -------------------------------
    def list_detached(self) -> list[BridgeConfig]:
        """List live detached workers published by ANY process.

        The daemon normally owns its instances in-process; this capability only
        surfaces bridges spawned by a one-shot ``avdfleet instances start`` CLI.
        """
        if self._detached is None:
            return []
        return list(self._detached.list_detached())

    def stop_detached(self, instance: str) -> None:
        """Stop an out-of-process worker for *instance* (no-op when absent)."""
        if self._detached is None:
            return
        try:
            self._detached.stop_detached(instance)
        except Exception as exc:  # noqa: BLE001 - never break in-process stop
            _LOGGER.warning(
                "detached bridge stop failed for %s: %s",
                instance,
                redact_text(str(exc)),
            )

    def cleanup_detached(
        self,
        is_running: Any | None = None,
        *,
        min_age_s: float = 0.0,
    ) -> list[str]:
        """Clean state files of dead/stopped detached workers; returns their names."""
        if self._detached is None:
            return []
        return list(self._detached.cleanup_detached(is_running=is_running, min_age_s=min_age_s))

    # -- extras ----------------------------------------------------------
    def http_port(self, instance: str) -> int | None:
        config = self.get(instance)
        return config.listen_port if config is not None else None

    def socks_port(self, instance: str) -> int | None:
        return self._submit(self._core.socks_port(instance))

    def bridge_url(self, instance: str) -> str | None:
        """The ``http://127.0.0.1:<port>`` URL to hand to the emulator."""
        config = self.get(instance)
        if config is None:
            return None
        return emulator_http_proxy_arg(config)

    def socks_url(self, instance: str) -> str | None:
        port = self.socks_port(instance)
        if port is None:
            return None
        return f"socks5://{self._core.listen_host}:{port}"

    def local_proxy_credentials(self, instance: str) -> tuple[str, str] | None:
        return self._submit(self._core.local_proxy_credentials(instance))

    def local_proxy_url(self, instance: str) -> str | None:
        """Listener URL including loopback Basic credentials, when configured.

        The returned URL embeds the caller-owned listener token: pass it only to
        the emulator/guest configuration for this instance and never log or
        serialize it (``BridgeConfig`` intentionally carries no token).
        """
        credentials = self.local_proxy_credentials(instance)
        port = self.http_port(instance)
        if port is None:
            return None
        if credentials is None:
            return f"http://{self._core.listen_host}:{port}"
        username, token = credentials
        return f"http://{quote(username, safe='')}:{quote(token, safe='')}@{self._core.listen_host}:{port}"

    def active_ports(self) -> dict[str, tuple[int, int | None]]:
        return self._submit(self._core.active_ports())

    @property
    def options(self) -> BridgeOptions:
        return self._core.options

    @property
    def closed(self) -> bool:
        return self._closed

    # -- async variants --------------------------------------------------
    async def astart(self) -> BridgeManager:
        await self._aexecute(self._core.start())
        return self

    async def aensure(
        self,
        instance: str,
        proxy: ProxyConfig | None = None,
        *,
        listen_port: int | None = None,
        socks_port: int | None = None,
        local_auth_token: str | None = None,
    ) -> BridgeConfig:
        return await self._aexecute(
            self._core.ensure(
                instance,
                proxy,
                listen_port=listen_port,
                socks_port=socks_port,
                local_auth_token=local_auth_token,
            )
        )

    async def aswap(self, instance: str, proxy: ProxyConfig | None = None) -> BridgeConfig:
        return await self._aexecute(self._core.swap(instance, proxy))

    async def astop(self, instance: str) -> None:
        await self._aexecute(self._core.stop(instance))

    async def aget(self, instance: str) -> BridgeConfig | None:
        return await self._aexecute(self._core.get(instance))

    async def alist_active(self) -> list[BridgeConfig]:
        return await self._aexecute(self._core.list_active())

    async def astop_all(self) -> None:
        await self._aexecute(self._core.stop_all())

    async def aclose(self) -> None:
        """Async teardown: stops all listeners, then the background loop."""
        if self._closed:
            return
        await self._aexecute(self._core.close())
        self._closed = True
        loop, thread = self._loop, self._thread
        if loop is not None and thread is not None and thread.is_alive():
            loop.call_soon_threadsafe(loop.stop)

    async def __aenter__(self) -> BridgeManager:
        return await self.astart()

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    # -- plumbing --------------------------------------------------------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_lock:
            loop = self._loop
            thread = self._thread
            if loop is not None and thread is not None and thread.is_alive():
                return loop
            if self._closed:
                raise ProxyError("bridge manager is closed")
            new_loop = asyncio.new_event_loop()
            ready = threading.Event()

            def _runner() -> None:
                asyncio.set_event_loop(new_loop)
                ready.set()
                try:
                    new_loop.run_forever()
                finally:
                    try:
                        new_loop.run_until_complete(new_loop.shutdown_asyncgens())
                    finally:
                        new_loop.close()

            new_thread = threading.Thread(
                target=_runner,
                name=f"{self._loop_thread_name}-{self._manager_id}",
                daemon=True,
            )
            self._loop = new_loop
            self._thread = new_thread
            new_thread.start()
            if not ready.wait(10.0):  # pragma: no cover - thread start failure
                raise ProxyError("bridge event loop failed to start")
            return new_loop

    def _submit(self, coro: Coroutine[Any, Any, _T]) -> _T:
        if self._closed:
            coro.close()
            raise ProxyError("bridge manager is closed")
        loop = self._ensure_loop()
        if threading.current_thread() is self._thread:
            coro.close()
            raise RuntimeError(
                "sync bridge API called from the bridge event-loop thread; use the a* methods"
            )
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()
            raise ProxyError("bridge manager is closed") from None
        try:
            return future.result(self._call_timeout_s)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise ProxyError("bridge operation timed out") from None

    async def _aexecute(self, coro: Coroutine[Any, Any, _T]) -> _T:
        if self._closed:
            coro.close()
            raise ProxyError("bridge manager is closed")
        if self._thread is not None and threading.current_thread() is self._thread:
            return await coro
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise


# ---------------------------------------------------------------------------
# Helpers used by the runtime (N1) and API (H1)
# ---------------------------------------------------------------------------


def emulator_http_proxy_arg(config: BridgeConfig) -> str:
    """Return the emulator ``-http-proxy`` value for a bridge config."""
    return f"http://{config.listen_host}:{config.listen_port}"


__all__ = [
    "AsyncBridgeManager",
    "BridgeManager",
    "DEFAULT_BRIDGE_PORT_START",
    "emulator_http_proxy_arg",
]
