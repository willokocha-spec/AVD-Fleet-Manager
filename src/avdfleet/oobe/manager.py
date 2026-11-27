"""Concrete OOBE service.

:class:`OobeManager` implements :class:`avdfleet.core.interfaces.OobeManager` on
top of injected services only:

* ``runtime`` — ``RuntimeManager`` implementation (N1): start/stop/wait_boot/
  snapshots/status;
* ``avds`` — ``AvdManager`` implementation (A1): creates the temporary AVD used
  for ``verify_image`` and deletes it afterwards;
* ``images`` — ``ImageCatalog`` (G1x): image metadata and the persisted OOBE
  matrix (``oobe_matrix.json``);
* ``adb`` — an injected process runner used for ``dumpsys`` parsing, boot
  facts and screenshot fallbacks (S1/N1 subprocess helper or a test fake);
* ``screen`` — optional ``ScreenClient``-like object used to capture evidence
  screenshots (V1); the adb ``screencap`` path is the fallback.

The manager owns the OOBE matrix evidence policy, failure records and the
golden-snapshot marker file.  It never raises for *operational* failures of
``verify_image``/``reset``: those are reported as failed
:class:`~avdfleet.core.models.OobeRecord` / ``OobeResetResult`` values so the API
and CLI can render them.  Argument errors (unknown image/instance, invalid
strategy) still raise the core error types.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ..core.config import Config
from ..core.errors import AvdFleetError, NotFoundError, OobeError, ValidationError
from ..core.events import EventSink, EventType
from ..core.logging import get_logger, redact
from ..core.models import (
    AvdInfo,
    OobeMatrix,
    OobeRecord,
    OobeResetResult,
    OobeResetStrategy,
    OobeStatus,
    Snapshot,
    SystemImage,
    slugify,
    utcnow,
)
from ..core.paths import Paths
from .detection import (
    Detection,
    OobeDetector,
    is_png,
    screenshot_filename,
    write_bytes_atomic,
    write_text_atomic,
)
from .matrix import GOLDEN_SNAPSHOT, OobeMatrixRegistry
from .reset import (
    PACKAGE_CLEANUP_COMMANDS,
    ResetEngine,
    invoke_adaptive,
    is_active,
    parse_reset_strategy,
)

logger = get_logger("avdfleet.oobe")

DEFAULT_DEVICE_ID = "pixel_7"
DEFAULT_TIMEOUT_S = 300
GOLDEN_MARKER_FILE = "oobe_golden_snapshots.json"

_PROVISIONER_METHODS: tuple[str, ...] = (
    "create_instance_from_image",
    "create_avd_from_image",
    "create_temp_avd",
    "create_from_image",
)


class OobeManager:
    """OOBE verification, reset strategies and the image OOBE matrix."""

    def __init__(
        self,
        runtime: Any,
        avds: Any,
        images: Any,
        paths: Paths | str | Path,
        *,
        adb: Any | None = None,
        adb_executable: str = "adb",
        screen: Any | None = None,
        config: Config | None = None,
        emulator_version: str | Callable[[], str] | None = None,
        provisioner: Callable[..., AvdInfo] | None = None,
        clock: Callable[[], float] = time.monotonic,
        event_bus: Any | None = None,
        detector: OobeDetector | None = None,
        reset_engine: ResetEngine | None = None,
        device_id: str = DEFAULT_DEVICE_ID,
        boot_timeout_s: int | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        if runtime is None:
            raise OobeError("an instance runtime is required for OOBE services")
        if avds is None:
            raise OobeError("an AvdManager is required for OOBE verify_image")
        if images is None:
            raise OobeError("an ImageCatalog is required for the OOBE matrix")
        self._runtime = runtime
        self._avds = avds
        self._images = images
        self._paths = paths if isinstance(paths, Paths) else _paths_for_root(paths)
        self._config = config
        self._device_id = device_id
        self._clock = clock
        self._logger = logger_ or logger
        self._screen = screen
        self._provisioner = provisioner
        self._emulator_version_source = emulator_version
        self._boot_timeout_s = int(
            boot_timeout_s
            if boot_timeout_s is not None
            else getattr(config, "boot_timeout_s", DEFAULT_TIMEOUT_S)
        )
        request_timeout = float(getattr(config, "request_timeout_s", 30.0) or 30.0)
        self._detector = detector or OobeDetector(
            adb,
            adb=adb_executable,
            timeout_s=request_timeout,
            logger_=self._logger,
        )
        self._matrix = OobeMatrixRegistry(images, logger_=self._logger)
        self._events = EventSink(event_bus) if event_bus is not None else None
        self._warnings: list[str] = []
        self._snapshot_name = GOLDEN_SNAPSHOT
        self._reset_engine = reset_engine or ResetEngine(
            runtime,
            detector=self._detector,
            serial_for=lambda name: self._serial_for(name),
            golden_available=self._golden_available,
            snapshot_name=self._snapshot_name,
            clock=clock,
            boot_timeout_s=self._boot_timeout_s,
            logger_=self._logger,
        )

    # -- introspection ---------------------------------------------------
    @property
    def detector(self) -> OobeDetector:
        return self._detector

    @property
    def reset_engine(self) -> ResetEngine:
        return self._reset_engine

    @property
    def warnings(self) -> list[str]:
        collected = list(self._warnings)
        for warning in self._matrix.warnings:
            if warning not in collected:
                collected.append(warning)
        return collected

    @property
    def emulator_version(self) -> str:
        return self._emulator_version_value()

    # -- public protocol -------------------------------------------------
    def matrix(self) -> OobeMatrix:
        """Return the persisted OOBE matrix with evidence enforcement applied."""
        return self._matrix.load()

    def record_for(self, image_id: str) -> OobeRecord | None:
        return self._matrix.record_for(image_id)

    def detect(self, avd_name: str) -> OobeRecord:
        """Detect the current OOBE state of a *running* instance.

        The returned record is evidence only; it is not persisted — use
        :meth:`verify_image` to record an image verification.
        """
        info = self._instance(avd_name)
        if not is_active(info):
            raise OobeError(
                f"instance {avd_name!r} is not running",
                hint="start the instance before detecting OOBE",
                details={"avd": avd_name},
            )
        serial = self._serial_for(avd_name, info)
        return self._detection_record(avd_name, serial, image_id=info.image_id or "", info=info, avd_info=info)

    def verify_image(
        self,
        image_id: str,
        *,
        timeout_s: int | None = None,
        keep_avd: bool = False,
    ) -> OobeRecord:
        """Boot a temporary AVD from *image_id* and record OOBE evidence."""
        image = self._images.get_image(image_id)
        timeout = float(timeout_s if timeout_s is not None else self._boot_timeout_s)
        avd_name = self._temp_avd_name(image)
        record = OobeRecord(
            image_id=image.id,
            api_level=image.api_level,
            tag=image.tag,
            abi=image.abi,
            status=OobeStatus.UNVERIFIED,
            checked_at=utcnow(),
            checked_by="avdfleet",
            emulator_version=self._emulator_version_value(),
            notes=f"created temporary AVD {avd_name} for OOBE verification"
            if image.installed
            else "image is not installed; download it before verifying OOBE",
            evidence=[],
        )
        boot_seconds: float | None = None
        cleanup_problems: list[str] = []
        provisioned = False
        try:
            if not image.installed:
                record.status = OobeStatus.FAILED
            else:
                info = self._provision(image, avd_name)
                provisioned = True
                boot_started = self._clock()
                self._start(avd_name)
                self._wait_boot(avd_name, timeout)
                boot_seconds = round(max(0.0, self._clock() - boot_started), 6)
                live_info = self._safe_instance(avd_name) or info
                serial = self._serial_for(avd_name, live_info)
                detected = self._detection_record(
                    avd_name,
                    serial,
                    image_id=image.id,
                    info=live_info,
                    avd_info=live_info,
                )
                record = self._merge_detection(record, detected, image, boot_seconds=boot_seconds)
        except AvdFleetError as exc:
            record = record.model_copy(
                update={
                    "status": OobeStatus.FAILED,
                    "notes": _append_note(record.notes, f"verification failed: {redact(str(exc))}"),
                    "checked_at": utcnow(),
                }
            )
            self._logger.warning("OOBE verification failed for image %s: %s", image.id, exc)
        except Exception as exc:  # noqa: BLE001 - safe failure record
            record = record.model_copy(
                update={
                    "status": OobeStatus.FAILED,
                    "notes": _append_note(
                        record.notes, f"verification failed: {redact(f'{type(exc).__name__}: {exc}')}"
                    ),
                    "checked_at": utcnow(),
                }
            )
            self._logger.warning("OOBE verification crashed for image %s", image.id, exc_info=True)
        finally:
            cleanup_problems = self._cleanup_temp_avd(avd_name, keep=keep_avd) if provisioned else []
            record = record.model_copy(
                update={
                    "boot_seconds": boot_seconds if boot_seconds is not None else record.boot_seconds,
                    "notes": (
                        _with_cleanup_notes(record.notes, cleanup_problems, kept=keep_avd)
                        if provisioned
                        else record.notes
                    ),
                    "evidence": list(record.evidence),
                }
            )
            record = self._persist(record)
        return record

    def reset(
        self,
        avd_name: str,
        *,
        strategy: str | OobeResetStrategy | None = None,
        verify: bool = True,
    ) -> OobeResetResult:
        """Reset *avd_name* to the out-of-box experience and time it."""
        self._instance(avd_name)  # 404 before doing anything destructive
        chosen = parse_reset_strategy(strategy) if strategy is not None else self._default_strategy(avd_name)
        result = self._reset_engine.run(
            avd_name,
            chosen,
            verify=verify,
            detect=self.detect if verify else None,
        )
        self._emit(
            EventType.OOBE_RESULT,
            {
                "kind": "reset",
                "avd_name": result.avd_name,
                "strategy": result.strategy.value,
                "ok": result.ok,
                "seconds": result.seconds,
                "activity": result.activity,
            },
        )
        return result

    def ensure_golden_snapshot(self, avd_name: str, snapshot: str = GOLDEN_SNAPSHOT) -> Snapshot:
        """Confirm OOBE is visible on the instance and save the golden snapshot.

        A running instance that already shows the Setup Wizard *is* a valid
        first-boot state (callers normally create the AVD immediately before
        calling this), so it is captured without an unnecessary stop/restart;
        a stopped or already-completed instance is booted/restarted first.
        """
        snapshot_name = str(snapshot or GOLDEN_SNAPSHOT).strip() or GOLDEN_SNAPSHOT
        info = self._instance(avd_name)
        existing = self._find_snapshot(avd_name, snapshot_name)
        if existing is not None and self._golden_available(avd_name):
            return _as_golden_snapshot(existing, avd_name, snapshot_name)

        detected = self._golden_detection(avd_name, info)
        if detected.status is not OobeStatus.VERIFIED:
            if is_active(info):
                # The guest is not on the Setup Wizard: restart it cold and
                # re-check (a reboot alone does not clear completed setup, so a
                # persistent failure is reported with a wipe hint).
                self._stop(avd_name)
            self._start(avd_name)
            self._wait_boot(avd_name, self._boot_timeout_s)
            live_info = self._safe_instance(avd_name) or info
            detected = self._detection_record(
                avd_name,
                self._serial_for(avd_name, live_info),
                image_id=live_info.image_id or info.image_id or "",
                info=live_info,
                avd_info=live_info,
            )
        if detected.status is not OobeStatus.VERIFIED:
            raise OobeError(
                f"cannot save golden snapshot {snapshot_name!r}: the setup wizard is not visible"
                f" on {avd_name!r}",
                hint="reset the instance with the wipe strategy, then retry",
                details={"activity": detected.activity, "notes": detected.notes},
            )
        saver = getattr(self._runtime, "save_snapshot", None)
        if not callable(saver):
            raise OobeError(
                "the runtime cannot save snapshots",
                hint="RuntimeManager.save_snapshot(name, snapshot, golden=True) is required",
            )
        saved = invoke_adaptive(saver, avd_name, snapshot_name, golden=True)
        golden = _as_golden_snapshot(saved, avd_name, snapshot_name)
        marker = {
            "avd_name": avd_name,
            "snapshot": snapshot_name,
            "image_id": detected.image_id,
            "activity": detected.activity,
            "screenshot_path": detected.screenshot_path,
            "boot_seconds": detected.boot_seconds,
            "emulator_version": detected.emulator_version,
            "evidence": list(detected.evidence),
            "created_at": utcnow().isoformat(),
            "checked_by": "avdfleet",
        }
        self._write_golden_marker(avd_name, marker)
        self._logger.info("saved golden OOBE snapshot %s for %s", snapshot_name, avd_name)
        return golden

    # -- golden snapshot bookkeeping ------------------------------------
    def _golden_detection(self, avd_name: str, info: AvdInfo) -> OobeRecord:
        """Detect OOBE on the currently running instance (used by golden save)."""
        live_info = info
        if not is_active(live_info):
            return OobeRecord(
                image_id=info.image_id or "",
                status=OobeStatus.FAILED,
                notes="instance is not running",
                checked_at=utcnow(),
                emulator_version=self._emulator_version_value(),
            )
        return self._detection_record(
            avd_name,
            self._serial_for(avd_name, live_info),
            image_id=live_info.image_id or "",
            info=live_info,
            avd_info=live_info,
        )

    @property
    def golden_markers_path(self) -> Path:
        return Path(self._paths.data_dir or self._paths.root) / GOLDEN_MARKER_FILE

    def golden_marker(self, avd_name: str) -> dict[str, Any] | None:
        markers = self._read_golden_markers().get("snapshots", {})
        if not isinstance(markers, dict):
            return None
        payload = markers.get(avd_name)
        return payload if isinstance(payload, dict) else None

    def _read_golden_markers(self) -> dict[str, Any]:
        path = self.golden_markers_path
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            self._warn(f"could not read golden-snapshot markers: {exc}")
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            self._warn(f"golden-snapshot marker file {path} is not valid JSON; ignoring it")
            return {}
        return data if isinstance(data, dict) else {}

    def _write_golden_marker(self, avd_name: str, payload: dict[str, Any]) -> None:
        data = self._read_golden_markers()
        data["version"] = "1"
        snapshots = data.get("snapshots")
        if not isinstance(snapshots, dict):
            snapshots = {}
        snapshots[avd_name] = payload
        data["snapshots"] = snapshots
        try:
            write_text_atomic(self.golden_markers_path, json.dumps(data, indent=2, sort_keys=True) + "\n")
        except OSError as exc:
            self._warn(f"could not record the golden-snapshot marker: {exc}")

    def _golden_available(self, avd_name: str) -> bool:
        marker = self.golden_marker(avd_name)
        if marker is not None and marker.get("snapshot") == self._snapshot_name:
            return True
        snapshot = self._find_snapshot(avd_name, self._snapshot_name)
        return snapshot is not None

    def _find_snapshot(self, avd_name: str, snapshot_name: str) -> Snapshot | None:
        lister = getattr(self._runtime, "list_snapshots", None)
        if not callable(lister):
            return None
        try:
            snapshots = lister(avd_name) or []
        except Exception as exc:  # noqa: BLE001 - missing/locked emulator means none
            self._logger.debug("could not list snapshots for %s: %s", avd_name, exc)
            return None
        for candidate in snapshots:
            name = str(getattr(candidate, "name", "") or "")
            if name == snapshot_name:
                return candidate
        for candidate in snapshots:
            if bool(getattr(candidate, "is_golden", False)):
                return candidate
        return None

    # -- detection internals --------------------------------------------
    def _detection_record(
        self,
        avd_name: str,
        serial: str,
        *,
        image_id: str = "",
        info: AvdInfo | None = None,
        avd_info: AvdInfo | None = None,
    ) -> OobeRecord:
        detection = self._detector.detect(serial)
        screenshot_path, evidence = self._capture_evidence(avd_name, serial, image_id=image_id)
        evidence_path = self._write_evidence(avd_name, detection, image_id=image_id)
        if evidence_path:
            evidence = [*evidence, evidence_path]

        status = OobeStatus.FAILED
        notes = ""
        first_run_component = ""
        if detection.kind.value == "setup_wizard" and detection.activity:
            first_run_component = detection.activity
            if screenshot_path:
                status = OobeStatus.VERIFIED
                notes = f"setup wizard visible: {detection.activity}"
            else:
                notes = (
                    f"setup wizard visible ({detection.activity}) but screenshot evidence"
                    " could not be captured"
                )
        elif detection.kind.value == "launcher":
            notes = (
                f"launcher visible ({detection.activity or 'unknown'});"
                " first-run setup appears to be complete"
            )
        elif detection.activity:
            notes = f"neither the setup wizard nor a launcher is in the foreground: {detection.activity}"
        else:
            detail = "; ".join(detection.errors) if detection.errors else "no top activity was parsed"
            notes = f"could not determine the top activity: {detail}"
        if detection.errors and status is OobeStatus.VERIFIED:
            notes = _append_note(notes, "; ".join(detection.errors))

        api_level = detection.api_level or (getattr(info, "api_level", 0) if info else 0) or 0
        record = OobeRecord(
            image_id=image_id or (getattr(avd_info, "image_id", "") or "" if avd_info else ""),
            api_level=int(api_level or 0),
            tag="",
            abi=detection.abi or "",
            status=status,
            activity=detection.activity,
            first_run_component=first_run_component,
            screenshot_path=screenshot_path,
            boot_seconds=_uptime_seconds(info),
            checked_at=utcnow(),
            checked_by="avdfleet",
            emulator_version=self._emulator_version_value(),
            notes=notes,
            evidence=evidence,
        )
        return record

    def _merge_detection(
        self,
        record: OobeRecord,
        detected: OobeRecord,
        image: SystemImage,
        *,
        boot_seconds: float | None,
    ) -> OobeRecord:
        notes = detected.notes
        if image.sha256:
            notes = _append_note(notes, f"image sha256: {image.sha256}")
        if image.package:
            notes = _append_note(notes, f"image package: {image.package}")
        if boot_seconds is not None:
            notes = _append_note(notes, f"boot measured in {boot_seconds:.3f}s")
        return record.model_copy(
            update={
                "api_level": int(detected.api_level or image.api_level or 0),
                "tag": image.tag,
                "abi": image.abi or detected.abi,
                "status": detected.status,
                "activity": detected.activity,
                "first_run_component": detected.first_run_component,
                "screenshot_path": detected.screenshot_path,
                "boot_seconds": boot_seconds if boot_seconds is not None else detected.boot_seconds,
                "checked_at": utcnow(),
                "emulator_version": detected.emulator_version or record.emulator_version,
                "notes": notes,
                "evidence": list(detected.evidence),
            }
        )

    # -- evidence files --------------------------------------------------
    def _capture_evidence(
        self,
        avd_name: str,
        serial: str,
        *,
        image_id: str = "",
    ) -> tuple[str, list[str]]:
        try:
            data = self._capture_screenshot(avd_name, serial)
        except Exception as exc:  # noqa: BLE001 - missing evidence is a warning
            self._warn(f"OOBE screenshot capture failed for {avd_name}: {redact(str(exc))}")
            return "", []
        if not is_png(data):
            self._warn(f"OOBE screenshot for {avd_name} was not a PNG; discarding it")
            return "", []
        target = Path(self._paths.screenshots_dir) / screenshot_filename(avd_name, image_id)
        try:
            write_bytes_atomic(target, data)
        except OSError as exc:
            self._warn(f"could not save the OOBE screenshot to {target}: {exc}")
            return "", []
        return str(target), [str(target)]

    def _capture_screenshot(self, avd_name: str, serial: str) -> bytes:
        screen = self._screen
        if screen is not None:
            capture = screen if callable(screen) else getattr(screen, "capture", None)
            if not callable(capture):
                raise OobeError(
                    "the injected screen service does not expose capture(avd_name)",
                    hint="pass a ScreenClient or a callable accepting the AVD name",
                )
            data = capture(avd_name)
            if isinstance(data, (bytearray, memoryview)):
                data = bytes(data)
            if isinstance(data, bytes):
                return data
            raise OobeError("the injected screen service returned no image data")
        return self._detector.capture_screenshot(serial)

    def _write_evidence(
        self,
        avd_name: str,
        detection: Detection,
        *,
        image_id: str = "",
    ) -> str:
        stamp = utcnow().strftime("%Y%m%dT%H%M%S%f")
        slug = slugify(image_id or avd_name, max_length=48)
        target = Path(self._paths.artifacts_dir) / "oobe" / f"{slug}-{stamp}-detection.txt"
        try:
            write_text_atomic(target, detection.evidence_text(avd_name=avd_name, image_id=image_id))
        except OSError as exc:
            self._warn(f"could not write OOBE evidence file: {exc}")
            return ""
        return str(target)

    # -- instance helpers ------------------------------------------------
    def _safe_instance(self, avd_name: str) -> AvdInfo | None:
        try:
            return self._instance(avd_name)
        except (AvdFleetError, KeyError, IndexError):
            return None

    def _instance(self, avd_name: str) -> AvdInfo:
        name = str(avd_name or "").strip()
        if not name:
            raise ValidationError("an AVD name is required")
        status = getattr(self._runtime, "status", None)
        if callable(status):
            try:
                info = status(name)
                if info is not None:
                    return info
            except (KeyError, IndexError):
                pass
            except AvdFleetError:
                raise
        getter = getattr(self._avds, "get_instance", None)
        if callable(getter):
            try:
                info = getter(name)
            except (KeyError, IndexError) as exc:
                raise NotFoundError(f"instance {name!r} was not found") from exc
            if info is not None:
                return info
        raise NotFoundError(f"instance {name!r} was not found")

    def _serial_for(self, avd_name: str, info: AvdInfo | None = None) -> str:
        info = info or self._safe_instance(avd_name)
        serial = getattr(info, "adb_serial", None) if info is not None else None
        if not serial and info is not None:
            ports = getattr(info, "ports", None)
            console = getattr(ports, "console", None) if ports is not None else None
            if console:
                serial = f"emulator-{console}"
        if not serial:
            raise OobeError(
                f"cannot determine the adb serial for instance {avd_name!r}",
                hint="start the instance so runtime.status(name).adb_serial is populated",
            )
        return str(serial)

    def _start(self, avd_name: str) -> None:
        starter = getattr(self._runtime, "start", None)
        if not callable(starter):
            raise OobeError(
                "the runtime cannot start instances",
                hint="RuntimeManager.start(name) is required to verify OOBE images",
            )
        invoke_adaptive(starter, avd_name)

    def _stop(self, avd_name: str) -> None:
        stopper = getattr(self._runtime, "stop", None)
        if not callable(stopper):
            return
        # Prefer the graceful console/adb kill; the runtime escalates to
        # terminate/kill itself if the emulator ignores it.
        invoke_adaptive(stopper, avd_name, force=False, timeout_s=None)

    def _wait_boot(self, avd_name: str, timeout_s: float) -> None:
        waiter = getattr(self._runtime, "wait_boot", None)
        if not callable(waiter):
            raise OobeError(
                "the runtime cannot wait for boot completion",
                hint="RuntimeManager.wait_boot(name, timeout_s=...) is required",
            )
        invoke_adaptive(waiter, avd_name, timeout_s=timeout_s)

    # -- provisioning ----------------------------------------------------
    def _temp_avd_name(self, image: SystemImage) -> str:
        base = slugify(f"avdfleet-oobe-{image.id}", max_length=48)
        return f"{base}-{uuid.uuid4().hex[:6]}"

    def _provision(self, image: SystemImage, avd_name: str) -> AvdInfo:
        if self._provisioner is not None:
            result = invoke_adaptive(
                self._provisioner,
                image,
                avd_name,
                image_id=image.id,
                device_id=self._device_id,
            )
            info = result if isinstance(result, AvdInfo) else self._safe_instance(avd_name)
            if info is None:
                raise OobeError(
                    f"the injected provisioner did not create instance {avd_name!r}",
                    hint="the provisioner must return an AvdInfo or register the instance",
                )
            return info
        for attribute in _PROVISIONER_METHODS:
            method = getattr(self._avds, attribute, None)
            if not callable(method):
                continue
            result = invoke_adaptive(
                method,
                image_id=image.id,
                name=avd_name,
                device_id=self._device_id,
                config={},
                overrides={},
            )
            info = result if isinstance(result, AvdInfo) else self._safe_instance(avd_name)
            if info is None:
                raise OobeError(
                    f"the AvdManager did not register the temporary instance {avd_name!r}",
                    hint="return an AvdInfo from the create method or make get_instance find it",
                )
            return info
        raise OobeError(
            "cannot create a temporary AVD from a system image",
            hint=(
                "inject provisioner= or give the AvdManager a "
                "create_instance_from_image(image_id, name, device_id=...) method"
            ),
            details={"image_id": image.id, "avd_name": avd_name},
        )

    def _cleanup_temp_avd(self, avd_name: str, *, keep: bool) -> list[str]:
        if keep:
            return []
        problems: list[str] = []
        try:
            if is_active(self._safe_instance(avd_name)):
                self._stop(avd_name)
        except Exception as exc:  # noqa: BLE001 - cleanup is best effort
            problems.append(f"could not stop the temporary AVD: {redact(str(exc))}")
        deleter = getattr(self._avds, "delete_instance", None)
        if not callable(deleter):
            problems.append("the AvdManager does not implement delete_instance")
            return problems
        try:
            invoke_adaptive(deleter, avd_name, delete_files=True)
        except Exception as exc:  # noqa: BLE001 - cleanup is best effort
            problems.append(f"could not delete the temporary AVD: {redact(str(exc))}")
        return problems

    # -- persistence / events -------------------------------------------
    def _persist(self, record: OobeRecord) -> OobeRecord:
        try:
            stored = self._matrix.upsert(record)
        except AvdFleetError as exc:
            self._warn(f"could not persist the OOBE record: {exc}")
            return record
        self._emit(
            EventType.OOBE_RESULT,
            {
                "kind": "verify_image",
                "image_id": stored.image_id,
                "status": stored.status.value,
                "activity": stored.activity,
                "boot_seconds": stored.boot_seconds,
            },
        )
        return stored

    def _default_strategy(self, avd_name: str) -> OobeResetStrategy:
        return (
            OobeResetStrategy.SNAPSHOT
            if self._golden_available(avd_name)
            else OobeResetStrategy.WIPE
        )

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._events is None:
            return
        try:
            self._events.emit(event_type, payload)
        except Exception:  # pragma: no cover - event delivery must never break OOBE
            self._logger.debug("failed to publish OOBE event", exc_info=True)

    def _warn(self, message: str) -> None:
        if message not in self._warnings:
            self._warnings.append(message)
        self._logger.warning("%s", message)

    def _emulator_version_value(self) -> str:
        source = self._emulator_version_source
        if source is not None:
            try:
                value = source() if callable(source) else source
                if value:
                    return str(value)
            except Exception:  # noqa: BLE001 - metadata only
                self._logger.debug("emulator version provider failed", exc_info=True)
        for holder in (self._runtime, self._avds):
            candidate = getattr(holder, "emulator_version", None)
            if candidate is None:
                continue
            try:
                value = candidate() if callable(candidate) else candidate
            except Exception:  # noqa: BLE001 - metadata only
                continue
            if value:
                return str(value)
        return ""


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _paths_for_root(root: str | Path) -> Paths:
    root_path = Path(root).expanduser()
    return Paths(
        root=root_path,
        data_dir=root_path / "data",
        config_dir=root_path / "config",
        cache_dir=root_path / "cache",
        log_dir=root_path / "logs",
        sdk_root=root_path / "sdk",
        avd_home=root_path / "avd",
    )


def _uptime_seconds(info: AvdInfo | None) -> float | None:
    if info is None:
        return None
    uptime = getattr(info, "uptime_seconds", None)
    if uptime is None:
        return None
    try:
        return round(float(uptime), 6)
    except (TypeError, ValueError):
        return None


def _append_note(notes: str, extra: str) -> str:
    extra = (extra or "").strip()
    if not extra:
        return notes or ""
    return f"{notes}; {extra}" if notes else extra


def _with_cleanup_notes(notes: str, problems: Sequence[str], *, kept: bool) -> str:
    if problems:
        return _append_note(notes, f"cleanup problems: {'; '.join(problems)}")
    if not kept:
        return _append_note(notes, "temporary AVD removed")
    return _append_note(notes, "temporary AVD kept (keep_avd=True)")


def _as_golden_snapshot(value: Any, avd_name: str, snapshot_name: str) -> Snapshot:
    if isinstance(value, Snapshot):
        return value.model_copy(
            update={
                "name": value.name or snapshot_name,
                "avd_name": value.avd_name or avd_name,
                "is_golden": True,
                "created_at": value.created_at or utcnow(),
            }
        )
    return Snapshot(
        name=snapshot_name,
        avd_name=avd_name,
        created_at=utcnow(),
        is_golden=True,
    )


__all__ = [
    "DEFAULT_DEVICE_ID",
    "DEFAULT_TIMEOUT_S",
    "GOLDEN_MARKER_FILE",
    "OobeManager",
    "PACKAGE_CLEANUP_COMMANDS",
]
