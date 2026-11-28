"""Root & Instrumentation orchestration.

The service owns the business logic; the API and CLI only translate. It reuses
the rest of AVD Fleet rather than reimplementing it:

* instance state and adb serials come from the existing runtime manager through a
  narrow resolver callable injected by the container;
* the central module library and its files live under the data directory;
* deployments run one thread per target with a bounded pool, per-target results,
  a cancellation token and a persistent log.

Failure isolation is the important property: one incompatible or broken AVD must
never stop the others, so every target is executed inside its own try/except and
its outcome is recorded independently.
"""

from __future__ import annotations

import re
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import AvdFleetError, ConfigError, NotFoundError, ValidationError
from ..core.logging import get_logger, redact
from ..core.models import utcnow
from .acquire import AcquisitionStore, find_artifact
from .capabilities import LSPOSED_DB, DeviceProber
from .library import ModuleLibrary
from .models import (
    ComponentState,
    DeploymentRun,
    DeviceInstrumentation,
    InstrumentationProfile,
    LogEntry,
    ModuleKind,
    ModuleRecord,
    ProfileStep,
    RootImplementation,
    RunState,
    StepAction,
    StepResult,
    TargetResult,
)
from .repository import InstrumentationRepository

logger = get_logger("avdfleet.instrumentation.service")

#: Where a module archive is staged on the device before Magisk installs it.
STAGING_DIR = "/data/local/tmp"
#: Default parallelism for a deployment; the CLI flag can lower it.
DEFAULT_CONCURRENCY = 4


@dataclass(frozen=True)
class TargetDevice:
    """Everything the service needs to talk to one AVD."""

    instance: str
    serial: str
    running: bool
    boot_completed: bool
    image_id: str = ""
    image_tag: str = ""
    api_level: int = 0
    abi: str = ""


TargetResolver = Callable[[str], TargetDevice]


class InstrumentationService:
    """Capability probing, module library, profiles and deployment."""

    def __init__(
        self,
        repository: InstrumentationRepository,
        library: ModuleLibrary,
        prober: DeviceProber,
        resolve_target: TargetResolver,
        *,
        magisk_installer: Callable[[str], StepResult] | None = None,
        max_concurrency: int = DEFAULT_CONCURRENCY,
        acquisition: AcquisitionStore | None = None,
        events: Any | None = None,
        settings: Any | None = None,
    ) -> None:
        self._settings = settings
        self._events = events
        self._acquisition = acquisition
        self._repo = repository
        self._library = library
        self._prober = prober
        self._resolve = resolve_target
        self._magisk_installer = magisk_installer
        self._max_concurrency = max(1, int(max_concurrency))
        self._cancellations: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # -- module library --------------------------------------------------
    def list_modules(self, kind: ModuleKind | None = None) -> list[ModuleRecord]:
        return self._repo.list_modules(kind)

    def get_module(self, module_id: str) -> ModuleRecord:
        record = self._repo.get_module(module_id)
        if record is None:
            raise NotFoundError(f"module not found: {module_id}")
        return record

    def import_upload(self, filename: str, data: bytes) -> ModuleRecord:
        record = self._library.build_record(data, filename, source="upload")
        return self._repo.add_module(record)

    def import_path(self, path: str | Path) -> ModuleRecord:
        record = self._library.import_file(path)
        return self._repo.add_module(record)

    def import_url(self, url: str) -> ModuleRecord:
        record = self._library.import_url(url)
        return self._repo.add_module(record)

    def delete_module(self, module_id: str, *, delete_file: bool = True) -> None:
        record = self.get_module(module_id)
        self._repo.delete_module(module_id)
        if delete_file:
            self._library.delete_file(record)

    # -- lifecycle automation ---------------------------------------------
    #: Settings key holding ``{instance: profile_id}`` for start-time automation.
    AUTO_PROFILE_KEY = "instrumentation.auto_profiles"

    def _auto_map(self) -> dict[str, str]:
        if self._settings is None:
            return {}
        raw = self._settings.get(self.AUTO_PROFILE_KEY, {})
        if not isinstance(raw, dict):
            return {}
        return {
            str(instance): str(profile)
            for instance, profile in raw.items()
            if str(instance).strip() and str(profile).strip()
        }

    def get_auto_profile(self, instance: str) -> dict[str, Any]:
        """Return the profile configured to run when *instance* boots."""
        return {"instance": instance, "profile_id": self._auto_map().get(instance, "")}

    def set_auto_profile(self, instance: str, profile_id: str | None) -> dict[str, Any]:
        """Bind (or clear) the profile applied automatically when *instance* boots."""
        if self._settings is None:
            raise ConfigError("lifecycle automation is not configured")
        name = str(instance or "").strip()
        if not name:
            raise ValidationError("an instance name is required")
        mapping = self._auto_map()
        if profile_id:
            profile = self.get_profile(profile_id)
            mapping[name] = profile.id
        else:
            mapping.pop(name, None)
        self._settings.set(self.AUTO_PROFILE_KEY, mapping)
        return self.get_auto_profile(name)

    def try_auto_profile(self, instance: str) -> DeploymentRun | None:
        """Queue the profile bound to *instance*, once for the current guest boot.

        The boot identity (``ro.boot.serialno`` plus
        ``/proc/sys/kernel/random/boot_id``) is claimed atomically in the
        database before the run is created, so a repeated trigger for the same
        boot -- including two concurrent ones -- can only create one run.  The
        caller executes the returned run (the container does it on a worker
        thread) so a slow deployment never delays boot handling.

        Every failure is logged and swallowed: automation must never break an
        instance start.
        """
        try:
            profile_id = self._auto_map().get(instance)
        except Exception as exc:  # noqa: BLE001 - a broken setting is not a boot failure
            logger.warning("auto profile for %s could not read the binding: %s", instance, exc)
            return None
        if not profile_id:
            return None
        try:
            self.get_profile(profile_id)
        except AvdFleetError as exc:
            logger.warning(
                "auto profile for %s points at an unavailable profile: %s",
                instance,
                exc.message,
            )
            return None
        except Exception as exc:  # noqa: BLE001 - defensive: automation is best effort
            logger.warning("auto profile for %s could not load its profile: %s", instance, exc)
            return None
        boot_key = self._boot_key(instance)
        if boot_key is None:
            return None
        try:
            run_id = self._repo.claim_auto_run(instance, profile_id, boot_key)
        except Exception as exc:  # noqa: BLE001 - a storage failure must not escape
            logger.warning(
                "auto profile for %s could not claim boot %s: %s", instance, boot_key, exc
            )
            return None
        if run_id is None:
            logger.info("auto profile for %s already ran on boot %s", instance, boot_key)
            return None
        try:
            run = self.start_run(
                profile_id=profile_id,
                targets=[instance],
                label=f"auto on boot: {instance}",
                run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001 - release the claim, never raise
            self._repo.release_auto_run(instance, profile_id, boot_key)
            logger.warning(
                "auto profile for %s could not start: %s", instance, redact(str(exc))
            )
            return None
        self._log(
            run.id,
            instance,
            f"queued automatically when the instance booted (boot {boot_key})",
        )
        return run

    #: Commands that identify one guest boot on both API 25 and API 34.
    BOOT_SERIALNO_COMMAND = "getprop ro.boot.serialno"
    BOOT_ID_COMMAND = "cat /proc/sys/kernel/random/boot_id"

    def _boot_key(self, instance: str) -> str | None:
        """Return the boot identity for *instance*, or None when it is unknown.

        ``ro.boot.serialno`` names the emulator and
        ``/proc/sys/kernel/random/boot_id`` changes on every guest boot.  When the
        id cannot be read there is no way to prove this boot has not already been
        handled, so the caller fails closed instead of risking a second run.
        """
        try:
            target = self._resolve(instance)
        except Exception as exc:  # noqa: BLE001 - resolver failures are data here
            logger.warning(
                "auto profile for %s: cannot resolve the instance: %s",
                instance,
                getattr(exc, "message", str(exc)),
            )
            return None
        if not target.running:
            logger.info("auto profile for %s: the instance is not running", instance)
            return None
        serial = str(target.serial or "").strip()
        if not serial:
            logger.warning("auto profile for %s: the instance has no adb serial", instance)
            return None
        try:
            serialno = self._prober.shell(serial, self.BOOT_SERIALNO_COMMAND).text()
            boot_id = self._prober.shell(serial, self.BOOT_ID_COMMAND).text()
        except Exception as exc:  # noqa: BLE001 - the adb boundary is not trusted
            logger.warning(
                "auto profile for %s: could not read the boot identity: %s", instance, exc
            )
            return None
        if not boot_id:
            logger.warning(
                "auto profile for %s: the device reported no boot id; not queueing",
                instance,
            )
            return None
        return f"{serialno}:{boot_id}" if serialno else boot_id

    # -- toolchain acquisition --------------------------------------------
    def toolchain_status(self) -> dict[str, Any]:
        """Pinned upstream artifacts and what is already downloaded."""
        if self._acquisition is None:
            return {"artifacts": [], "manifest": [], "available": False}
        return {
            "artifacts": [item.as_dict() for item in self._acquisition.list_statuses()],
            "manifest": self._acquisition.manifest(),
            "available": True,
        }

    def acquire_toolchain(self, key: str, *, confirm: bool) -> dict[str, Any]:
        """Download a pinned upstream artifact and, for a Magisk module, import it.

        Acquisition is not installation: the artifact is verified and stored, the
        module ZIP is added to the library, and deploying it remains a separate
        explicit step.
        """
        if self._acquisition is None:
            raise ConfigError("toolchain acquisition is not configured")
        status = self._acquisition.acquire(key, confirm=confirm)
        module_id = ""
        if status.kind == "module_zip" and status.present:
            existing = self._repo.get_module_by_sha(status.sha256)
            if existing is not None:
                module_id = existing.id
            else:
                record = self._library.build_record(
                    Path(status.path).read_bytes(),
                    Path(status.path).name,
                    source="official-release",
                    source_url=status.source_url,
                )
                module_id = self._repo.add_module(record).id
        return {"artifact": status.as_dict(), "module_id": module_id}

    # -- profiles --------------------------------------------------------
    def list_profiles(self) -> list[InstrumentationProfile]:
        return self._repo.list_profiles()

    def get_profile(self, profile_id: str) -> InstrumentationProfile:
        profile = self._repo.get_profile(profile_id)
        if profile is None:
            raise NotFoundError(f"profile not found: {profile_id}")
        return profile

    def save_profile(
        self,
        *,
        profile_id: str | None,
        name: str,
        steps: Sequence[ProfileStep] | Sequence[dict[str, Any]],
        description: str = "",
    ) -> InstrumentationProfile:
        if not str(name or "").strip():
            raise ValidationError("a profile needs a name")
        parsed_steps = [_coerce_step(step) for step in steps]
        if not parsed_steps:
            raise ValidationError("a profile needs at least one step")
        existing = self._repo.get_profile(profile_id) if profile_id else None
        if profile_id and existing is None:
            raise NotFoundError(f"profile not found: {profile_id}")
        if existing is not None and existing.builtin:
            raise ValidationError(f"profile {existing.name!r} is built in and cannot be edited")
        profile = InstrumentationProfile(
            id=existing.id if existing else (profile_id or f"prof_{uuid.uuid4().hex[:12]}"),
            name=str(name).strip(),
            description=description,
            revision=(existing.revision + 1) if existing else 1,
            steps=parsed_steps,
            builtin=False,
            created_at=existing.created_at if existing else utcnow(),
            updated_at=utcnow(),
        )
        if existing is None:
            return self._repo.add_profile(profile)
        return self._repo.save_profile(profile)

    def delete_profile(self, profile_id: str) -> None:
        self._repo.delete_profile(profile_id)
        # Drop auto-start bindings pointing at the deleted profile.
        if self._settings is not None and self._auto_map():
            mapping = {
                instance: profile
                for instance, profile in self._auto_map().items()
                if profile != profile_id
            }
            self._settings.set(self.AUTO_PROFILE_KEY, mapping)

    def clone_profile(self, profile_id: str, *, name: str = "") -> InstrumentationProfile:
        """Copy any profile (including a built-in) into an editable one.

        Built-ins are read-only by design, but they are useless without the
        operator's own module choices, so cloning is the supported way to get an
        editable copy.
        """
        source = self.get_profile(profile_id)
        base = name.strip() or f"{source.name} (copy)"
        candidate = base
        index = 1
        while self._repo.get_profile_by_name(candidate) is not None:
            index += 1
            candidate = f"{base} {index}"
        stamp = utcnow()
        clone = InstrumentationProfile(
            id=f"prof_{uuid.uuid4().hex[:12]}",
            name=candidate,
            description=source.description,
            revision=1,
            steps=[step.model_copy(deep=True) for step in source.steps],
            builtin=False,
            created_at=stamp,
            updated_at=stamp,
        )
        return self._repo.add_profile(clone)

    # -- scanning --------------------------------------------------------
    def scan_device(self, instance: str) -> DeviceInstrumentation:
        target = self._resolve(instance)
        if not target.running:
            return DeviceInstrumentation(
                instance=instance,
                image_id=target.image_id,
                image_tag=target.image_tag,
                api_level=target.api_level,
                abi=target.abi,
                running=False,
                boot_completed=False,
                compatible=False,
                blockers=["instance is not running"],
            )
        return self._prober.probe(
            instance=target.instance,
            serial=target.serial,
            image_id=target.image_id,
            image_tag=target.image_tag,
            running=target.running,
            boot_completed=target.boot_completed,
            api_level=target.api_level,
            abi=target.abi,
        )

    def scan(self, targets: Iterable[str]) -> list[DeviceInstrumentation]:
        results: list[DeviceInstrumentation] = []
        for instance in _unique(targets):
            try:
                results.append(self.scan_device(instance))
            except AvdFleetError as exc:
                results.append(
                    DeviceInstrumentation(
                        instance=instance,
                        compatible=False,
                        blockers=[redact(exc.message)],
                    )
                )
        return results

    # -- deployments -----------------------------------------------------
    def start_run(
        self,
        *,
        profile_id: str,
        targets: Iterable[str],
        label: str = "",
        run_id: str | None = None,
    ) -> DeploymentRun:
        profile = self.get_profile(profile_id)
        selected = _unique(targets)
        if not selected:
            raise ValidationError("select at least one AVD to deploy to")
        run = DeploymentRun(
            id=run_id or f"irun_{uuid.uuid4().hex[:12]}",
            profile_id=profile.id,
            label=label or f"{profile.name} -> {len(selected)} AVD(s)",
            state=RunState.QUEUED,
            targets=list(selected),
            created_at=utcnow(),
        )
        return self._repo.create_run(run)

    def execute_run(self, run_id: str, *, max_concurrency: int | None = None) -> DeploymentRun:
        """Execute a run synchronously; the API schedules this on a worker thread."""
        run = self._repo.get_run(run_id)
        if run is None:
            raise NotFoundError(f"deployment run not found: {run_id}")
        if run.state is not RunState.QUEUED:
            self._log(run.id, "", f"run is {run.state}; not executing it")
            return run
        profile = self.get_profile(run.profile_id) if run.profile_id else None
        if profile is None:
            run.state = RunState.FAILED
            run.error = "the profile for this run no longer exists"
            run.finished_at = utcnow()
            return self._repo.save_run(run)

        cancellation = threading.Event()
        with self._lock:
            self._cancellations[run.id] = cancellation

        run.state = RunState.RUNNING
        run.started_at = utcnow()
        self._repo.save_run(run)
        self._emit(
            "started", run_id=run.id, profile_id=profile.id, targets=run.targets
        )
        self._log(run.id, "", f"deploying profile {profile.name!r} to {len(run.targets)} AVD(s)")

        workers = min(max_concurrency or self._max_concurrency, max(1, len(run.targets)))
        results: list[TargetResult] = []
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self._execute_target, run.id, profile, instance, cancellation): instance
                    for instance in run.targets
                }
                for future in futures:
                    instance = futures[future]
                    try:
                        results.append(future.result())
                    except Exception as exc:  # noqa: BLE001 - one target must not sink the run
                        logger.warning("target %s failed: %s", instance, exc)
                        results.append(
                            TargetResult(
                                instance=instance,
                                ok=False,
                                error=redact(f"{type(exc).__name__}: {exc}"),
                            )
                        )
        finally:
            with self._lock:
                self._cancellations.pop(run.id, None)

        run.results = results
        run.finished_at = utcnow()
        if cancellation.is_set():
            run.state = RunState.CANCELLED
        elif results and all(result.ok for result in results):
            run.state = RunState.SUCCEEDED
        elif any(result.ok for result in results):
            run.state = RunState.PARTIAL
        else:
            run.state = RunState.FAILED
        self._log(run.id, "", f"run finished: {run.state}")
        self._emit(
            "finished",
            run_id=run.id,
            state=str(run.state),
            targets=run.targets,
            failed=[result.instance for result in results if not result.ok],
        )
        return self._repo.save_run(run)

    def cancel_run(self, run_id: str) -> DeploymentRun:
        with self._lock:
            event = self._cancellations.get(run_id)
        if event is not None:
            event.set()
            self._log(run_id, "", "cancellation requested")
        run = self._repo.get_run(run_id)
        if run is None:
            raise NotFoundError(f"deployment run not found: {run_id}")
        if event is None and run.state in (RunState.QUEUED, RunState.RUNNING):
            run.state = RunState.CANCELLED
            run.finished_at = utcnow()
            self._repo.save_run(run)
        return run

    def list_runs(self, limit: int = 50) -> list[DeploymentRun]:
        return self._repo.list_runs(limit)

    def get_run(self, run_id: str) -> DeploymentRun:
        run = self._repo.get_run(run_id)
        if run is None:
            raise NotFoundError(f"deployment run not found: {run_id}")
        return run

    def logs(self, run_id: str, limit: int = 500) -> list[LogEntry]:
        return self._repo.list_logs(run_id, limit)

    # -- internals -------------------------------------------------------
    def _execute_target(
        self,
        run_id: str,
        profile: InstrumentationProfile,
        instance: str,
        cancellation: threading.Event,
    ) -> TargetResult:
        if cancellation.is_set():
            return TargetResult(instance=instance, ok=False, skipped=True, error="cancelled")
        try:
            target = self._resolve(instance)
        except AvdFleetError as exc:
            self._log(run_id, instance, f"cannot resolve instance: {exc.message}", level="error")
            return TargetResult(instance=instance, ok=False, error=redact(exc.message))

        if not target.running:
            self._log(run_id, instance, "instance is not running; skipping", level="warn")
            return TargetResult(
                instance=instance,
                ok=False,
                skipped=True,
                error="instance is not running (start it first)",
            )

        result = TargetResult(instance=instance)
        failed_steps: list[StepResult] = []
        for step in profile.steps:
            if cancellation.is_set():
                result.ok = False
                result.error = "cancelled"
                return result
            step_result = self._run_step(run_id, step, target)
            result.steps.append(step_result)
            self._log(
                run_id,
                instance,
                f"{step.action}: {'ok' if step_result.ok else 'failed'} - {step_result.message}",
                level="info" if step_result.ok else "error",
            )
            if not step_result.ok:
                failed_steps.append(step_result)
                if step.params.get("continue_on_error") is not True:
                    result.ok = False
                    result.error = step_result.message
                    return result
        if failed_steps:
            # continue_on_error keeps going, but the target is still not ok.
            result.ok = False
            result.error = "; ".join(item.message for item in failed_steps)
            return result
        result.ok = True
        return result

    def _run_step(self, run_id: str, step: ProfileStep, target: TargetDevice) -> StepResult:
        try:
            handler = getattr(self, f"_step_{step.action.value}", None)
            if handler is None:
                return StepResult(
                    step_id=step.id,
                    action=step.action,
                    ok=False,
                    state=ComponentState.UNSUPPORTED,
                    message=f"action {step.action} is not implemented",
                )
            return handler(step, target)
        except AvdFleetError as exc:
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                message=redact(exc.message),
            )
        except Exception as exc:  # noqa: BLE001 - a step failure is data, not a crash
            logger.warning("step %s failed on %s: %s", step.action, target.instance, exc)
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                message=redact(f"{type(exc).__name__}: {exc}"),
            )

    # -- step handlers ---------------------------------------------------
    def _step_verify_root(self, step: ProfileStep, target: TargetDevice) -> StepResult:
        device = self._probe(target)
        ok = device.root_implementation is not RootImplementation.NONE
        return StepResult(
            step_id=step.id,
            action=step.action,
            ok=ok,
            state=device.root_state,
            message=(
                f"root available via {device.root_implementation}"
                if ok
                else "; ".join(device.blockers) or "no root available"
            ),
            data={"root_implementation": str(device.root_implementation)},
        )

    def _step_install_magisk(self, step: ProfileStep, target: TargetDevice) -> StepResult:
        if self._magisk_installer is None:
            raise ConfigError(
                "no Magisk installer is configured",
                hint=(
                    "Magisk is GPL-licensed and is not bundled. Acquire the pinned release with"
                    " 'avdfleet instrumentation toolchain acquire magisk --confirm', then point"
                    " AVDFLEET_MAGISK_INSTALLER at your ramdisk patcher (for example rootAVD);"
                    " the verified APK path is passed to it as AVDFLEET_MAGISK_APK"
                ),
            )
        return self._magisk_installer(target.instance)

    def _step_complete_magisk_setup(
        self, step: ProfileStep, target: TargetDevice
    ) -> StepResult:
        """Drive Magisk's own "Additional Setup" to completion.

        The ramdisk payload only becomes a working environment once the Magisk app
        runs its setup; until then magiskd logs ``/data/adb is not present, abort``
        and no module script executes. Measured on API 34: the flow is an APK
        install, launching the app, then confirming dialogs -- completable without a
        human. Dialogs are driven from ``uiautomator dump`` so the tap targets come
        from the live UI instead of hard-coded coordinates.
        """
        if self._acquisition is None:
            raise ConfigError("toolchain acquisition is not configured")
        artifact = find_artifact("magisk")
        status = self._acquisition.status(artifact)
        if not (status.present and status.verified):
            raise ValidationError(
                "the pinned Magisk APK has not been acquired",
                hint=(
                    "run 'avdfleet instrumentation toolchain acquire magisk --confirm'"
                    " so the verified APK is available for setup"
                ),
            )
        device = self._probe(target)
        if not device.capabilities.magisk:
            # magiskd can still be starting, and shell su is rejected until the app
            # grants it; adbd root is enough to see whether the payload is alive.
            for _ in range(8):
                pid = self._prober.root_shell(target.serial, "pidof magiskd", magisk=True)
                version = self._prober.root_shell(target.serial, "magisk -V", magisk=True)
                if (pid.ok and pid.text()) or (version.ok and version.text()):
                    device.capabilities.magisk = True
                    break
                time.sleep(5)
        if device.capabilities.magisk_environment_ready:
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=True,
                state=ComponentState.ACTIVE,
                message="the Magisk environment is already complete",
            )
        if not device.capabilities.magisk:
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                state=ComponentState.UNSUPPORTED,
                message=(
                    "Magisk is not running on this device; patch the ramdisk first"
                    " (install_magisk), then run this step"
                ),
            )

        install = self._prober.run(target.serial, "install", "-r", "-g", status.path)
        output = (install.stdout or "") + (install.stderr or "")
        if not install.ok and "INSTALL_FAILED_ALREADY_EXISTS" not in output:
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                message=f"could not install the Magisk app: {redact(output.strip())[:300]}",
            )

        self._prober.shell(
            target.serial, "am start -n com.topjohnwu.magisk/.ui.MainActivity"
        )
        rebooted = False
        tapped = 0
        dialog_deadline = time.monotonic() + 150
        while time.monotonic() < dialog_deadline:
            time.sleep(2.5)
            if self._tap_dialog(
                target.serial, ("ok", "allow", "grant", "proceed", "yes", "reboot")
            ):
                tapped += 1
                if tapped >= 3:
                    break
                continue
            # A slow emulator shows "System UI isn't responding"; dismissing it keeps
            # the setup going instead of stalling until the deadline.
            if self._tap_dialog(target.serial, ("wait", "close app", "close", "ok")):
                continue
            ready = self._prober.root_shell(
                target.serial, "ls /data/adb/magisk", magisk=True
            )
            if ready.ok and ready.text():
                break
            boot = self._prober.shell(target.serial, "getprop sys.boot_completed")
            if not (boot.ok and boot.text().strip() == "1"):
                # The app is rebooting the device. A guest reboot drops adbd's root
                # state, so the cached root must not survive it.
                self._prober.forget_root(target.serial)
                rebooted = True
                break  # the app is rebooting the device; wait for it below

        # The app reboots the device itself once setup succeeds.
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            time.sleep(5)
            boot = self._prober.shell(target.serial, "getprop sys.boot_completed")
            if not (boot.ok and boot.text().strip() == "1"):
                if not rebooted:
                    self._prober.forget_root(target.serial)
                    rebooted = True
                continue
            ready = self._prober.root_shell(target.serial, "ls /data/adb/magisk", magisk=True)
            if ready.ok and ready.text():
                return StepResult(
                    step_id=step.id,
                    action=step.action,
                    ok=True,
                    state=ComponentState.ENABLED,
                    message=(
                        "Magisk setup completed and /data/adb/magisk is populated;"
                        " modules can now run"
                    ),
                    data={"reboot_required": True},
                )
        return StepResult(
            step_id=step.id,
            action=step.action,
            ok=False,
            message=(
                "Magisk setup did not complete unattended; open the Magisk app on the"
                " device and confirm 'Requires Additional Setup'"
            ),
        )

    def _tap_dialog(self, serial: str, labels: tuple[str, ...]) -> bool:
        """Tap the first UI button whose text matches *labels*; True when tapped."""
        dump = self._prober.shell(serial, "uiautomator dump /sdcard/avdfleet-ui.xml")
        if not dump.ok:
            # The window may still be drawing; one immediate retry is enough, the
            # caller keeps looping until its own deadline.
            time.sleep(1.0)
            dump = self._prober.shell(serial, "uiautomator dump /sdcard/avdfleet-ui.xml")
            if not dump.ok:
                return False
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "ui.xml"
            pull = self._prober.run(serial, "pull", "/sdcard/avdfleet-ui.xml", str(local))
            if not pull.ok or not local.is_file():
                return False
            try:
                tree = ElementTree.parse(local)
            except ElementTree.ParseError:
                return False
        center = _pick_dialog_target(tree.getroot(), labels)
        if center is None:
            return False
        self._prober.shell(serial, f"input tap {center[0]} {center[1]}")
        return True

    def _step_enable_zygisk(self, step: ProfileStep, target: TargetDevice) -> StepResult:
        device = self._probe(target)
        if not self._magisk_available(target, device):
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                state=ComponentState.UNSUPPORTED,
                message="Zygisk requires Magisk root",
            )
        result = self._root_shell(
            target,
            "magisk --sqlite \"REPLACE INTO settings (key, value) VALUES('zygisk', 1)\"",
            magisk=True,
        )
        if not result.ok:
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                message=f"could not persist the Zygisk setting: {result.text() or result.stderr}",
            )
        return StepResult(
            step_id=step.id,
            action=step.action,
            ok=True,
            state=ComponentState.ENABLED,
            message="Zygisk enabled; reboot the AVD and run the verify step to confirm it is active",
            data={"reboot_required": True},
        )

    def _step_disable_zygisk(self, step: ProfileStep, target: TargetDevice) -> StepResult:
        result = self._root_shell(
            target,
            "magisk --sqlite \"REPLACE INTO settings (key, value) VALUES('zygisk', 0)\"",
            magisk=True,
        )
        return StepResult(
            step_id=step.id,
            action=step.action,
            ok=result.ok,
            state=ComponentState.ENABLED if result.ok else ComponentState.UNKNOWN,
            message="Zygisk disabled; reboot to apply" if result.ok else result.text(),
        )

    def _magisk_available(
        self, target: TargetDevice, device: DeviceInstrumentation | None = None
    ) -> bool:
        """Whether Magisk can run commands here, independent of the shell's su grant.

        After a reboot Magisk has no stored policy for the shell user, so ``su -c``
        is rejected until the app grants it; adbd root still reaches magiskd, and
        that is what matters for installing modules and toggling Zygisk.
        """
        probe = device or self._probe(target)
        if probe.capabilities.magisk:
            return True
        version = self._prober.root_shell(target.serial, "magisk -V", magisk=True)
        if version.ok and version.text() and "not found" not in version.text().lower():
            probe.capabilities.magisk = True
            return True
        pid = self._prober.root_shell(target.serial, "pidof magiskd", magisk=True)
        if pid.ok and pid.text():
            probe.capabilities.magisk = True
            return True
        return False

    def _step_install_lsposed(self, step: ProfileStep, target: TargetDevice) -> StepResult:
        # LSPosed refuses to install below API 27 (verified live: it aborts with
        # "Unsupported Android version"). Fail early with the reason instead of
        # pushing a module the framework will reject.
        minimum = find_artifact("lsposed").min_api
        probed = self._probe(target)
        level = target.api_level or probed.api_level
        if level and level < minimum:
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                state=ComponentState.UNSUPPORTED,
                message=(
                    f"LSPosed requires Android API {minimum} or newer;"
                    f" this AVD is API {level}"
                ),
            )
        record = self._module_for(step, ModuleKind.LSPOSED_ZIP, "LSPosed")
        return self._install_magisk_module(step, target, record)

    def _step_install_module(self, step: ProfileStep, target: TargetDevice) -> StepResult:
        try:
            record = self._module_for(step, None, "module")
        except ValidationError as exc:
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                state=ComponentState.UNKNOWN,
                message=exc.message,
            )

        if record.kind is ModuleKind.XPOSED_APK:
            return self._install_xposed_apk(step, target, record)
        try:
            return self._install_magisk_module(step, target, record)
        except ValidationError as exc:
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                state=ComponentState.UNKNOWN,
                message=exc.message,
            )

    def _step_enable_module(self, step: ProfileStep, target: TargetDevice) -> StepResult:
        return self._module_marker(step, target, enable=True)

    def _step_disable_module(self, step: ProfileStep, target: TargetDevice) -> StepResult:
        return self._module_marker(step, target, enable=False)

    def _step_remove_module(self, step: ProfileStep, target: TargetDevice) -> StepResult:
        try:
            identifier = self._module_identifier(step)
        except ValidationError as exc:
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                state=ComponentState.UNKNOWN,
                message=exc.message,
            )

        result = self._root_shell(
            target, f"touch /data/adb/modules/{identifier}/remove", magisk=True
        )
        return StepResult(
            step_id=step.id,
            action=step.action,
            ok=result.ok,
            state=ComponentState.INSTALLED if result.ok else ComponentState.UNKNOWN,
            message=(
                f"module {identifier} queued for removal at the next boot"
                if result.ok
                else result.text()
            ),
        )

    def _step_enable_xposed_module(self, step: ProfileStep, target: TargetDevice) -> StepResult:
        """Enable or disable a recognised Xposed module in LSPosed's database."""
        package = _validate_package(
            str(step.params.get("package") or step.module_identifier or ""), "package"
        )
        enabled = bool(step.params.get("enabled", True))
        # Fail closed: an UPDATE that matches no row must not report success, and
        # LSPosed registers a module only after the framework has seen it.
        registered = self._root_shell(
            target,
            f"sqlite3 {LSPOSED_DB} "
            f"\"select mid from modules where module_pkg_name='{package}'\"",
            magisk=True,
        )
        mid = registered.text().splitlines()[0].strip() if registered.ok and registered.text() else ""
        if not mid.isdigit():
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                state=ComponentState.UNSUPPORTED,
                message=(
                    f"LSPosed does not recognise {package}; install the APK and let the"
                    " framework register it before changing its state"
                ),
                data={"package": package},
            )
        sql = (
            f"UPDATE modules SET enabled={1 if enabled else 0}"
            f" WHERE module_pkg_name='{package}'"
        )
        return self._write_lsposed(step, target, sql, package=package, enabled=enabled)

    def _step_set_xposed_scope(self, step: ProfileStep, target: TargetDevice) -> StepResult:
        """Replace or extend an Xposed module's application scope."""
        package = _validate_package(
            str(step.params.get("package") or step.module_identifier or ""), "package"
        )
        apps = step.params.get("apps") or []
        if not isinstance(apps, list) or not apps:
            raise ValidationError("set_xposed_scope needs a non-empty 'apps' list")
        targets = [_validate_package(str(app), "application package") for app in apps]
        lookup = self._root_shell(
            target,
            f"sqlite3 {LSPOSED_DB} "
            f"\"select mid from modules where module_pkg_name='{package}'\"",
            magisk=True,
        )
        mid = lookup.text().splitlines()[0].strip() if lookup.ok and lookup.text() else ""
        if not mid.isdigit():
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                state=ComponentState.UNSUPPORTED,
                message=(
                    f"LSPosed does not recognise {package}; install the APK and open the"
                    " LSPosed manager once so the module is registered"
                ),
            )
        statements = []
        if step.params.get("replace", True):
            statements.append(f"DELETE FROM scope WHERE mid={mid}")
        for app in targets:
            statements.append(
                "INSERT OR IGNORE INTO scope (mid, app_pkg_name, user_id)"
                f" VALUES ({mid}, '{app}', 0)"
            )
        return self._write_lsposed(
            step, target, "; ".join(statements), package=package, scope=targets
        )

    def _write_lsposed(
        self,
        step: ProfileStep,
        target: TargetDevice,
        sql: str,
        *,
        package: str,
        **data: Any,
    ) -> StepResult:
        """Apply one LSPosed database change; lspd reloads it only at the next boot."""
        result = self._root_shell(target, f"sqlite3 {LSPOSED_DB} \"{sql}\"", magisk=True)
        ok = result.ok
        return StepResult(
            step_id=step.id,
            action=step.action,
            ok=ok,
            state=ComponentState.ENABLED if ok else ComponentState.UNKNOWN,
            message=(
                f"LSPosed configuration updated for {package}; reboot the AVD so lspd reloads it"
                if ok
                else (result.text() or result.stderr or "the LSPosed database write failed")
            ),
            data={"package": package, "reboot_required": ok, **data},
        )

    def _step_enable_adb_root(self, step: ProfileStep, target: TargetDevice) -> StepResult:
        """Restart adbd as uid 0 where the image permits it."""
        result = self._prober.run(target.serial, "root")
        if not result.ok:
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                state=ComponentState.UNSUPPORTED,
                message=(
                    "this image refuses adb root (a production/user build);"
                    " Magisk is the only root path here"
                ),
            )
        check = self._prober.shell(target.serial, "id")
        ok = check.ok and "uid=0" in check.text()
        return StepResult(
            step_id=step.id,
            action=step.action,
            ok=ok,
            state=ComponentState.ENABLED if ok else ComponentState.UNKNOWN,
            message=(
                "adbd is running as uid 0 (host-side root; it does not give applications su"
                " and does not survive a reboot)"
                if ok
                else (check.text() or "adbd did not come back as root")
            ),
            data={"adb_root": ok},
        )

    def _step_enable_writable_system(
        self, step: ProfileStep, target: TargetDevice
    ) -> StepResult:
        """Request a writable /system and report whether it is writable *now*."""
        self._prober.run(target.serial, "root")
        remount = self._prober.run(target.serial, "remount")
        device = self._probe(target)
        writable = device.capabilities.writable_system
        return StepResult(
            step_id=step.id,
            action=step.action,
            ok=writable,
            state=ComponentState.ENABLED if writable else ComponentState.INSTALLED,
            message=(
                "/system is writable"
                if writable
                else (
                    "remount requested, but /system is still read-only: on API 30+ this sets"
                    " up overlayfs and needs a reboot before it becomes writable"
                    if remount.ok
                    else (remount.text() or remount.stderr or "adb remount failed")
                )
            ),
            data={"writable_system": writable, "reboot_required": not writable and remount.ok},
        )

    def _step_install_lsposed_manager(
        self, step: ProfileStep, target: TargetDevice
    ) -> StepResult:
        """Install the LSPosed manager, which lspd deliberately does not install."""
        apk = "/data/adb/modules/zygisk_lsposed/manager.apk"
        exists = self._root_shell(target, f"[ -f {apk} ] && echo yes", magisk=True)
        if "yes" not in exists.stdout.lower():
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                state=ComponentState.UNSUPPORTED,
                message="the LSPosed module is not installed on this device",
            )
        install = self._root_shell(target, f"pm install -r {apk}", magisk=True)
        check = self._prober.shell(target.serial, "pm list packages org.lsposed.manager")
        installed = {
            line.strip().removeprefix("package:")
            for line in check.stdout.splitlines()
            if line.strip()
        }
        ok = check.ok and "org.lsposed.manager" in installed
        return StepResult(
            step_id=step.id,
            action=step.action,
            ok=ok,
            state=ComponentState.ACTIVE if ok else ComponentState.INSTALLED,
            message=(
                "LSPosed manager installed (org.lsposed.manager); open it to enable modules"
                " and set their application scope"
                if ok
                else (install.text() or install.stderr or "the manager APK did not install")
            ),
            data={"package": "org.lsposed.manager"},
        )

    def _step_reboot(self, step: ProfileStep, target: TargetDevice) -> StepResult:
        """Reboot the AVD and wait for it to come back, so later steps see the result."""
        budget = float(step.params.get("timeout_s", 300))
        before = self._prober.shell(
            target.serial, "cat /proc/sys/kernel/random/boot_id"
        )
        previous = before.text().strip() if before.ok else ""
        issued = self._prober.run(target.serial, "reboot")
        if not issued.ok:
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                message=(
                    "the reboot command failed: "
                    f"{issued.text() or issued.stderr or issued.returncode}"
                ),
            )
        # A guest reboot drops adbd's root state; re-establish it on the next root call.
        self._prober.forget_root(target.serial)
        deadline = time.monotonic() + budget
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Never sleep past the caller's budget.
            time.sleep(min(5.0, remaining))
            boot = self._prober.shell(target.serial, "getprop sys.boot_completed")
            current = self._prober.shell(
                target.serial, "cat /proc/sys/kernel/random/boot_id"
            )
            rebooted = bool(
                previous
                and current.ok
                and current.text().strip()
                and current.text().strip() != previous
            )
            if boot.ok and boot.text().strip() == "1" and (rebooted or not previous):
                return StepResult(
                    step_id=step.id,
                    action=step.action,
                    ok=True,
                    state=ComponentState.ACTIVE,
                    message="the AVD rebooted and finished booting",
                    data={"timeout_s": budget},
                )
        return StepResult(
            step_id=step.id,
            action=step.action,
            ok=False,
            message=f"the AVD did not finish booting within {budget:.0f}s",
        )

    def _step_remove_lsposed(self, step: ProfileStep, target: TargetDevice) -> StepResult:
        """Queue the LSPosed module for removal at the next boot."""
        base = "/data/adb/modules/zygisk_lsposed"
        exists = self._root_shell(
            target, f"[ -d {base} ] && echo yes || echo no", magisk=True
        )
        definitely_absent = "no" in exists.stdout.lower() and "yes" not in exists.stdout.lower()
        if definitely_absent:
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=True,
                state=ComponentState.ABSENT,
                message="LSPosed is not installed",
            )
        if not (exists.ok and exists.stdout.strip()):
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                state=ComponentState.UNKNOWN,
                message=(
                    "could not determine whether LSPosed is installed: "
                    f"{exists.text() or exists.stderr or 'no root access'}"
                ),
            )
        result = self._root_shell(target, f"touch {base}/remove", magisk=True)
        return StepResult(
            step_id=step.id,
            action=step.action,
            ok=result.ok,
            state=ComponentState.INSTALLED if result.ok else ComponentState.UNKNOWN,
            message=(
                "LSPosed queued for removal at the next boot; reboot to complete it"
                if result.ok
                else (result.text() or "could not write the remove marker")
            ),
            data={"reboot_required": result.ok},
        )

    def _step_remove_magisk(self, step: ProfileStep, target: TargetDevice) -> StepResult:
        """Refuse to fake removal: Magisk lives in the image ramdisk, not in /data."""
        return StepResult(
            step_id=step.id,
            action=step.action,
            ok=False,
            state=ComponentState.UNSUPPORTED,
            message=(
                "removing Magisk means restoring the image ramdisk, which mutates the shared"
                " system image; stop the AVD, restore ramdisk.img from ramdisk.img.backup"
                " (rootAVD keeps it) and reinstall the image if needed"
            ),
        )

    def _step_verify(self, step: ProfileStep, target: TargetDevice) -> StepResult:
        device = self._probe(target)
        caps = device.capabilities
        checks = {
            "root": device.root_implementation is not RootImplementation.NONE,
            "magisk": caps.magisk,
            "zygisk_active": caps.zygisk_active,
            "lsposed_active": caps.lsposed_active,
        }
        expected = step.params.get("expect") or {}
        unknown = [
            name
            for name, value in expected.items()
            if name not in checks or not isinstance(value, bool)
        ]
        failed = [
            name
            for name, value in checks.items()
            if isinstance(expected.get(name), bool) and expected[name] is not value
        ]
        failed.extend(unknown)
        return StepResult(
            step_id=step.id,
            action=step.action,
            ok=not failed,
            state=ComponentState.ACTIVE if not failed else ComponentState.ENABLED,
            message=(
                "all expected components are operational"
                if not failed
                else "not operational yet: " + ", ".join(failed)
            ),
            data={"checks": checks, "failed": failed},
        )

    # -- helpers ---------------------------------------------------------
    def _probe(self, target: TargetDevice) -> DeviceInstrumentation:
        return self._prober.probe(
            instance=target.instance,
            serial=target.serial,
            image_id=target.image_id,
            image_tag=target.image_tag,
            running=target.running,
            boot_completed=target.boot_completed,
            api_level=target.api_level,
            abi=target.abi,
        )

    def _module_for(
        self, step: ProfileStep, expected: ModuleKind | None, what: str
    ) -> ModuleRecord:
        if step.module_id:
            record = self.get_module(step.module_id)
        elif not step.module_identifier and expected is not None:
            # A built-in profile has no module bound by design (the operator's
            # library is unknown to it), so it uses the newest library artifact
            # of the required kind rather than failing on a missing binding.
            candidates = self._repo.list_modules(expected)
            if not candidates:
                raise NotFoundError(
                    f"no {what} is available in the module library",
                    hint=(
                        "acquire the pinned release with"
                        " 'avdfleet instrumentation toolchain acquire lsposed'"
                        " or import the file manually"
                    ),
                )
            record = candidates[0]
        elif step.module_identifier:
            matches = [
                module
                for module in self._repo.list_modules()
                if module.package_name == step.module_identifier or module.name == step.module_identifier
            ]
            if not matches:
                raise NotFoundError(f"no library module named {step.module_identifier!r}")
            record = matches[0]
        else:
            raise ValidationError(f"the {what} step needs a module from the library")
        if expected is not None and record.kind is not expected:
            raise ValidationError(
                f"{record.name or record.id} is a {record.kind}, not a {expected}",
                hint="import the right artifact type into the module library",
            )
        return record

    def _module_identifier(self, step: ProfileStep) -> str:
        if step.module_identifier:
            return _validate_module_id(step.module_identifier)
        if step.module_id:
            record = self.get_module(step.module_id)
            prop = record.metadata.get("module_prop") or {}
            identifier = prop.get("id") or record.package_name
            if identifier:
                return _validate_module_id(str(identifier))
        raise ValidationError("this step needs a module identifier")

    def _install_magisk_module(
        self, step: ProfileStep, target: TargetDevice, record: ModuleRecord
    ) -> StepResult:
        device = self._probe(target)
        if not self._magisk_available(target, device):
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                state=ComponentState.UNSUPPORTED,
                message="installing a Magisk module requires Magisk root",
            )
        remote = f"{STAGING_DIR}/{Path(record.file_path).name}"
        push = self._prober.run(target.serial, "push", record.file_path, remote)
        if not push.ok:
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                message=f"could not push the module: {push.text() or push.stderr}",
            )
        install = self._root_shell(target, f"magisk --install-module {remote}", magisk=True)
        prop = record.metadata.get("module_prop") or {}
        raw_identifier = str(prop.get("id") or "") or _module_id_from_archive(
            record.file_path
        )
        identifier = _validate_module_id(raw_identifier) if raw_identifier else ""
        if not identifier:
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                state=ComponentState.INSTALLED,
                message=(
                    "the archive declares no module id, so the install cannot be verified;"
                    " add id=<name> to its module.prop"
                ),
            )
        present = False
        if identifier:
            check = self._root_shell(
                target, f"[ -d /data/adb/modules/{identifier} ] && echo yes", magisk=True
            )
            present = "yes" in check.stdout.lower()
        ok = install.ok and (present or not identifier)
        return StepResult(
            step_id=step.id,
            action=step.action,
            ok=ok,
            state=ComponentState.ENABLED if ok else ComponentState.INSTALLED,
            message=(
                f"{record.name or record.kind} installed; reboot the AVD for it to become active"
                if ok
                else (install.text() or install.stderr or "the module was not installed")
            ),
            data={"module_id": record.id, "identifier": identifier, "reboot_required": ok},
        )

    def _install_xposed_apk(
        self, step: ProfileStep, target: TargetDevice, record: ModuleRecord
    ) -> StepResult:
        device = self._probe(target)
        if not device.capabilities.lsposed_installed:
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                state=ComponentState.UNSUPPORTED,
                message="installing an Xposed APK needs LSPosed (or another Xposed framework)",
            )
        install = self._prober.run(target.serial, "install", "-r", "-g", record.file_path)
        if not install.ok:
            message = install.stdout or install.stderr
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                message=f"adb install failed: {redact(message.strip())}",
            )
        return StepResult(
            step_id=step.id,
            action=step.action,
            ok=True,
            state=ComponentState.INSTALLED,
            message=(
                "APK installed; Xposed modules are only active once enabled and scoped in the"
                " LSPosed manager (guided step required)"
            ),
            data={
                "package_name": record.package_name,
                "activation": "requires-lsposed-manager",
            },
        )

    def _module_marker(
        self, step: ProfileStep, target: TargetDevice, *, enable: bool
    ) -> StepResult:
        try:
            identifier = self._module_identifier(step)
        except ValidationError as exc:
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                state=ComponentState.UNKNOWN,
                message=exc.message,
            )

        base = f"/data/adb/modules/{identifier}"
        present = self._root_shell(target, f"[ -d {base} ] && echo yes", magisk=True)
        if not (present.ok and "yes" in present.stdout.lower()):
            return StepResult(
                step_id=step.id,
                action=step.action,
                ok=False,
                state=ComponentState.ABSENT,
                message=(
                    f"module {identifier} is not installed under /data/adb/modules, so it"
                    " cannot be enabled or disabled"
                ),
            )
        command = (
            f"rm -f {base}/{DISABLE_MARKER}"
            if enable
            else f"touch {base}/{DISABLE_MARKER}"
        )
        result = self._root_shell(target, command, magisk=True)
        state = ComponentState.ENABLED if enable else ComponentState.INSTALLED
        return StepResult(
            step_id=step.id,
            action=step.action,
            ok=result.ok,
            state=state if result.ok else ComponentState.UNKNOWN,
            message=(
                f"{'enabled' if enable else 'disabled'} {identifier}; reboot to apply"
                if result.ok
                else result.text()
            ),
        )

    def _root_shell(self, target: TargetDevice, command: str, *, magisk: bool) -> Any:
        return self._prober.root_shell(target.serial, command, magisk=magisk)

    def _emit(self, action: str, **payload: Any) -> None:
        """Publish an instrumentation event on the existing WebSocket bus."""
        if self._events is None:
            return
        try:
            from ..core.events import Event, EventType

            self._events.publish(
                Event(
                    type=EventType.INSTRUMENTATION_RUN,
                    source="instrumentation",
                    payload={"action": action, **payload},
                )
            )
        except Exception as exc:  # noqa: BLE001 - telemetry must never break a run
            logger.debug("could not publish instrumentation event: %s", exc)

    def _log(self, run_id: str, instance: str, message: str, *, level: str = "info") -> None:
        try:
            self._repo.append_log(
                LogEntry(
                    run_id=run_id,
                    instance=instance,
                    level=level,
                    message=redact(message),
                    at=utcnow(),
                )
            )
        except Exception as exc:  # noqa: BLE001 - logging must never break a run
            logger.debug("could not persist instrumentation log: %s", exc)


DISABLE_MARKER = "disable"


def _module_id_from_archive(path: str) -> str:
    """Read ``module.prop`` out of a stored module ZIP and return its id.

    Archives whose metadata was not captured at import time are still verified after
    installation; without an id the presence check would have to be waived, so the id
    is derived here instead.
    """
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            if "module.prop" not in archive.namelist():
                return ""
            text = archive.read("module.prop").decode("utf-8", "replace")
    except (OSError, zipfile.BadZipFile):
        return ""
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "id":
            return value.strip()
    return ""

_BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _last_int(text: str) -> int:
    """Return the last integer in *text*, or 0 (sqlite `select changes()`)."""
    matches = re.findall(r"-?\d+", text or "")
    return int(matches[-1]) if matches else 0


def _pick_dialog_target(
    tree: ElementTree.Element, labels: tuple[str, ...]
) -> tuple[int, int] | None:
    """Choose the tap point for a dialog button.

    Only clickable nodes qualify, an exact label beats a substring, and a generic
    ``button1`` id is the last resort. The body text of Magisk's setup dialog contains
    the word "proceed", so a naive substring match would tap the message instead of
    the confirming button -- which is exactly the bug this ranking prevents.
    """
    ranked: list[tuple[int, tuple[int, int]]] = []
    for node in tree.iter("node"):
        if (node.get("clickable") or "").lower() != "true":
            continue
        text = (node.get("text") or "").strip().lower()
        resource = (node.get("resource-id") or "").lower()
        center = _bounds_center(node.get("bounds") or "")
        if center is None:
            continue
        if text and text in labels:
            ranked.append((0, center))
        elif text and any(label in text for label in labels):
            ranked.append((1, center))
        elif resource.endswith("button1"):
            ranked.append((2, center))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


def _bounds_center(value: str) -> tuple[int, int] | None:
    """Return the centre of a uiautomator ``[x1,y1][x2,y2]`` bounds string."""
    match = _BOUNDS.search(value or "")
    if match is None:
        return None
    x1, y1, x2, y2 = (int(part) for part in match.groups())
    return ((x1 + x2) // 2, (y1 + y2) // 2)

_PACKAGE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")

#: Magisk module ids are directory names: letters, digits, dot, underscore, dash.
_MODULE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _validate_module_id(value: str) -> str:
    """Reject a module id that could escape or inject into a root shell command.

    The id is interpolated into ``/data/adb/modules/<id>`` paths that run as root,
    so this is the shell-injection boundary for ids coming from an imported ZIP's
    ``module.prop`` as well as from a profile step.
    """
    candidate = (value or "").strip()
    if not _MODULE_ID_PATTERN.match(candidate) or candidate in {".", ".."}:
        raise ValidationError(
            f"invalid Magisk module id: {value!r}",
            hint="module ids may contain letters, digits, dot, underscore and dash",
        )
    return candidate


def _validate_package(value: str, what: str) -> str:
    """Reject anything that is not a plausible Android package name.

    The value is embedded in a device-side SQL statement, so this is both an
    input check and the injection boundary.
    """
    candidate = (value or "").strip()
    if not _PACKAGE_PATTERN.match(candidate):
        raise ValidationError(
            f"invalid Android {what}: {value!r}",
            hint="expected a package name like com.example.module",
        )
    return candidate


def _unique(values: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        name = str(value or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def _coerce_step(step: ProfileStep | dict[str, Any]) -> ProfileStep:
    if isinstance(step, ProfileStep):
        return step
    if not isinstance(step, dict):
        raise ValidationError("a profile step must be an object")
    try:
        action = StepAction(str(step.get("action")))
    except ValueError as exc:
        raise ValidationError(
            f"unknown step action: {step.get('action')!r}",
            hint="use one of: " + ", ".join(item.value for item in StepAction),
        ) from exc
    return ProfileStep(
        id=str(step.get("id") or f"step_{uuid.uuid4().hex[:8]}"),
        action=action,
        module_id=str(step.get("module_id") or ""),
        module_identifier=str(step.get("module_identifier") or ""),
        params=dict(step.get("params") or {}),
    )


__all__ = [
    "DEFAULT_CONCURRENCY",
    "InstrumentationService",
    "STAGING_DIR",
    "TargetDevice",
    "TargetResolver",
]
