"""Install manifest persistence.

The manifest is the installer's checkpoint file: it records the host, the SDK
root and the outcome of every step.  It is written *atomically* after each
step so that a reboot, a crash or `Ctrl-C` never leaves a half-written JSON
document behind.  `avdfleet install --resume` reads it to continue exactly
where the previous run stopped.

The on-disk location is ``Paths.manifest_path`` (``<data dir>/install-manifest.json``).
No secret is ever stored in the manifest: stdout/stderr are redacted before
they reach this module by the engine.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from avdfleet.core.errors import InstallerError
from avdfleet.core.models import InstallManifest, StepResult, StepStatus, utcnow

MANIFEST_VERSION = "1"
MANIFEST_FILENAME = "install-manifest.json"

#: Statuses that mean "this step will not be executed again".
DONE_STATUSES = frozenset({StepStatus.SUCCEEDED, StepStatus.SKIPPED})
#: Statuses that require the engine to run the step again.
RUNNABLE_STATUSES = frozenset(
    {StepStatus.PENDING, StepStatus.FAILED, StepStatus.REBOOT_REQUIRED, StepStatus.RUNNING}
)


def new_manifest(
    *,
    host: object | None = None,
    sdk_root: str = "",
    avdfleet_version: str = "",
) -> InstallManifest:
    """Create a fresh manifest with the required bookkeeping fields."""
    manifest = InstallManifest(version=MANIFEST_VERSION, avdfleet_version=avdfleet_version, sdk_root=sdk_root)
    if host is not None:
        manifest.host = host  # type: ignore[assignment]
    manifest.started_at = utcnow()
    manifest.updated_at = manifest.started_at
    return manifest


def atomic_write_json(path: str | Path, payload: object) -> Path:
    """Write *payload* as pretty JSON to *path* atomically (tmp + fsync + replace)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort cleanup
                pass
        raise
    return target


class ManifestStore:
    """Load/save/delete a single :class:`InstallManifest` at a fixed path."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> InstallManifest | None:
        """Return the persisted manifest, or ``None`` when absent/unreadable."""
        if not self.path.is_file():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        try:
            return InstallManifest.model_validate(raw)
        except Exception:  # pydantic ValidationError and friends
            return None

    def save(self, manifest: InstallManifest) -> InstallManifest:
        manifest.updated_at = utcnow()
        try:
            atomic_write_json(self.path, manifest.model_dump(mode="json"))
        except OSError as exc:  # pragma: no cover - disk errors are environmental
            raise InstallerError(
                f"could not write install manifest: {exc}",
                hint=f"check permissions for {self.path.parent}",
            ) from exc
        return manifest

    def delete(self) -> bool:
        """Remove the manifest file; return ``True`` when something was deleted."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True


def pending_step(manifest: InstallManifest | None) -> str | None:
    """Return the id of the step that paused installation for a reboot."""
    if manifest is None:
        return None
    for step_id, result in manifest.steps.items():
        if result.status is StepStatus.REBOOT_REQUIRED:
            return step_id
    return None


def first_incomplete_step(manifest: InstallManifest | None, step_ids: list[str]) -> str | None:
    """Return the first step id that is not done, honouring *step_ids* order."""
    if manifest is None:
        return step_ids[0] if step_ids else None
    for step_id in step_ids:
        result = manifest.steps.get(step_id)
        if result is None or result.status in RUNNABLE_STATUSES:
            return step_id
    return None


def record_step(
    manifest: InstallManifest,
    step_id: str,
    *,
    status: StepStatus,
    message: str = "",
    output: str = "",
    started_at: object | None = None,
    duration_s: float | None = None,
) -> StepResult:
    """Store/overwrite a step result in *manifest* and return it."""
    result = StepResult(
        step_id=step_id,
        status=status,
        message=message,
        output=output,
        started_at=started_at,  # type: ignore[arg-type]
        finished_at=utcnow() if status.terminal else None,
        duration_s=duration_s,
    )
    manifest.steps[step_id] = result
    return result


def summarize(manifest: InstallManifest) -> dict[str, int]:
    """Count step statuses; handy for CLI/API output and tests."""
    counts: dict[str, int] = {}
    for result in manifest.steps.values():
        key = result.status.value
        counts[key] = counts.get(key, 0) + 1
    return counts


def is_complete(manifest: InstallManifest | None) -> bool:
    return bool(manifest is not None and manifest.completed_at is not None)


__all__ = [
    "DONE_STATUSES",
    "MANIFEST_FILENAME",
    "MANIFEST_VERSION",
    "RUNNABLE_STATUSES",
    "ManifestStore",
    "atomic_write_json",
    "first_incomplete_step",
    "is_complete",
    "new_manifest",
    "pending_step",
    "record_step",
    "summarize",
]
