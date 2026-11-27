"""The installer engine: ordered steps, dry-run, checkpoint/resume, repair.

``InstallerEngine`` implements the core :class:`avdfleet.core.interfaces.Installer`
protocol.  A run is:

1. select the ordered step list (with ``--only`` dependency closure),
2. build one :class:`~.context.InstallContext` shared by all steps,
3. for each step: skip when ``check()`` says it is already satisfied, else run
   it, persist the checkpoint atomically, publish progress, and act on failure,
4. when every step is done, stamp ``completed_at`` and emit ``install.finished``.

Reboot semantics: a step that needs a reboot raises ``RebootRequiredError``.
The engine records ``reboot_required`` for it, writes the manifest with
``completed_at = null``, emits ``install.finished`` with the pending step and
raises the error again (the CLI maps it to exit code 3010).  :meth:`resume`
restarts at the pending step; the step's own ``check()`` normally reports it as
satisfied after the reboot, so the install continues without repeating work.

This module never reboots the host and never downloads anything itself.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from avdfleet.core.config import APP_VERSION, Config
from avdfleet.core.errors import InstallerError, RebootRequiredError, ValidationError
from avdfleet.core.events import EventBus, EventType
from avdfleet.core.logging import get_logger, redact, redact_text
from avdfleet.core.models import (
    HostInfo,
    InstallManifest,
    InstallOptions,
    InstallPlan,
    OperationResult,
    StepResult,
    StepStatus,
    utcnow,
)
from avdfleet.core.paths import Paths

from .context import CommandRunner, InstallContext
from .manifest import (
    ManifestStore,
    first_incomplete_step,
    new_manifest,
    pending_step,
    summarize,
)
from .providers import get_provider
from .providers.base import InstallProvider
from .steps import STEP_ALIASES, BaseStep, build_steps, validate_steps

#: Exit code the CLI returns when the install paused for a reboot.
REBOOT_REQUIRED_EXIT_CODE = 3010

ProgressCallback = Callable[[str, dict[str, Any]], None]


class InstallerEngine:
    """Step engine implementing the core ``Installer`` protocol."""

    def __init__(
        self,
        config: Config | None = None,
        paths: Paths | None = None,
        provider: InstallProvider | None = None,
        *,
        runner: CommandRunner | None = None,
        bus: EventBus | None = None,
        logger: Any | None = None,
        steps: Sequence[BaseStep] | None = None,
        sdk_backend: Any | None = None,
        host: HostInfo | None = None,
        on_event: ProgressCallback | None = None,
        provider_id: str | None = None,
        manifest_store: ManifestStore | None = None,
    ) -> None:
        self.config = config or Config.load()
        self.paths = paths or self.config.resolved_paths()
        self._provider = provider or get_provider(provider_id)
        self._provider_id = provider_id
        self._runner = runner
        self.bus = bus
        self.logger = logger or get_logger("installer")
        self._steps_override = list(steps) if steps is not None else None
        self._sdk_backend = sdk_backend
        self._host = host
        self._on_event = on_event
        self.store = manifest_store or ManifestStore(self.paths.manifest_path)
        #: Last manifest produced by install/resume/repair (available after failures).
        self.last_manifest: InstallManifest | None = None
        self._last_dry_run: bool = False

    # ------------------------------------------------------------------
    # Wiring helpers
    # ------------------------------------------------------------------
    @property
    def provider(self) -> InstallProvider:
        return self._provider

    @property
    def host(self) -> HostInfo:
        return self._host or self._provider.host_info()

    def _options(self, options: InstallOptions | Mapping[str, Any] | None) -> InstallOptions:
        if options is None:
            return InstallOptions()
        if isinstance(options, InstallOptions):
            return options
        return InstallOptions.model_validate(dict(options))

    def _provider_for(self, options: InstallOptions) -> InstallProvider:
        if options.platform_provider:
            resolved = get_provider(options.platform_provider)
            if resolved.id == self._provider.id:
                return self._provider
            return resolved
        return self._provider

    def _runner_for(self, options: InstallOptions) -> CommandRunner:
        if self._runner is not None:
            self._runner.dry_run = bool(options.dry_run)
            return self._runner
        return CommandRunner(dry_run=bool(options.dry_run))

    def _make_context(
        self,
        options: InstallOptions,
        *,
        provider: InstallProvider | None = None,
        manifest: InstallManifest | None = None,
        runner: CommandRunner | None = None,
    ) -> InstallContext:
        return InstallContext(
            config=self.config,
            paths=self.paths,
            provider=provider or self._provider_for(options),
            runner=runner or self._runner_for(options),
            options=options,
            logger=self.logger,
            bus=self.bus,
            manifest=manifest,
            sdk_backend=self._sdk_backend,
            host=self._host,
            emit=self._emit if (self._on_event is not None or self.bus is not None) else None,
        )

    # ------------------------------------------------------------------
    # Step selection
    # ------------------------------------------------------------------
    def _closure(self, steps: Sequence[BaseStep], only: Sequence[str]) -> list[BaseStep]:
        """Filter an explicit step list with ``--only`` semantics (deps included)."""
        available = {step.id: step for step in steps}
        wanted: list[str] = []
        unknown: list[str] = []
        for raw in only:
            token = (raw or "").strip().lower()
            if not token:
                continue
            alias = STEP_ALIASES.get(token, token)
            matches = [
                step.id
                for step in steps
                if step.id == alias or step.phase == alias or step.id.startswith(alias + ".")
            ]
            if matches:
                wanted.extend(matches)
            else:
                unknown.append(raw)
        if unknown:
            raise ValidationError(
                f"unknown installer step selection: {', '.join(unknown)}",
                hint=f"available steps: {', '.join(available)}",
            )
        closure: list[str] = []
        pending = list(reversed(wanted))
        while pending:
            step_id = pending.pop()
            if step_id in closure:
                continue
            step = available.get(step_id)
            if step is None:
                raise ValidationError(f"installer step {step_id!r} is not available")
            closure.append(step_id)
            pending.extend(reversed(step.depends_on))
        selected = set(closure)
        return [step for step in steps if step.id in selected]

    def _select_steps(self, options: InstallOptions) -> list[BaseStep]:
        if self._steps_override is not None:
            steps = list(self._steps_override)
            if options.skip_appium:
                steps = [step for step in steps if step.id != "tools.appium"]
            if options.skip_studio:
                steps = [step for step in steps if step.id != "studio.config"]
            if options.skip_scrcpy:
                steps = [step for step in steps if step.id != "tools.scrcpy"]
            if options.only:
                steps = self._closure(steps, options.only)
            validate_steps(steps)
            return steps
        return build_steps(
            only=options.only,
            skip_appium=options.skip_appium,
            skip_studio=options.skip_studio,
            skip_scrcpy=options.skip_scrcpy,
        )

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------
    def plan(self, options: InstallOptions | Mapping[str, Any] | None = None) -> InstallPlan:
        """Return the ordered plan and which steps are already satisfied.

        Read-only: no manifest is written, no command is executed (checks are
        pure provider/filesystem probes).
        """
        resolved = self._options(options)
        steps = self._select_steps(resolved)
        context = self._make_context(resolved)
        satisfied: dict[str, bool] = {}
        planned = []
        for step in steps:
            if step.always_run:
                satisfied[step.id] = False
            else:
                try:
                    satisfied[step.id] = bool(step.check(context))
                except Exception as exc:  # a broken probe means "not satisfied"
                    self.logger.debug("check failed for %s: %s", step.id, exc)
                    satisfied[step.id] = False
            planned.append(step.spec())
        required = [step for step in steps if not step.always_run]
        already_installed = all(satisfied[step.id] for step in required) if required else True
        requires_reboot = any(
            (not satisfied[step.id]) and step.requires_reboot for step in steps
        )
        return InstallPlan(
            host=context.host,
            steps=planned,
            dry_run=resolved.dry_run,
            already_installed=already_installed,
            requires_reboot=requires_reboot,
        )

    # ------------------------------------------------------------------
    # Install / resume / repair
    # ------------------------------------------------------------------
    def install(self, options: InstallOptions | Mapping[str, Any] | None = None) -> InstallManifest:
        resolved = self._options(options)
        if resolved.repair:
            return self._execute(resolved, mode="repair")
        return self._execute(resolved, mode="install")

    def resume(self, options: InstallOptions | Mapping[str, Any] | None = None) -> InstallManifest:
        return self._execute(self._options(options), mode="resume")

    def repair(self, options: InstallOptions | Mapping[str, Any] | None = None) -> InstallManifest:
        return self._execute(self._options(options), mode="repair")

    def status(self) -> InstallManifest | None:
        """Return the persisted manifest (``None`` when no install has run)."""
        return self.store.load()

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _execute(self, options: InstallOptions, *, mode: str) -> InstallManifest:
        steps = self._select_steps(options)
        provider = self._provider_for(options)
        runner = self._runner_for(options)
        context = self._make_context(options, provider=provider, runner=runner)
        self._last_dry_run = context.dry_run
        existing = self.store.load()
        selected_ids = [step.id for step in steps]

        # Fast no-op: a completed manifest whose checks all still pass.
        if (
            mode == "install"
            and existing is not None
            and existing.completed_at is not None
            and not options.only
        ):
            context.manifest = existing
            if self._all_satisfied(steps, context):
                self.last_manifest = existing
                if not context.dry_run:
                    self.store.save(existing)
                self._emit_finished(existing, ok=True, already_installed=True)
                return existing

        if mode == "resume":
            if existing is None:
                raise InstallerError(
                    "there is no install to resume",
                    hint="run 'avdfleet install' first",
                    details={"manifest_path": str(self.store.path)},
                )
            if existing.completed_at is not None:
                self.last_manifest = existing
                self._emit_finished(existing, ok=True, already_installed=True)
                return existing
            start_id = pending_step(existing) or first_incomplete_step(existing, selected_ids)
            if start_id is None:
                start_index = len(steps)
            elif start_id not in selected_ids:
                raise ValidationError(
                    f"pending step {start_id!r} is not part of this install selection",
                    hint="resume without --only, or include the pending step",
                )
            else:
                start_index = selected_ids.index(start_id)
            manifest = existing
            manifest.resumed_count += 1
            manifest.completed_at = None
        elif mode == "repair":
            manifest = existing or new_manifest(
                host=context.host,
                sdk_root=str(context.sdk_root),
                avdfleet_version=APP_VERSION,
            )
            manifest.completed_at = None
            if existing is not None:
                manifest.resumed_count += 1
            start_index = 0
        else:  # install (fresh or continuing an interrupted run)
            if existing is not None and existing.completed_at is None:
                start_id = pending_step(existing) or first_incomplete_step(existing, selected_ids)
                start_index = selected_ids.index(start_id) if start_id in selected_ids else 0
                manifest = existing
                manifest.resumed_count += 1
                manifest.completed_at = None
            else:
                manifest = new_manifest(
                    host=context.host,
                    sdk_root=str(context.sdk_root),
                    avdfleet_version=APP_VERSION,
                )
                start_index = 0

        manifest.host = context.host
        manifest.sdk_root = str(context.sdk_root)
        context.manifest = manifest
        self.last_manifest = manifest
        self._checkpoint(manifest, context)
        self._emit(
            EventType.INSTALL_STEP,
            {
                "status": "started",
                "mode": mode,
                "dry_run": context.dry_run,
                "total": len(steps),
                "start_step": selected_ids[start_index] if steps else None,
                "steps": selected_ids,
            },
        )

        total = len(steps)
        for index, step in enumerate(steps):
            if index < start_index:
                continue
            result = self._run_step(step, context, index, total)
            manifest.steps[step.id] = result
            self._checkpoint(manifest, context)
            self._emit_step(step, result, index, total)

            if result.status is StepStatus.REBOOT_REQUIRED:
                manifest.completed_at = None
                self._checkpoint(manifest, context)
                self._emit_finished(manifest, ok=False, pending_step=step.id, mode=mode)
                error = RebootRequiredError(
                    f"a reboot is required to continue after step {step.id!r}",
                    hint="reboot the host, then run 'avdfleet install --resume'",
                    details={"pending_step": step.id, "manifest": manifest.model_dump(mode="json")},
                )
                error.manifest = manifest  # type: ignore[attr-defined]
                raise error
            if result.status is StepStatus.FAILED and step.critical:
                manifest.completed_at = None
                self._checkpoint(manifest, context)
                hint = result.output or ""
                self._emit_finished(manifest, ok=False, failed_step=step.id, mode=mode)
                failure_error = InstallerError(
                    f"install step {step.id!r} failed: {result.message}",
                    hint=hint or "fix the reported problem, then run 'avdfleet install --repair'",
                    details={"step_id": step.id, "manifest": manifest.model_dump(mode="json")},
                )
                failure_error.manifest = manifest  # type: ignore[attr-defined]
                raise failure_error

        manifest.completed_at = utcnow()
        self._checkpoint(manifest, context)
        self._emit_finished(manifest, ok=True, mode=mode)
        return manifest

    def _all_satisfied(self, steps: Sequence[BaseStep], context: InstallContext) -> bool:
        for step in steps:
            if step.always_run:
                continue
            try:
                if not step.check(context):
                    return False
            except Exception:
                return False
        return True

    def _run_step(
        self,
        step: BaseStep,
        context: InstallContext,
        index: int,
        total: int,
    ) -> StepResult:
        started_at = utcnow()
        started_perf = time.perf_counter()
        self._emit(
            EventType.INSTALL_STEP,
            {
                "step_id": step.id,
                "title": step.title,
                "phase": step.phase,
                "status": StepStatus.RUNNING.value,
                "index": index,
                "total": total,
                "dry_run": context.dry_run,
            },
        )

        def finish(status: StepStatus, message: str, output: str = "") -> StepResult:
            return StepResult(
                step_id=step.id,
                status=status,
                message=redact_text(message),
                output=redact_text(output),
                started_at=started_at,
                finished_at=utcnow(),
                duration_s=round(time.perf_counter() - started_perf, 3),
            )

        if not step.always_run:
            try:
                satisfied = bool(step.check(context))
            except Exception as exc:
                self.logger.debug("check failed for %s: %s", step.id, exc)
                satisfied = False
            if satisfied:
                return finish(StepStatus.SKIPPED, "already satisfied; nothing to do")

        try:
            outcome = step.run(context)
        except RebootRequiredError as exc:
            return finish(StepStatus.REBOOT_REQUIRED, str(exc))
        except InstallerError as exc:
            hint = getattr(exc, "hint", "") or ""
            return finish(StepStatus.FAILED, f"{exc}" + (f" ({hint})" if hint else ""))
        except Exception as exc:  # defensive: a step bug must not kill the run
            self.logger.exception("installer step %s crashed", step.id)
            return finish(StepStatus.FAILED, f"{type(exc).__name__}: {exc}")
        status = StepStatus.SUCCEEDED if outcome.ok else StepStatus.FAILED
        output = ""
        if isinstance(outcome.data, dict):
            output = str(outcome.data.get("output") or "")
            hint = str(outcome.data.get("hint") or "")
            if not outcome.ok and hint:
                output = hint
        return finish(status, outcome.message, output)

    # ------------------------------------------------------------------
    # Uninstall
    # ------------------------------------------------------------------
    def uninstall(self, *, dry_run: bool = False, keep_data: bool = False) -> OperationResult:
        """Remove installer-managed files/environment (never the system packages)."""
        options = InstallOptions(dry_run=dry_run)
        context = self._make_context(options, provider=self._provider, runner=self._runner_for(options))
        manifest = self.store.load()
        context.manifest = manifest
        steps = list(self._steps_override) if self._steps_override is not None else build_steps()

        removed: list[str] = []
        kept: list[str] = []
        commands: list[list[str]] = []
        errors: list[str] = []
        for step in steps:
            try:
                outcome = step.uninstall(context)
            except Exception as exc:
                errors.append(f"{step.id}: {type(exc).__name__}: {exc}")
                continue
            data = outcome.data if isinstance(outcome.data, dict) else {}
            step_commands = data.get("commands") or []
            if isinstance(step_commands, list):
                commands.extend([list(map(str, command)) for command in step_commands])
            if data.get("removed") and data.get("path"):
                removed.append(str(data["path"]))

        managed_sdk = self._is_managed(context.sdk_root)
        managed_avd = self._is_managed(context.avd_home)
        if keep_data:
            kept.extend([str(context.sdk_root), str(context.avd_home), str(self.paths.data_dir)])
        else:
            for folder in (context.sdk_root, context.avd_home, self.paths.tools_dir):
                if not self._is_managed(folder):
                    kept.append(str(folder))
                    continue
                if dry_run:
                    removed.append(f"would remove {folder}")
                    continue
                if Path(folder).exists():
                    shutil.rmtree(folder, ignore_errors=True)
                    removed.append(str(folder))

        manifest_path = str(self.store.path)
        if dry_run:
            if self.store.exists():
                removed.append(f"would remove {manifest_path}")
        elif self.store.delete():
            removed.append(manifest_path)

        return OperationResult(
            ok=not errors,
            message=(
                "uninstall dry-run: no changes were made"
                if dry_run
                else "installer-managed files and environment removed"
            ),
            data={
                "dry_run": dry_run,
                "keep_data": keep_data,
                "removed": removed,
                "kept": kept,
                "commands": commands,
                "managed_sdk": managed_sdk,
                "managed_avd": managed_avd,
                "errors": errors,
                "manual": [
                    "system packages (JDK/Node/Appium/scrcpy) are left installed; remove them with your package manager",
                    "the avdfleet data directory (database, logs) is kept",
                ],
                "manifest_path": manifest_path,
            },
        )

    def _is_managed(self, path: str | Path) -> bool:
        """True when *path* belongs to the avdfleet data dir (or portable root)."""
        try:
            resolved = Path(path).expanduser().resolve()
        except OSError:
            return False
        roots = [Path(self.paths.data_dir or self.paths.root).expanduser().resolve()]
        if self.paths.portable:
            roots.append(Path(self.paths.root).expanduser().resolve())
        for root in roots:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            return True
        return False

    # ------------------------------------------------------------------
    # Checkpoint / events
    # ------------------------------------------------------------------
    def _checkpoint(self, manifest: InstallManifest, context: InstallContext) -> None:
        if context.dry_run:
            return
        self.store.save(manifest)

    def _emit_step(self, step: BaseStep, result: StepResult, index: int, total: int) -> None:
        payload = {
            "step_id": step.id,
            "title": step.title,
            "description": step.description,
            "phase": step.phase,
            "status": result.status.value,
            "message": result.message,
            "critical": step.critical,
            "requires_reboot": step.requires_reboot,
            "index": index,
            "total": total,
            "duration_s": result.duration_s,
        }
        self._emit(EventType.INSTALL_STEP, payload)
        if result.status is StepStatus.FAILED:
            self.logger.warning("install step failed: %s: %s", step.id, result.message)
        else:
            self.logger.info("install step %s: %s", step.id, result.status.value)

    def _emit_finished(self, manifest: InstallManifest, *, ok: bool, **extra: Any) -> None:
        payload = {
            "ok": ok,
            "completed_at": manifest.completed_at.isoformat() if manifest.completed_at else None,
            "resumed_count": manifest.resumed_count,
            "summary": summarize(manifest),
            "dry_run": self._last_dry_run,
            **extra,
        }
        self._emit(EventType.INSTALL_FINISHED, payload)

    def _emit(self, event_type: str, payload: Mapping[str, Any] | None = None) -> None:
        data = redact(dict(payload or {}))
        assert isinstance(data, dict)
        if self._on_event is not None:
            try:
                self._on_event(event_type, data)
            except Exception:  # pragma: no cover - callbacks must not break installs
                self.logger.exception("install progress callback failed")
        if self.bus is not None:
            try:
                self.bus.emit(event_type, data, source="installer")
            except Exception:  # pragma: no cover - bus must never break installs
                self.logger.exception("install event bus publish failed")


def create_engine(
    config: Config | None = None,
    paths: Paths | None = None,
    **kwargs: Any,
) -> InstallerEngine:
    """Convenience constructor used by the CLI/container wiring."""
    resolved_config = config or Config.load()
    return InstallerEngine(resolved_config, paths or resolved_config.resolved_paths(), **kwargs)


__all__ = ["REBOOT_REQUIRED_EXIT_CODE", "InstallerEngine", "ProgressCallback", "create_engine"]
