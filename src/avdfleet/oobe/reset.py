"""OOBE reset strategies.

The four strategies required by BRIEF §3.5 are implemented here, isolated from
the manager so the state enforcement and timing can be unit tested with a fake
runtime:

``snapshot``
    Load the golden ``avdfleet-golden`` snapshot (created by
    :meth:`OobeManager.ensure_golden_snapshot`).  Fastest and the preferred
    strategy; requires the golden snapshot to exist.
``wipe``
    Stop the instance and relaunch the emulator with ``-wipe-data``.
``package``
    Restore OOBE from inside the guest: clear the Setup Wizard package state and
    flip the ``user_setup_complete`` / ``device_provisioned`` settings back to 0.
``reboot``
    Restart the instance; used when the guest is already sitting on the first
    boot screen and only needs to be brought back up.

Every strategy measures wall-clock duration with an injectable clock, enforces
the running/stopped precondition of the underlying operation, and (unless
``verify=False``) re-detects the top activity afterwards.  Failures are reported
as :class:`~avdfleet.core.models.OobeResetResult` with ``ok=False`` rather than
escaping as exceptions; invalid arguments still raise the usual core errors.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from typing import Any

from ..core.errors import AvdFleetError, OobeError
from ..core.logging import get_logger, redact
from ..core.models import (
    AvdInfo,
    OobeRecord,
    OobeResetResult,
    OobeResetStrategy,
    OobeStatus,
    utcnow,
)

logger = get_logger("avdfleet.oobe.reset")

GOLDEN_SNAPSHOT = "avdfleet-golden"

PACKAGE_CLEANUP_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("pm", "clear", "com.google.android.setupwizard"),
    ("settings", "put", "secure", "user_setup_complete", "0"),
    ("settings", "put", "global", "device_provisioned", "0"),
    ("am", "force-stop", "com.google.android.setupwizard"),
    ("am", "start", "-n", "com.google.android.setupwizard/.SetupWizardActivity"),
)
"""Guest-side cleanup used by the ``package`` reset strategy."""

_RESET_STRATEGIES = tuple(strategy.value for strategy in OobeResetStrategy)


# ---------------------------------------------------------------------------
# Small adaptive helpers (shared with manager.py via import)
# ---------------------------------------------------------------------------


def accepts_keyword(function: Any, name: str) -> bool:
    """True when *function* accepts keyword *name* (or ``**kwargs``)."""
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):  # pragma: no cover - C callables
        return False
    if name in parameters:
        return True
    return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())


def invoke_adaptive(function: Any, *positional: Any, **kwargs: Any) -> Any:
    """Call *function* passing only keyword arguments it actually accepts."""
    if not callable(function):
        raise OobeError("expected a callable runtime operation", hint="check the injected runtime service")
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return function(*positional, **kwargs)
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return function(*positional, **kwargs)
    accepted = {
        name: value
        for name, value in kwargs.items()
        if name in parameters
        and parameters[name].kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return function(*positional, **accepted)


def parse_reset_strategy(value: str | OobeResetStrategy | None) -> OobeResetStrategy:
    """Validate a strategy string, raising ``ValidationError`` when unknown."""
    from ..core.errors import ValidationError

    if value is None:
        raise ValidationError(
            "an OOBE reset strategy is required",
            hint=f"choose one of: {', '.join(_RESET_STRATEGIES)}",
        )
    if isinstance(value, OobeResetStrategy):
        return value
    candidate = str(value).strip().lower()
    try:
        return OobeResetStrategy(candidate)
    except ValueError as exc:
        raise ValidationError(
            f"unknown OOBE reset strategy {value!r}",
            hint=f"choose one of: {', '.join(_RESET_STRATEGIES)}",
        ) from exc


def is_active(info: AvdInfo | None) -> bool:
    """True when an :class:`AvdInfo` describes a running/booting instance."""
    if info is None:
        return False
    state = getattr(info, "state", None)
    active = getattr(state, "active", None)
    if isinstance(active, bool):
        return active
    raw = getattr(state, "value", state)
    return str(raw or "").strip().lower() in {"starting", "booting", "running"}


# ---------------------------------------------------------------------------
# Reset engine
# ---------------------------------------------------------------------------


class ResetEngine:
    """Execute one OOBE reset strategy against an injected runtime.

    Parameters
    ----------
    runtime:
        Duck-typed ``RuntimeManager`` (core protocol).  Extra kwargs such as
        ``wipe_data`` are used when the concrete implementation supports them.
    detector:
        :class:`~avdfleet.oobe.detection.OobeDetector` used by the ``package``
        strategy; may be ``None`` if that strategy is never selected.
    serial_for:
        Callable returning the adb serial for an AVD name.
    golden_available:
        Callable telling whether a golden snapshot exists for an AVD.  Defaults
        to checking ``runtime.list_snapshots``.
    detect:
        Callable returning an :class:`OobeRecord` after the reset (used when
        ``verify=True``).  The manager passes its own ``detect`` method.
    clock:
        Injectable monotonic clock (tests pass a fake to assert timing).
    boot_timeout_s:
        Boot wait timeout forwarded to ``runtime.wait_boot``.
    """

    def __init__(
        self,
        runtime: Any,
        *,
        detector: Any | None = None,
        serial_for: Callable[[str], str] | None = None,
        golden_available: Callable[[str], bool] | None = None,
        snapshot_name: str = GOLDEN_SNAPSHOT,
        clock: Callable[[], float] = time.monotonic,
        boot_timeout_s: int | None = None,
        verify_attempts: int = 2,
        verify_interval_s: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        logger_: Any | None = None,
    ) -> None:
        if runtime is None:
            raise OobeError("an instance runtime is required for OOBE resets")
        self._runtime = runtime
        self._detector = detector
        self._serial_for = serial_for or (lambda name: "")
        self._golden_available = golden_available or self._default_golden_available
        self._snapshot_name = snapshot_name or GOLDEN_SNAPSHOT
        self._clock = clock
        self._boot_timeout_s = boot_timeout_s
        self._verify_attempts = max(1, int(verify_attempts))
        self._verify_interval_s = max(0.0, float(verify_interval_s))
        self._sleep = sleep
        self._logger = logger_ or logger

    # -- public ----------------------------------------------------------
    def run(
        self,
        avd_name: str,
        strategy: OobeResetStrategy,
        *,
        verify: bool = True,
        detect: Callable[[str], OobeRecord] | None = None,
    ) -> OobeResetResult:
        started = self._clock()
        failures: list[str] = []
        try:
            if strategy is OobeResetStrategy.SNAPSHOT:
                early = self._snapshot_reset(avd_name)
                if early is not None:
                    seconds = self._elapsed(started)
                    early.seconds = seconds
                    early.at = utcnow()
                    return early
            elif strategy is OobeResetStrategy.WIPE:
                self._wipe_reset(avd_name)
            elif strategy is OobeResetStrategy.PACKAGE:
                failures = self._package_reset(avd_name)
            elif strategy is OobeResetStrategy.REBOOT:
                self._reboot_reset(avd_name)
            else:  # pragma: no cover - parse_reset_strategy rejects unknown values
                raise OobeError(f"unsupported OOBE reset strategy {strategy!r}")
        except AvdFleetError as exc:
            return self._failure_result(avd_name, strategy, started, exc)
        except Exception as exc:  # noqa: BLE001 - reported through the result model
            self._logger.warning("OOBE reset %s failed for %s", strategy.value, avd_name, exc_info=True)
            return self._failure_result(avd_name, strategy, started, exc)

        activity = ""
        screenshot_path = ""
        ok = True
        message = f"{strategy.value} reset applied"
        if failures:
            ok = False
            message = f"{strategy.value} reset reported guest-side failures: {'; '.join(failures)}"

        if verify:
            if detect is None:
                ok = False
                message = f"{strategy.value} reset completed but no OOBE verifier was provided"
            else:
                try:
                    record = self._verify_with_retry(avd_name, detect)
                except Exception as exc:  # noqa: BLE001 - verification failure is reported
                    return self._failure_result(avd_name, strategy, started, exc)
                if record is None:
                    return OobeResetResult(
                        avd_name=avd_name,
                        strategy=strategy,
                        ok=False,
                        seconds=self._elapsed(started),
                        message=f"{strategy.value} reset completed but OOBE verification failed",
                        at=utcnow(),
                    )
                activity = record.activity or ""
                screenshot_path = record.screenshot_path or ""
                if record.status is OobeStatus.VERIFIED:
                    ok = True
                    message = "setup wizard is visible after reset"
                    if failures:
                        message = f"{message} (guest-side warnings: {'; '.join(failures)})"
                else:
                    ok = False
                    detail = activity or "no top activity"
                    message = f"reset completed but the top activity is not the setup wizard: {detail}"
                    if record.notes:
                        message = f"{message} ({record.notes})"

        return OobeResetResult(
            avd_name=avd_name,
            strategy=strategy,
            ok=ok,
            seconds=self._elapsed(started),
            activity=activity,
            screenshot_path=screenshot_path,
            message=message,
            at=utcnow(),
        )

    # -- verification ----------------------------------------------------
    def _verify_with_retry(
        self,
        avd_name: str,
        detect: Callable[[str], OobeRecord],
    ) -> OobeRecord | None:
        """Run the OOBE detector, retrying briefly for guest-side settle time.

        A freshly rebooted/package-reset guest can take a moment before the
        Setup Wizard reaches the foreground; without the retry a correct reset
        would be reported as failed.  The last record wins.
        """
        record: OobeRecord | None = None
        last_error: Exception | None = None
        for attempt in range(self._verify_attempts):
            try:
                record = detect(avd_name)
                last_error = None
            except Exception as exc:  # noqa: BLE001 - retried, re-raised when final
                last_error = exc
            if record is not None and record.status is OobeStatus.VERIFIED:
                return record
            if attempt + 1 < self._verify_attempts and self._verify_interval_s > 0:
                self._sleep(self._verify_interval_s)
        if last_error is not None and record is None:
            raise last_error
        return record

    # -- strategies ------------------------------------------------------
    def _snapshot_reset(self, avd_name: str) -> OobeResetResult | None:
        if not self._golden_available(avd_name):
            return OobeResetResult(
                avd_name=avd_name,
                strategy=OobeResetStrategy.SNAPSHOT,
                ok=False,
                message=(
                    f"golden snapshot {self._snapshot_name!r} is missing; "
                    "run ensure_golden_snapshot first"
                ),
                at=utcnow(),
            )
        self._ensure_running(avd_name)
        load = getattr(self._runtime, "load_snapshot", None)
        if not callable(load):
            raise OobeError(
                "the runtime cannot load snapshots",
                hint="RuntimeManager.load_snapshot(name, snapshot) is required for the snapshot strategy",
            )
        invoke_adaptive(load, avd_name, self._snapshot_name)
        self._wait_boot(avd_name, reason="snapshot restore")
        return None

    def _wipe_reset(self, avd_name: str) -> None:
        self._ensure_stopped(avd_name)
        self._start(avd_name, wipe_data=True)
        self._wait_boot(avd_name, reason="wipe-data boot")

    def _package_reset(self, avd_name: str) -> list[str]:
        detector = self._detector
        if detector is None:
            raise OobeError(
                "the package reset strategy needs an adb detector",
                hint="inject a command runner into OobeManager",
            )
        self._ensure_running(avd_name)
        serial = self._serial_for(avd_name)
        if not serial:
            raise OobeError(
                f"cannot determine the adb serial for instance {avd_name!r}",
                hint="ensure runtime.status(name).adb_serial is populated",
            )
        failures: list[str] = []
        for command in PACKAGE_CLEANUP_COMMANDS:
            result = detector.shell(serial, *command)
            if getattr(result, "ok", False):
                continue
            detail = str(getattr(result, "error_text", lambda: "")() or "").strip()
            failures.append(f"{' '.join(command)} (exit {getattr(result, 'returncode', '?')}{': ' + detail if detail else ''})")
        return failures

    def _reboot_reset(self, avd_name: str) -> None:
        self._ensure_running(avd_name)
        restart = getattr(self._runtime, "restart", None)
        if callable(restart):
            invoke_adaptive(restart, avd_name)
        else:
            self._stop(avd_name)
            self._start(avd_name)
        self._wait_boot(avd_name, reason="reboot")

    # -- state helpers ---------------------------------------------------
    def _status(self, avd_name: str) -> AvdInfo | None:
        status = getattr(self._runtime, "status", None)
        if callable(status):
            try:
                return status(avd_name)
            except Exception as exc:  # noqa: BLE001 - fall back to the running list
                self._logger.debug("runtime.status(%s) failed: %s", avd_name, exc)
        lister = getattr(self._runtime, "list_running", None)
        if callable(lister):
            for info in lister() or []:
                if getattr(info, "name", None) == avd_name:
                    return info
        return None

    def _ensure_running(self, avd_name: str) -> None:
        info = self._status(avd_name)
        if is_active(info):
            return
        self._start(avd_name)
        self._wait_boot(avd_name, reason="start")

    def _ensure_stopped(self, avd_name: str) -> None:
        info = self._status(avd_name)
        if info is None or not is_active(info):
            return
        self._stop(avd_name)

    def _start(self, avd_name: str, *, wipe_data: bool = False) -> None:
        start = getattr(self._runtime, "start", None)
        if not callable(start):
            raise OobeError(
                "the runtime cannot start instances",
                hint="RuntimeManager.start(name) is required for OOBE resets",
            )
        if not wipe_data:
            invoke_adaptive(start, avd_name)
            return
        if accepts_keyword(start, "wipe_data"):
            invoke_adaptive(start, avd_name, wipe_data=True)
            return
        for alternative in ("start_wipe", "start_with_wipe"):
            candidate = getattr(self._runtime, alternative, None)
            if callable(candidate):
                invoke_adaptive(candidate, avd_name)
                return
        raise OobeError(
            "the runtime does not support '-wipe-data' relaunch",
            hint="RuntimeManager.start(..., wipe_data=True) is required for the wipe reset strategy",
        )

    def _stop(self, avd_name: str) -> None:
        stop = getattr(self._runtime, "stop", None)
        if not callable(stop):
            raise OobeError(
                "the runtime cannot stop instances",
                hint="RuntimeManager.stop(name) is required for OOBE resets",
            )
        # Graceful first (console / adb emu kill); the runtime escalates to
        # terminate/kill itself when the emulator ignores the request.
        invoke_adaptive(stop, avd_name, force=False, timeout_s=None)

    def _wait_boot(self, avd_name: str, *, reason: str = "boot") -> None:
        waiter = getattr(self._runtime, "wait_boot", None)
        if not callable(waiter):
            self._logger.debug("runtime has no wait_boot; continuing after %s", reason)
            return
        invoke_adaptive(waiter, avd_name, timeout_s=self._boot_timeout_s)

    def _default_golden_available(self, avd_name: str) -> bool:
        lister = getattr(self._runtime, "list_snapshots", None)
        if not callable(lister):
            return False
        try:
            snapshots = lister(avd_name) or []
        except Exception as exc:  # noqa: BLE001 - treat unreadable state as missing
            self._logger.debug("could not list snapshots for %s: %s", avd_name, exc)
            return False
        for snapshot in snapshots:
            name = str(getattr(snapshot, "name", "") or "")
            if name == self._snapshot_name or bool(getattr(snapshot, "is_golden", False)):
                return True
        return False

    # -- misc ------------------------------------------------------------
    def _elapsed(self, started: float) -> float:
        return round(max(0.0, float(self._clock() - started)), 6)

    def _failure_result(
        self,
        avd_name: str,
        strategy: OobeResetStrategy,
        started: float,
        error: BaseException,
    ) -> OobeResetResult:
        message = redact(str(error)) or type(error).__name__
        self._logger.warning("OOBE reset %s failed for %s: %s", strategy.value, avd_name, message)
        return OobeResetResult(
            avd_name=avd_name,
            strategy=strategy,
            ok=False,
            seconds=self._elapsed(started),
            message=message,
            at=utcnow(),
        )


__all__ = [
    "GOLDEN_SNAPSHOT",
    "PACKAGE_CLEANUP_COMMANDS",
    "ResetEngine",
    "accepts_keyword",
    "invoke_adaptive",
    "is_active",
    "parse_reset_strategy",
]
