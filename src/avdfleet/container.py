"""Production service container.

This is the integration seam between the adapters (API, CLI, portable launcher)
and the subsystem implementations.  Construction is explicit and dependency
ordered; every service is created once and shared for the process lifetime.
Tests inject their own containers into ``create_app``/CLI commands instead of
building this graph.

Nothing in this module performs network access, installs software or starts an
emulator until a service method is called.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

from .core.childenv import ensure_process_adb_environment
from .core.config import APP_VERSION, Config
from .core.events import EventBus
from .core.paths import Paths
from .core.ports import PortAllocator

logger = logging.getLogger("avdfleet.container")


class Container:
    """Dependency-ordered service graph implementing core ServiceContainer."""

    def __init__(self, config: Config, paths: Paths) -> None:
        self.config = config
        self.paths = paths
        ensure_process_adb_environment(port_end=config.port_range_end)
        self.paths.ensure()
        self.event_bus = EventBus()
        self._closed = False

        # -- persistence -------------------------------------------------
        from .store.db import Store

        self.store = Store(paths.db_path)
        self.store.initialize()

        # -- host + tooling ---------------------------------------------
        from .platform import PlatformService
        from .platform.sdk import get_backend

        self.platform = PlatformService(config=config, paths=paths)
        self.host_info = self.platform.detect()
        self.sdk: Any | None
        try:
            self.sdk = get_backend(config, self.host_info)
            logger.debug("SDK backend selected: %s", getattr(self.sdk, "name", "unknown"))
        except Exception:  # pragma: no cover - missing SDK on a fresh host
            logger.warning("SDK backend unavailable; run 'avdfleet install' first", exc_info=True)
            self.sdk = None

        self.adb_path = self._resolve_tool("adb")
        self.emulator_path = self._resolve_tool("emulator")

        # -- images ------------------------------------------------------
        from .platform.images import ImageService

        self.images_service = ImageService(
            paths,
            config,
            sdk=self.sdk,
            host=self.host_info,
        )

        # -- proxy (needed by runtime for strict egress isolation) -------
        from .proxy import AssignmentService, ProxyPoolService, create_vault
        from .proxy.bridge import BridgeManager
        from .proxy.verify import GuestEgressVerifier

        self.vault = create_vault(paths, backend=getattr(config, "vault_backend", "auto"))
        if (
            type(self.vault).__name__ == "EncryptedFileVault"
            and not os.environ.get("AVDFLEET_VAULT_PASSPHRASE")
        ):
            logger.warning(
                "proxy secrets use the encrypted file vault without AVDFLEET_VAULT_PASSPHRASE; "
                "a generated key file beside the vault only obfuscates secrets, it does not "
                "protect them like a user passphrase or OS keyring. Set "
                "AVDFLEET_VAULT_PASSPHRASE or install a keyring backend."
            )
        self.proxy_service = ProxyPoolService(self.store, self.vault, config)
        self.assignment_service = AssignmentService(self.store, self.proxy_service)
        self.port_allocator = PortAllocator(
            start=config.port_range_start,
            end=config.port_range_end,
            appium_start=config.appium_port_start,
            bridge_start=config.bridge_port_start,
        )
        self.bridge_service = BridgeManager(config, port_allocator=self.port_allocator)
        self.verifier_service = GuestEgressVerifier(
            config,
            adb_path=[str(self.adb_path)] if self.adb_path else None,
            serial_resolver=self._serial_for,
            ssl_verify=False,
        )

        # -- AVDs / runtime ---------------------------------------------
        from .platform.avd import AvdManagerService

        self.avds_service = AvdManagerService(
            backend=self.sdk,
            store=self.store,
            config=config,
            paths=paths,
            images=self.images_service,
            events=self.event_bus,
        )
        # Runtime needs a usable adb/emulator.  A fresh host without an SDK must
        # still be able to run the daemon, doctor and installer, so construct it
        # lazily and let the API surface a service-unavailable error instead.
        self._runtime_service: Any | None = None
        self._screen_service: Any | None = None
        self._oobe_service: Any | None = None

        # -- automation --------------------------------------------------
        from .appium import AppiumManager
        from .scripts import ScriptService

        self.script_service = ScriptService(
            store=self.store,
            event_bus=self.event_bus,
            config=config,
            paths=paths,
            instance_resolver=self._instance_or_none,
            adb_command=[str(self.adb_path)] if self.adb_path else None,
        )
        self.appium_service = AppiumManager(
            config,
            paths,
            event_bus=self.event_bus,
            adb_command=[str(self.adb_path)] if self.adb_path else None,
            instance_resolver=self._instance_or_none,
            port_allocator=self.port_allocator,
        )

        # -- installer / fleet / portable -------------------------------
        from .fleetspec import FleetReconciler
        from .portable.bundle import PortableBundleBuilder

        self._installer: Any | None = None
        self._installer_provider_id: str | None = None
        self.fleet_service = FleetReconciler(
            self.avds_service,
            proxies=self.proxy_service,
            assignments=self.assignment_service,
            images=self.images_service,
            store=self.store,
            events=self.event_bus,
        )
        self.portable_service = PortableBundleBuilder(
            cache_dir=paths.cache_dir,
            version=APP_VERSION,
            host_os=self.host_info.os.value if self.host_info else None,
            host_arch=self.host_info.arch.value if self.host_info else None,
            python_version=self.host_info.python_version if self.host_info else "",
        )

    # ------------------------------------------------------------------
    # lazily constructed runtime-dependent services
    # ------------------------------------------------------------------
    def _build_runtime(self) -> Any:
        from .platform.runtime import RuntimeManagerService

        return RuntimeManagerService(
            self.avds_service,
            backend=self.sdk,
            config=self.config,
            paths=self.paths,
            store=self.store,
            events=self.event_bus,
            port_allocator=self.port_allocator,
            proxy_pool=self.proxy_service,
            bridge=self.bridge_service,
            direct_proxy_enabled=False,
        )

    def runtime(self) -> Any:
        if self._runtime_service is None:
            self._runtime_service = self._build_runtime()
        return self._runtime_service

    def screen(self) -> Any:
        if self._screen_service is None:
            from .screen.service import ScreenService

            self._screen_service = ScreenService(
                self.runtime(),
                self.paths,
                adb=None,
                avds=self.avds_service,
                config=self.config,
                adb_executable=str(self.adb_path or "adb"),
                event_bus=self.event_bus,
            )
        return self._screen_service

    def oobe(self) -> Any:
        if self._oobe_service is None:
            from .oobe.manager import OobeManager

            self._oobe_service = OobeManager(
                self.runtime(),
                self.avds_service,
                self.images_service,
                self.paths,
                adb=None,
                adb_executable=str(self.adb_path or "adb"),
                screen=self.screen(),
                config=self.config,
                event_bus=self.event_bus,
            )
        return self._oobe_service

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _resolve_tool(self, tool: str) -> Path | None:
        candidates: list[Path] = []
        try:
            if self.sdk is not None:
                try:
                    candidates.append(Path(self.sdk.executable(tool)))
                except Exception:
                    pass
            if self.paths.sdk_root:
                sdk_root = Path(self.paths.sdk_root)
                if tool == "adb":
                    candidates.append(sdk_root / "platform-tools" / "adb")
                elif tool == "emulator":
                    candidates.append(sdk_root / "emulator" / "emulator")
        except Exception:  # pragma: no cover - defensive
            pass
        for candidate in candidates:
            if candidate.exists():
                return candidate
        found = shutil.which(tool)
        return Path(found) if found else None

    def _instance_or_none(self, name: str) -> Any | None:
        try:
            return self.runtime().status(name)
        except Exception:
            return None

    def _serial_for(self, name: str) -> str:
        info = self._instance_or_none(name)
        if info is not None:
            if getattr(info, "adb_serial", None):
                return str(info.adb_serial)
            ports = getattr(info, "ports", None)
            if ports is not None and getattr(ports, "console", None):
                return f"emulator-{ports.console}"
        return f"emulator-{name}"

    # ------------------------------------------------------------------
    # ServiceContainer API
    # ------------------------------------------------------------------
    def doctor(self) -> Any:
        return self.platform.doctor

    def host(self) -> Any:
        return self.platform

    def installer(self) -> Any:
        if self._installer is None:
            from .platform.installer import create_engine
            from .platform.installer.providers import detect_provider_id, get_provider

            provider_id = detect_provider_id()
            self._installer_provider_id = provider_id
            provider = get_provider(provider_id, which=shutil.which)
            self._installer = create_engine(
                self.config,
                self.paths,
                provider=provider,
                sdk_backend=self.sdk,
                bus=self.event_bus,
                host=self.host_info,
            )
        return self._installer

    def images(self) -> Any:
        return self.images_service

    def avds(self) -> Any:
        return self.avds_service

    def proxies(self) -> Any:
        return self.proxy_service

    def assignments(self) -> Any:
        return self.assignment_service

    def bridge(self) -> Any:
        return self.bridge_service

    def verifier(self) -> Any:
        return self.verifier_service

    def scripts(self) -> Any:
        return self.script_service

    def appium(self) -> Any:
        return self.appium_service

    def fleet(self) -> Any:
        return self.fleet_service

    def portable(self) -> Any:
        return self.portable_service

    def repository(self) -> Any:
        return self.store

    def update_config(self, values: dict[str, Any]) -> Config:
        """Apply a validated config patch to the running daemon and persist it.

        Only the whitelisted PATCH /api/v1/config fields are accepted (the API
        validates before calling); persistence merges into ``config.toml`` so a
        restart keeps the change.
        """
        allowed = {
            "headless",
            "gpu_mode",
            "max_instances",
            "default_ram_mb",
            "theme",
            "log_level",
        }
        clean = {key: value for key, value in values.items() if key in allowed and value is not None}
        if not clean:
            return self.config
        updated = Config.model_validate({**self.config.model_dump(), **clean})
        for key, value in clean.items():
            setattr(self.config, key, value)
        _persist_config_values(self.paths.config_path, clean)
        return updated

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for name, closer in (
            ("appium", getattr(self.appium_service, "close", None)),
            ("bridge", getattr(self.bridge_service, "close", None)),
            ("screen", getattr(self._screen_service, "close", None)),
            ("runtime", getattr(self._runtime_service, "close", None)),
            ("store", getattr(self.store, "close", None)),
        ):
            if not callable(closer):
                continue
            try:
                closer()
            except Exception:  # pragma: no cover - shutdown must not raise
                logger.warning("failed to close %s", name, exc_info=True)
        try:
            self.event_bus.close()
        except Exception:  # pragma: no cover
            pass

    def __enter__(self) -> Container:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"Container(edition={self.config.edition!r}, db={str(self.store.db_path)!r}, "
            f"backend={getattr(self.sdk, 'name', None)!r})"
        )


def _persist_config_values(path: Path, values: dict[str, Any]) -> None:
    """Merge flat top-level values into config.toml without clobbering comments.

    The config loader accepts a flat TOML document, so only top-level keys are
    replaced.  Simple scalars/lists are sufficient for the PATCH whitelist.
    """
    import tomllib

    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = tomllib.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, tomllib.TOMLDecodeError):
            existing = {}
    existing.update(values)

    def render(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return "[" + ", ".join(render(item) for item in value) + "]"
        return repr(str(value))

    lines = [
        "# AVD Fleet configuration (written by the daemon; see docs/configuration.md)",
        "",
    ]
    for key in sorted(existing):
        lines.append(f"{key} = {render(existing[key])}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def build_container(
    config: Config | None = None,
    paths: Paths | None = None,
) -> Container:
    """Build the production container (used by ``create_app`` and the CLI)."""
    resolved_config = config or Config.load()
    resolved_paths = paths or resolved_config.resolved_paths()
    return Container(resolved_config, resolved_paths)


__all__ = ["Container", "build_container"]
