"""Emulator snapshot management (save / load / delete / list).

Snapshots are taken by the emulator itself.  There are two equivalent control
paths and the service tries them in order:

1. the emulator console (injected :data:`ConsoleFactory`; ``snapshot save/load/
   delete/list`` over the authenticated line protocol), then
2. ``adb -s emulator-<console> emu snapshot ...`` (injected
   :class:`~avdfleet.platform.runtime.boot.AdbRunner`).

Metadata (creation time, size/path if discoverable, and the *golden* marker used
by the OOBE reset strategy) is persisted in an atomic JSON index under
``Paths.snapshots_dir``; being able to list the golden snapshot after a daemon
restart is a hard requirement for OOBE.  A stopped emulator cannot answer either
control path, so ``list``/``delete`` fall back to the persisted index and
``save``/``load`` raise a clear :class:`InstanceError` telling the caller to
start the instance.

Names are validated before they reach a console/adb command: a newline would
otherwise inject an extra emulator console command.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from avdfleet.core.errors import InstanceError, NotFoundError, ToolError, ValidationError
from avdfleet.core.logging import get_logger, redact
from avdfleet.core.models import Snapshot, utcnow
from avdfleet.core.paths import Paths

logger = get_logger("platform.runtime.snapshots")

SNAPSHOT_INDEX_FILENAME = "snapshots.json"
SNAPSHOT_FORMAT_VERSION = "1"
GOLDEN_SNAPSHOT = "avdfleet-golden"
_SNAPSHOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


# ---------------------------------------------------------------------------
# Console protocol used for injection
# ---------------------------------------------------------------------------


@runtime_checkable
class EmulatorConsoleLike(Protocol):
    """Subset of ``platform.sdk.console.EmulatorConsole`` used for snapshots."""

    def connect(self, port: int, *, timeout_s: float = 5.0) -> None: ...

    def command(self, command: str) -> str: ...

    def close(self) -> None: ...


ConsoleFactory = Callable[[], EmulatorConsoleLike]


def default_console_factory() -> EmulatorConsoleLike:
    """Use the SDK emulator console implementation (imported lazily)."""
    from avdfleet.platform.sdk.console import EmulatorConsole

    return EmulatorConsole()


def validate_snapshot_name(name: str) -> str:
    candidate = (name or "").strip()
    if not candidate:
        raise ValidationError("snapshot name must not be empty")
    if not _SNAPSHOT_NAME_RE.match(candidate):
        raise ValidationError(
            f"invalid snapshot name {candidate!r}: use letters, digits, '.', '_' and '-'",
        )
    return candidate


# ---------------------------------------------------------------------------
# Persistent index
# ---------------------------------------------------------------------------


class SnapshotStore:
    """Atomic JSON index of snapshot metadata keyed by AVD name."""

    def __init__(self, paths: Paths | None, *, filename: str = SNAPSHOT_INDEX_FILENAME) -> None:
        self._paths = paths
        self._filename = filename
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        if self._paths is None:
            raise InstanceError(
                "snapshot metadata storage is not configured",
                hint="Wire Paths into the runtime service.",
            )
        return Path(self._paths.snapshots_dir) / self._filename

    # -- io -------------------------------------------------------------
    def _read(self) -> dict[str, dict[str, dict[str, Any]]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.warning("could not read snapshot index %s: %s", self.path, exc)
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("snapshot index %s is corrupt; treating it as empty", self.path)
            return {}
        snapshots = data.get("snapshots") if isinstance(data, dict) else None
        if not isinstance(snapshots, dict):
            return {}
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for avd, entries in snapshots.items():
            if isinstance(entries, dict):
                result[str(avd)] = {
                    str(name): dict(payload)
                    for name, payload in entries.items()
                    if isinstance(payload, dict)
                }
        return result

    def _write(self, data: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> None:
        target = self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SNAPSHOT_FORMAT_VERSION,
            "updated_at": utcnow().isoformat(),
            "snapshots": {
                avd: {name: dict(entry) for name, entry in entries.items()}
                for avd, entries in data.items()
            },
        }
        temporary = target.with_name(f"{target.name}.tmp-{os.getpid()}")
        try:
            temporary.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, target)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise InstanceError(
                f"could not persist snapshot metadata {target}: {exc}",
                hint="Check that the avdfleet data directory is writable.",
            ) from exc

    # -- crud -----------------------------------------------------------
    def list(self, avd_name: str) -> list[Snapshot]:
        with self._lock:
            entries = self._read().get(avd_name, {})
        snapshots: list[Snapshot] = []
        for name, payload in entries.items():
            try:
                snapshots.append(Snapshot.model_validate({**payload, "name": name, "avd_name": avd_name}))
            except Exception:  # noqa: BLE001 - one bad row must not hide others
                logger.warning("skipping invalid snapshot metadata for %s/%s", avd_name, name)
        return sorted(snapshots, key=lambda item: (item.created_at is None, item.created_at, item.name))

    def get(self, avd_name: str, name: str) -> Snapshot | None:
        for snapshot in self.list(avd_name):
            if snapshot.name == name:
                return snapshot
        return None

    def upsert(self, snapshot: Snapshot) -> Snapshot:
        with self._lock:
            data = self._read()
            entries = data.setdefault(snapshot.avd_name, {})
            if snapshot.is_golden:
                for name, existing in entries.items():
                    if name != snapshot.name:
                        existing["is_golden"] = False
            entries[snapshot.name] = snapshot.model_dump(mode="json")
            self._write(data)
        return snapshot

    def delete(self, avd_name: str, name: str) -> bool:
        with self._lock:
            data = self._read()
            entries = data.get(avd_name, {})
            if name not in entries:
                return False
            del entries[name]
            if not entries:
                data.pop(avd_name, None)
            self._write(data)
        return True

    def delete_avd(self, avd_name: str) -> None:
        with self._lock:
            data = self._read()
            if data.pop(avd_name, None) is not None:
                self._write(data)

    def golden(self, avd_name: str) -> Snapshot | None:
        for snapshot in self.list(avd_name):
            if snapshot.is_golden:
                return snapshot
        return None


# ---------------------------------------------------------------------------
# Snapshot service
# ---------------------------------------------------------------------------


@dataclass
class SnapshotTarget:
    """Where an instance's snapshot commands should be sent."""

    name: str
    console_port: int | None = None
    adb_serial: str | None = None
    avd_dir: Path | None = None
    running: bool = False


class SnapshotService:
    """Save/load/delete/list snapshots through the console or ``adb emu``."""

    def __init__(
        self,
        paths: Paths | None,
        *,
        console_factory: ConsoleFactory | None = None,
        adb: Any | None = None,
        clock: Callable[[], datetime] = utcnow,
        console_timeout_s: float = 10.0,
    ) -> None:
        self.store = SnapshotStore(paths)
        self.console_factory: ConsoleFactory = console_factory or default_console_factory
        self.adb = adb
        self._clock = clock
        self.console_timeout_s = float(console_timeout_s)

    # -- command plumbing ----------------------------------------------
    def _console_command(self, target: SnapshotTarget, command: str) -> str:
        if target.console_port is None:
            raise ToolError("the emulator console port is unknown for this instance")
        console = self.console_factory()
        try:
            console.connect(int(target.console_port), timeout_s=self.console_timeout_s)
            return console.command(command)
        finally:
            try:
                console.close()
            except Exception:  # noqa: BLE001 - cleanup only
                pass

    def _adb_command(self, target: SnapshotTarget, command: str) -> str:
        if self.adb is None or not target.adb_serial:
            raise ToolError("adb is not available for snapshot control")
        result = self.adb.emu(str(target.adb_serial), command, timeout_s=self.console_timeout_s)
        if not getattr(result, "ok", True):
            raise ToolError(
                f"adb emu {command!r} failed: {redact(getattr(result, 'error_text', 'unknown error'))}"
            )
        return str(getattr(result, "stdout", "") or "")

    def _command(self, target: SnapshotTarget, command: str) -> str:
        """Try the console, then ``adb emu``; raise when both fail."""
        errors: list[str] = []
        try:
            return self._console_command(target, command)
        except Exception as exc:  # noqa: BLE001 - fall back to adb
            errors.append(f"console: {redact(str(exc))}")
            logger.debug("snapshot console command %r failed: %s", command, exc)
        try:
            return self._adb_command(target, command)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"adb: {redact(str(exc))}")
            logger.debug("snapshot adb command %r failed: %s", command, exc)
        raise InstanceError(
            f"could not run emulator snapshot command {command!r}",
            hint="Check that the instance is running and its console/adb is reachable.",
            details={"errors": errors},
        )

    # -- helpers --------------------------------------------------------
    def _snapshot_path(self, target: SnapshotTarget, name: str) -> str | None:
        if target.avd_dir is None:
            return None
        candidate = Path(target.avd_dir) / "snapshots" / name
        return str(candidate) if candidate.exists() else None

    def _snapshot_size(self, target: SnapshotTarget, name: str) -> int | None:
        if target.avd_dir is None:
            return None
        directory = Path(target.avd_dir) / "snapshots" / name
        if not directory.is_dir():
            return None
        total = 0
        try:
            for child in directory.rglob("*"):
                if child.is_file():
                    total += child.stat().st_size
        except OSError:
            return None
        return total or None

    # -- public API -----------------------------------------------------
    def list(self, target: SnapshotTarget) -> list[Snapshot]:
        known = {snapshot.name: snapshot for snapshot in self.store.list(target.name)}
        remote: list[str] = []
        if target.running:
            try:
                output = self._command(target, "snapshot list")
                for line in str(output).splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.lower().startswith(("snapshot", "ok")):
                        continue
                    candidate = stripped.split()[-1] if stripped.split() else stripped
                    if _SNAPSHOT_NAME_RE.match(candidate):
                        remote.append(candidate)
            except InstanceError as exc:
                logger.debug("falling back to persisted snapshot list: %s", exc)
        for name in remote:
            if name not in known:
                known[name] = Snapshot(
                    name=name,
                    avd_name=target.name,
                    created_at=None,
                    size_bytes=self._snapshot_size(target, name),
                    path=self._snapshot_path(target, name),
                )
        return sorted(
            known.values(),
            key=lambda item: (item.created_at is None, item.created_at, item.name),
        )

    def save(
        self,
        target: SnapshotTarget,
        name: str,
        *,
        golden: bool = False,
    ) -> Snapshot:
        snapshot_name = validate_snapshot_name(name)
        if not target.running:
            raise InstanceError(
                f"cannot save snapshot {snapshot_name!r}: instance {target.name!r} is not running",
                hint="Start the instance and wait for boot before taking a snapshot.",
            )
        self._command(target, f"snapshot save {snapshot_name}")
        snapshot = Snapshot(
            name=snapshot_name,
            avd_name=target.name,
            created_at=self._clock(),
            size_bytes=self._snapshot_size(target, snapshot_name),
            path=self._snapshot_path(target, snapshot_name),
            is_golden=bool(golden),
        )
        self.store.upsert(snapshot)
        logger.info(
            "saved snapshot %s for %s%s",
            snapshot_name,
            target.name,
            " (golden)" if golden else "",
        )
        return snapshot

    def load(self, target: SnapshotTarget, name: str) -> Snapshot:
        snapshot_name = validate_snapshot_name(name)
        if not target.running:
            raise InstanceError(
                f"cannot load snapshot {snapshot_name!r}: instance {target.name!r} is not running",
                hint="Start the instance first; snapshot load is an emulator console command.",
            )
        existing = self.store.get(target.name, snapshot_name)
        if existing is None:
            # Unknown locally: still let the emulator answer, then remember it.
            self._command(target, f"snapshot load {snapshot_name}")
            snapshot = Snapshot(
                name=snapshot_name,
                avd_name=target.name,
                created_at=self._clock(),
                size_bytes=self._snapshot_size(target, snapshot_name),
                path=self._snapshot_path(target, snapshot_name),
            )
            return self.store.upsert(snapshot)
        self._command(target, f"snapshot load {snapshot_name}")
        return existing

    def delete(self, target: SnapshotTarget, name: str) -> None:
        snapshot_name = validate_snapshot_name(name)
        existing = self.store.get(target.name, snapshot_name)
        remote_error: Exception | None = None
        if target.running:
            try:
                self._command(target, f"snapshot delete {snapshot_name}")
            except InstanceError as exc:
                remote_error = exc
        removed = self.store.delete(target.name, snapshot_name)
        if not removed and remote_error is not None:
            raise NotFoundError(
                f"snapshot {snapshot_name!r} was not found for instance {target.name!r}"
            ) from remote_error
        if not removed and existing is None and not target.running:
            raise NotFoundError(
                f"snapshot {snapshot_name!r} was not found for instance {target.name!r}"
            )
        logger.info("deleted snapshot %s for %s", snapshot_name, target.name)

    def get(self, avd_name: str, name: str) -> Snapshot | None:
        return self.store.get(avd_name, name)

    def golden(self, avd_name: str) -> Snapshot | None:
        return self.store.golden(avd_name)


__all__ = [
    "ConsoleFactory",
    "EmulatorConsoleLike",
    "GOLDEN_SNAPSHOT",
    "SNAPSHOT_INDEX_FILENAME",
    "SnapshotService",
    "SnapshotStore",
    "SnapshotTarget",
    "default_console_factory",
    "validate_snapshot_name",
]
