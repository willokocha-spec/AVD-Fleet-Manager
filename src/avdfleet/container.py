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
import threading
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

    def __init__(self, config: Config, paths: Paths, *, detached_bridge: bool = False) -> None:
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
        from .proxy.bridge import BridgeManager, DetachedBridgeManager
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
        # The bridge must outlive the process that starts an emulator: the
        # emulator probes its proxy early in boot and silently falls back to
        # direct host egress when nothing answers on the loopback URL.
        #
        # Two owners need two lifetimes.  A one-shot CLI invocation exits seconds
        # after spawning the emulator, so it needs the detached per-instance
        # worker (``detached_bridge=True``).  The daemon and the test suite are
        # long-lived owners: an in-process bridge is correct for them, cheaper,
        # and observable.  The daemon additionally receives a detached manager so
        # it can see and stop workers spawned by a one-shot CLI invocation.
        self._detached_bridges: Any | None = None
        self._bridge_runtime_state: Any | None = None
        self.bridge_service: Any
        if detached_bridge:
            self.bridge_service = DetachedBridgeManager(config, paths)
        else:
            self._detached_bridges = DetachedBridgeManager(config, paths)
            self.bridge_service = BridgeManager(
                config,
                port_allocator=self.port_allocator,
                detached=self._detached_bridges,
            )
        self.verifier_service = GuestEgressVerifier(
            config,
            adb_path=[str(self.adb_path)] if self.adb_path else None,
            serial_resolver=self._serial_for,
            ssl_verify=False,
        )

        # -- root & instrumentation -------------------------------------
        # The library stores operator-supplied artifacts; nothing here bundles
        # or downloads Magisk/LSPosed (both GPL). aapt2 comes from the SDK's
        # build-tools so APK metadata is read without an extra dependency.
        from .instrumentation import (
            AcquisitionStore,
            InstrumentationRepository,
            ModuleLibrary,
            ensure_builtin_profiles,
            find_aapt2,
        )

        self.instrumentation_repository = InstrumentationRepository(self.store.database)
        self.instrumentation_modules_dir = (
            Path(paths.data_dir or paths.root) / "instrumentation" / "modules"
        )
        self.instrumentation_modules_dir.mkdir(parents=True, exist_ok=True)
        self.instrumentation_library = ModuleLibrary(
            self.instrumentation_modules_dir,
            aapt_path=find_aapt2(getattr(paths, "sdk_root", None)),
        )
        self.instrumentation_toolchain_dir = (
            Path(paths.data_dir or paths.root) / "instrumentation" / "toolchain"
        )
        self.instrumentation_acquisition = AcquisitionStore(
            self.instrumentation_toolchain_dir
        )
        try:
            ensure_builtin_profiles(self.instrumentation_repository)
        except Exception as exc:  # noqa: BLE001 - a broken seed must not block startup
            logger.warning("could not seed built-in instrumentation profiles: %s", exc)
        self._instrumentation_service: Any | None = None

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
        from .scripts.runners import DEFAULT_APPIUM_URL

        # appium_python/appium_js scripts must reach the server this fleet
        # actually runs.  Its port comes from the Appium port block, so the
        # historical hard-coded 4723/wd/hub would silently target a different
        # server whenever 4723 is taken.  AppiumManager.url reads the port back
        # from server.json, so this also resolves in a fresh CLI process; the
        # default is kept for a user-managed Appium server at 4723.
        self.script_service = ScriptService(
            store=self.store,
            event_bus=self.event_bus,
            config=config,
            paths=paths,
            instance_resolver=self._instance_or_none,
            adb_command=[str(self.adb_path)] if self.adb_path else None,
            appium_url_resolver=lambda target: self.appium_service.url or DEFAULT_APPIUM_URL,
        )
        self.appium_service = AppiumManager(
            config,
            paths,
            event_bus=self.event_bus,
            adb_command=[str(self.adb_path)] if self.adb_path else None,
            instance_resolver=self._instance_or_none,
            port_allocator=self.port_allocator,
            # The Appium server is a cross-process service: 'avdfleet appium
            # start' records pid/port in server.json and exits, so tearing the
            # container down must never stop a server another command (or the
            # daemon) is still using.  'avdfleet appium stop' is the only
            # implicit-free shutdown path.
            shutdown_on_close=False,
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

        # Detached proxy workers outlive the CLI that spawned them.  A state file
        # whose worker died with its instance, or whose instance is no longer
        # running, must not confuse a later start/stop (or keep a listener alive
        # for an emulator that is gone).
        self._cleanup_detached_bridges()

    # ------------------------------------------------------------------
    # lazily constructed runtime-dependent services
    # ------------------------------------------------------------------
    def _build_runtime(self) -> Any:
        from .platform.runtime import RuntimeManagerService

        runtime = RuntimeManagerService(
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
        # Boot-time automation has exactly one trigger point: the runtime's
        # boot-completed hook.  It therefore covers the HTTP wait-boot route, the
        # CLI's direct wait_boot call and a repeated wait on a booted instance.
        runtime.on_boot_completed(self._apply_auto_profile)
        return runtime

    def _apply_auto_profile(self, name: str) -> None:
        """Queue (and run) the instrumentation profile bound to *instance*.

        The hook runs inside ``wait_boot``; a broken profile or a slow deployment
        must never fail the boot wait, so every failure is logged and swallowed
        and the deployment itself runs on a daemon thread after it is queued.
        """
        try:
            service = self.instrumentation
            run = service.try_auto_profile(name)
        except Exception:  # noqa: BLE001 - automation must not break instance start
            logger.exception("auto instrumentation profile for %s could not be queued", name)
            return
        if run is None:
            return

        def execute() -> None:
            try:
                service.execute_run(run.id)
            except Exception:  # noqa: BLE001 - background run failures are logged
                logger.exception("auto instrumentation run %s crashed", run.id)

        threading.Thread(
            target=execute, name=f"avdfleet-auto-{run.id}", daemon=True
        ).start()

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
    # detached proxy bridge workers (out-of-process)
    # ------------------------------------------------------------------
    def _detached_cleaner(self) -> Any | None:
        for name in ("cleanup_detached", "cleanup_orphans"):
            candidate = getattr(self.bridge_service, name, None)
            if callable(candidate):
                return candidate
        return None

    def _bridge_instance_running(self, instance: str) -> bool | None:
        """Whether *instance*'s emulator is running, from persisted runtime state.

        ``None`` means "unknown"; cleanup then leaves a live worker alone.  This
        deliberately does not construct the full runtime service (it is lazy for
        a reason) and never guesses from a missing record.
        """
        try:
            from .platform.runtime.manager import RuntimeStateStore
            from .proxy.bridge.detached import pid_alive

            state = self._bridge_runtime_state
            if state is None:
                state = RuntimeStateStore(self.store, self.paths)
                self._bridge_runtime_state = state
            info = state.load(instance)
        except Exception:  # noqa: BLE001 - an unreadable state is unknown
            return None
        if info is None:
            return None
        if not bool(getattr(getattr(info, "state", None), "active", False)):
            return False
        pid = getattr(info, "pid", None)
        if pid is None:
            return None
        try:
            return pid_alive(int(pid))
        except (TypeError, ValueError):
            return None

    def _cleanup_detached_bridges(self, *, min_age_s: float | None = None) -> list[str] | None:
        """Clean detached workers whose pid died or whose instance stopped.

        Returns the cleaned instance names, or ``None`` when no detached-worker
        capability is wired or cleanup could not run.
        """
        cleaner = self._detached_cleaner()
        if cleaner is None:
            return None
        if min_age_s is None:
            from .proxy.bridge.detached import DEFAULT_ORPHAN_MIN_AGE_S

            min_age_s = DEFAULT_ORPHAN_MIN_AGE_S
        try:
            cleaned = cleaner(self._bridge_instance_running, min_age_s=min_age_s)
        except Exception as exc:  # noqa: BLE001 - cleanup must never block startup
            logger.warning("detached bridge cleanup failed: %s", exc)
            return None
        names = [str(name) for name in cleaned or []]
        if names:
            logger.info(
                "cleaned %d orphaned detached bridge state file(s): %s",
                len(names),
                ", ".join(names),
            )
        return names

    # ------------------------------------------------------------------
    # ServiceContainer API
    # ------------------------------------------------------------------
    def doctor(self) -> Any:
        return _BridgeAwareDoctor(self.platform.doctor, self)

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

    @property
    def instrumentation(self) -> Any:
        """The Root & Instrumentation service (built lazily: it needs the runtime)."""
        if self._instrumentation_service is None:
            from .instrumentation import DeviceProber, InstrumentationService, TargetDevice

            runtime = self.runtime()
            images = self.images_service

            def resolve(name: str) -> TargetDevice:
                info = runtime.status(name)
                serial = str(getattr(info, "adb_serial", "") or "")
                ports = getattr(info, "ports", None)
                if not serial and ports is not None:
                    serial = f"emulator-{ports.console}"
                values = getattr(info, "config", {}) or {}
                image_id = str(getattr(info, "image_id", "") or "")
                image_tag = ""
                if image_id:
                    try:
                        image_tag = str(getattr(images.get_image(image_id), "tag", "") or "")
                    except Exception:  # noqa: BLE001 - a missing catalogue row is fine
                        image_tag = ""
                return TargetDevice(
                    instance=name,
                    serial=serial,
                    running=bool(getattr(getattr(info, "state", None), "active", False)),
                    boot_completed=bool(getattr(info, "boot_completed", False)),
                    image_id=image_id,
                    image_tag=image_tag,
                    api_level=0,
                    abi=str(values.get("abi.type", "") or ""),
                )

            self._instrumentation_service = InstrumentationService(
                self.instrumentation_repository,
                self.instrumentation_library,
                DeviceProber(str(self.adb_path) if self.adb_path else None),
                resolve,
                magisk_installer=_magisk_installer_from_environment(self),
                acquisition=self.instrumentation_acquisition,
                events=self.event_bus,
                settings=self.store.settings,
            )
        return self._instrumentation_service

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


class _BridgeAwareDoctor:
    """Doctor facade that also reports detached proxy-bridge housekeeping.

    The platform doctor owns its fixed check list; detached workers are a
    cross-process concern of this container, so their cleanup is appended here
    (only when a detached-worker manager is actually wired).
    """

    def __init__(self, inner: Any, container: Container) -> None:
        self._inner = inner
        self._container = container

    def run(self, *, include_slow: bool = False) -> Any:
        cleaned = self._container._cleanup_detached_bridges()
        report = self._inner.run(include_slow=include_slow)
        return _with_bridge_check(report, cleaned)

    def fix(self, check_id: str, **kwargs: Any) -> Any:
        return self._inner.fix(check_id, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _doctor_summary(checks: list[Any]) -> str:
    from .core.models import CheckStatus

    counts = {status: 0 for status in CheckStatus}
    for check in checks:
        status = getattr(check, "status", None)
        if status in counts:
            counts[status] += 1
    ok = counts[CheckStatus.OK]
    warn = counts[CheckStatus.WARN]
    fail = counts[CheckStatus.FAIL]
    skip = counts[CheckStatus.SKIP]
    parts = f"{ok} ok, {warn} warning(s), {fail} failed, {skip} skipped"
    if fail:
        return f"{fail} critical issue(s) found: {parts}"
    if warn:
        return f"All critical checks passed with {warn} warning(s): {parts}"
    return f"All checks passed: {parts}"


def _with_bridge_check(report: Any, cleaned: list[str] | None) -> Any:
    if cleaned is None:
        return report
    from .core.models import CheckStatus, DoctorCheck

    if cleaned:
        detail = (
            f"cleaned {len(cleaned)} orphaned detached bridge state file(s): "
            + ", ".join(sorted(cleaned))
        )
    else:
        detail = "no orphaned detached bridge state files"
    check = DoctorCheck(
        id="bridge_workers",
        title="Detached proxy bridge workers",
        status=CheckStatus.OK,
        detail=detail,
    )
    checks = [*list(getattr(report, "checks", []) or []), check]
    try:
        return report.model_copy(update={"checks": checks, "summary": _doctor_summary(checks)})
    except AttributeError:  # pragma: no cover - duck-typed report
        return report


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
    *,
    detached_bridge: bool = False,
) -> Container:
    """Build the production container (used by ``create_app`` and the CLI).

    ``detached_bridge`` selects a per-instance detached proxy-bridge worker.
    One-shot CLI invocations pass ``True`` because the emulator outlives them;
    the daemon keeps the in-process bridge (its default) for observation.
    """
    resolved_config = config or Config.load()
    resolved_paths = paths or resolved_config.resolved_paths()
    return Container(resolved_config, resolved_paths, detached_bridge=detached_bridge)


def _magisk_installer_from_environment(container: Container) -> Any | None:
    """Build an external Magisk installer from operator-supplied settings.

    Magisk and rootAVD are GPL-licensed, so neither is bundled or downloaded.
    When the operator points ``AVDFLEET_MAGISK_INSTALLER`` at their own toolchain
    (``{instance}`` and ``{serial}`` are substituted) we run it; otherwise the
    service raises a configuration error rather than pretending to install.

    The command runs with the operator's privileges and can modify the image, so
    it is only ever executed by an explicitly configured, explicitly requested
    profile step.
    """
    import subprocess

    from .core.childenv import child_environment
    from .instrumentation import ComponentState, StepAction, StepResult

    command = (os.environ.get("AVDFLEET_MAGISK_INSTALLER") or "").strip()
    if not command:
        return None

    def installer(instance: str) -> StepResult:
        info = container.runtime().status(instance)
        serial = str(getattr(info, "adb_serial", "") or "")
        # Built through the child-env sanitiser: the operator's installer must
        # never inherit the daemon bearer token or the vault passphrase.
        extra: dict[str, str] = {}
        if serial:
            extra["ANDROID_SERIAL"] = serial
        sdk_root = getattr(container.paths, "sdk_root", None)
        if sdk_root:
            extra["ANDROID_HOME"] = str(sdk_root)
            extra["ANDROID_SDK_ROOT"] = str(sdk_root)
        # If the operator acquired the pinned Magisk release through this app, hand
        # its verified path to the installer so the toolchain stays self-consistent.
        if "AVDFLEET_MAGISK_APK" not in extra:
            try:
                from .instrumentation import find_artifact

                status = container.instrumentation_acquisition.status(find_artifact("magisk"))
                if status.present and status.verified:
                    extra["AVDFLEET_MAGISK_APK"] = status.path
            except Exception:  # noqa: BLE001 - acquisition is optional
                pass
        environment = child_environment(extra=extra)
        argv = command.replace("{instance}", instance).replace("{serial}", serial)
        try:
            completed = subprocess.run(  # noqa: S602 - operator-supplied toolchain
                argv,
                shell=True,
                capture_output=True,
                text=True,
                timeout=1800,
                env=environment,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return StepResult(
                step_id="install-magisk",
                action=StepAction.INSTALL_MAGISK,
                ok=False,
                message=f"the configured Magisk installer could not run: {exc}",
            )
        output = (completed.stderr or completed.stdout or "").strip()
        ok = completed.returncode == 0
        return StepResult(
            step_id="install-magisk",
            action=StepAction.INSTALL_MAGISK,
            ok=ok,
            state=ComponentState.ENABLED if ok else ComponentState.ABSENT,
            message=(
                "external Magisk installer completed; reboot the AVD and run verify"
                if ok
                else output[-400:] or f"installer exited {completed.returncode}"
            ),
            data={"returncode": completed.returncode},
        )

    return installer


__all__ = ["Container", "build_container"]
