"""Wrapper around the legacy ``sdkmanager`` executable.

Only argv construction and result parsing live here; the backend decides what
a failure means for the caller (``OperationResult`` vs exception).  All
commands pass ``--sdk_root`` so a portable or per-project SDK root is honoured
even when the ambient ``ANDROID_SDK_ROOT`` points elsewhere.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from ..base import (
    INSTALL_TIMEOUT_S,
    LICENSE_PROMPT_REPLY,
    LICENSE_TIMEOUT_S,
    LIST_TIMEOUT_S,
    VERSION_TIMEOUT_S,
    Runner,
    SdkPackage,
    ToolResult,
    failed_command_error,
    parse_package_table,
    require_ok,
    run_tool,
)
from . import parsing

DEFAULT_SDKMANAGER_TIMEOUT_S = LIST_TIMEOUT_S


class SdkManager:
    """Command builder/executor for ``sdkmanager``."""

    def __init__(
        self,
        executable: str | Path,
        sdk_root: str | Path | None = None,
        *,
        runner: Runner | None = None,
        environ: Mapping[str, str] | None = None,
        timeout_s: float = DEFAULT_SDKMANAGER_TIMEOUT_S,
    ) -> None:
        self.executable = Path(executable)
        self.sdk_root = str(sdk_root) if sdk_root else ""
        self._runner: Runner = runner or run_tool
        self._environ = dict(environ or {})
        self.timeout_s = float(timeout_s)

    # -- process plumbing ----------------------------------------------
    def argv_for(self, *args: str) -> list[str]:
        """Full argv for ``args``, with ``--sdk_root`` first when configured."""
        argv = [str(self.executable)]
        if self.sdk_root:
            argv.append(f"--sdk_root={self.sdk_root}")
        argv.extend(str(arg) for arg in args)
        return argv

    def run(
        self,
        *args: str,
        stdin: str | None = None,
        timeout_s: float | None = None,
    ) -> ToolResult:
        return self._runner(
            self.executable,
            self.argv_for(*args)[1:],
            stdin=stdin,
            timeout_s=self.timeout_s if timeout_s is None else float(timeout_s),
            env=self._environ or None,
        )

    # -- commands -------------------------------------------------------
    def version(self, *, timeout_s: float = VERSION_TIMEOUT_S) -> str:
        """Return the sdkmanager version string (``11.0.0``)."""
        result = require_ok(self.run("--version", timeout_s=timeout_s), "sdkmanager --version")
        version = parsing.parse_sdkmanager_version(result.combined)
        if not version:
            raise failed_command_error(result, "sdkmanager --version returned no recognisable version")
        return version

    def list(
        self,
        *,
        installed_only: bool = False,
        timeout_s: float = LIST_TIMEOUT_S,
    ) -> tuple[list[SdkPackage], list[SdkPackage]]:
        """Return ``(installed, available)`` packages from ``--list``."""
        flag = "--list_installed" if installed_only else "--list"
        result = require_ok(self.run(flag, timeout_s=timeout_s), f"sdkmanager {flag}")
        return parse_package_table(result.combined)

    def install(
        self,
        package_ids: Sequence[str],
        *,
        accept_licenses: bool = False,
        timeout_s: float = INSTALL_TIMEOUT_S,
    ) -> ToolResult:
        """Install packages; pipe ``y`` answers when licenses may be prompted."""
        ids = _clean_ids(package_ids)
        stdin = LICENSE_PROMPT_REPLY if accept_licenses else None
        return self.run("--install", *ids, stdin=stdin, timeout_s=timeout_s)

    def uninstall(self, package_ids: Sequence[str], *, timeout_s: float = INSTALL_TIMEOUT_S) -> ToolResult:
        ids = _clean_ids(package_ids)
        return self.run("--uninstall", *ids, timeout_s=timeout_s)

    def accept_licenses(self, *, timeout_s: float = LICENSE_TIMEOUT_S) -> ToolResult:
        """Accept every SDK license by piping ``y`` to each prompt."""
        return self.run("--licenses", stdin=LICENSE_PROMPT_REPLY, timeout_s=timeout_s)


def _clean_ids(package_ids: Sequence[str]) -> list[str]:
    cleaned = [str(package_id).strip() for package_id in package_ids if str(package_id).strip()]
    if not cleaned:
        raise ValueError("at least one package id is required")
    return cleaned


__all__ = ["DEFAULT_SDKMANAGER_TIMEOUT_S", "SdkManager"]
