"""Base class for installer steps.

A step is a small, declarative unit of work with stable identity:

``id``, ``title``, ``description``, ``phase``, ``requires_reboot``,
``critical`` and ``depends_on`` are part of the install manifest/API contract
and must never be renamed (the GUI and ``--only`` depend on them).

``check(context)`` is a cheap, side-effect-free idempotence probe: return
``True`` when the step is already satisfied so the engine can skip it.
``run(context)`` performs the work by generating provider commands and running
them through the context runner.  A step that needs a reboot raises
:class:`avdfleet.core.errors.RebootRequiredError`; the engine records
``reboot_required`` and stops (it never reboots the host itself).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from avdfleet.core.errors import InstallerError, RebootRequiredError
from avdfleet.core.models import InstallStep, OperationResult

from ..providers.base import Command

if TYPE_CHECKING:
    from ..context import CommandResult, InstallContext


class BaseStep(ABC):
    """One unit of installer work."""

    id: str = ""
    title: str = ""
    description: str = ""
    phase: str = "core"
    requires_reboot: bool = False
    critical: bool = True
    depends_on: tuple[str, ...] = ()
    #: Informational steps (host probing) are exempt from the "already installed" no-op check.
    always_run: bool = False

    # -- metadata --------------------------------------------------------
    def spec(self) -> InstallStep:
        return InstallStep(
            id=self.id,
            title=self.title,
            description=self.description,
            phase=self.phase,
            requires_reboot=self.requires_reboot,
            critical=self.critical,
            depends_on=list(self.depends_on),
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.id!r}>"

    # -- contract --------------------------------------------------------
    @abstractmethod
    def check(self, context: InstallContext) -> bool:
        """Return ``True`` when the step's goal is already satisfied."""

    @abstractmethod
    def run(self, context: InstallContext) -> OperationResult:
        """Perform the step, returning a redaction-safe result."""

    def uninstall(self, context: InstallContext) -> OperationResult:
        """Undo what :meth:`run` created (safe default: nothing)."""
        return OperationResult(
            ok=True,
            message=f"nothing to remove for {self.id}",
            data={"step": self.id},
        )

    # -- result helpers --------------------------------------------------
    def done(self, message: str, **data: Any) -> OperationResult:
        return OperationResult(ok=True, message=message, data={"step": self.id, **data})

    def failed(self, message: str, *, hint: str = "", **data: Any) -> OperationResult:
        payload = {"step": self.id, **data}
        if hint:
            payload["hint"] = hint
        return OperationResult(ok=False, message=message, data=payload)

    def skipped(self, message: str, **data: Any) -> OperationResult:
        return OperationResult(ok=True, message=message, data={"step": self.id, "skipped": True, **data})

    # -- command helpers -------------------------------------------------
    def run_commands(
        self,
        context: InstallContext,
        commands: list[Command],
        *,
        what: str,
    ) -> tuple[bool, str, list[CommandResult]]:
        """Run *commands*; return ``(ok, message, results)`` without raising."""
        if not commands:
            return True, f"no commands required for {what}", []
        results = context.run_commands(commands, stop_on_error=False)
        failures = [result for result in results if not result.ok]
        if failures:
            first = failures[0]
            message = (
                f"{what} failed (exit {first.exit_code}): {first.binary if first.argv else 'command'}"
            )
            if first.output:
                message += f"\n{first.tail(800)}"
            return False, message, results
        return True, f"{what} completed ({len(results)} command(s))", results

    @staticmethod
    def require_admin(context: InstallContext, what: str) -> None:
        context.require_admin(what)

    def check_admin(self, context: InstallContext, what: str) -> OperationResult | None:
        """Return a failed result when admin rights are required but missing."""
        try:
            context.require_admin(what)
        except InstallerError as exc:
            return self.failed(str(exc), hint=exc.hint or "")
        return None

    def bootstrap_package_manager(self, context: InstallContext) -> OperationResult | None:
        """Ensure the native package manager exists; return a failure when it cannot be."""
        try:
            context.ensure_package_manager()
        except InstallerError as exc:
            return self.failed(str(exc), hint=exc.hint or "")
        return None

    @staticmethod
    def command_lines(results: list[CommandResult]) -> list[list[str]]:
        return [list(result.argv) for result in results]

    @staticmethod
    def first_line(text: str) -> str:
        for line in text.splitlines():
            line = line.strip()
            if line:
                return line
        return ""

    @staticmethod
    def version_major(text: str) -> int | None:
        match = re.search(r"(\d+)(?:\.\d+)*", text or "")
        return int(match.group(1)) if match else None

    @staticmethod
    def reboot_required(message: str, *, hint: str = "", step_id: str = "") -> RebootRequiredError:
        return RebootRequiredError(
            message,
            hint=hint or "reboot the host, then run: avdfleet install --resume",
            details={"pending_step": step_id or None},
        )


__all__ = ["BaseStep"]
