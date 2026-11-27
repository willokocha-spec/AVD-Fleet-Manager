"""Command building for every script type.

One :class:`CommandBuilder` turns a :class:`~avdfleet.core.models.ScriptDefinition`
plus a resolved :class:`~avdfleet.scripts.types.InstanceTarget` into concrete
:class:`~avdfleet.scripts.types.PlannedCommand` objects:

============================  =================================================
``adb_shell``                 ``adb -s <serial> shell <content>`` (argv list,
                              never a host shell, so adb arguments cannot inject
                              commands into the daemon host)
``inline_shell``              host shell (``/bin/sh -c`` or ``cmd.exe /c``)
``host_python``               content/path written to a per-instance temp file
                              and executed with the interpreter used by the
                              daemon (:data:`sys.executable` by default)
``appium_python``             host Python plus ``APPIUM_URL``/``UDID`` env
``appium_js``                 Node.js (when installed) on a temp ``.js`` file
                              plus the same Appium environment

Interpreter resolution is injectable (``adb_command``/``python_command``/
``node_command`` or a custom ``which``) so the unit tests run with fake
executables in a temporary ``PATH`` and never require Android, Python-from-PATH
or Node to be installed.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from ..core.errors import ScriptError
from ..core.models import ScriptDefinition, ScriptType
from .types import InstanceTarget, PlannedCommand, ScriptContext

__all__ = ["CommandBuilder", "resolve_command"]

Which = Callable[[str], str | None]

_SAFE_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_INSTANCE_PLACEHOLDERS = frozenset(
    {"AVDFLEET_INSTANCE", "AVDFLEET_SERIAL", "AVDFLEET_UDID", "AVDFLEET_RUN_ID", "AVDFLEET_SCRIPT_ID"}
)
DEFAULT_APPIUM_URL = "http://127.0.0.1:4723/wd/hub"


def resolve_command(value: Sequence[str] | str | Path | None) -> tuple[str, ...] | None:
    """Normalise an injected executable specification to an argv prefix."""
    if value is None:
        return None
    if isinstance(value, (str, Path)):
        text = str(value)
        if not text:
            return None
        return (text,)
    argv = tuple(str(part) for part in value)
    return argv or None


class CommandBuilder:
    """Build :class:`PlannedCommand` objects for a script/target pair."""

    def __init__(
        self,
        *,
        adb_command: Sequence[str] | str | Path | None = None,
        python_command: Sequence[str] | str | Path | None = None,
        node_command: Sequence[str] | str | Path | None = None,
        which: Which | None = None,
        base_env: Mapping[str, str] | None = None,
        appium_url: str | None = None,
        appium_url_resolver: Callable[[InstanceTarget], str] | None = None,
        host_shell: bool = True,
    ) -> None:
        self._adb_command = resolve_command(adb_command)
        self._python_command = resolve_command(python_command)
        self._node_command = resolve_command(node_command)
        self._which: Which = which or shutil.which
        self._base_env = dict(base_env or {})
        self._appium_url = appium_url or DEFAULT_APPIUM_URL
        self._appium_url_resolver = appium_url_resolver
        self._host_shell = host_shell
        self._is_windows = os.name == "nt"

    # -- interpreter / tool resolution ------------------------------------
    def resolve_adb(self) -> tuple[str, ...]:
        command = self._adb_command or self._resolve_from_path("adb")
        if not command:
            raise ScriptError(
                "adb executable not found",
                hint="install Android platform-tools or add it to PATH",
            )
        return command

    def try_resolve_adb(self) -> tuple[str, ...] | None:
        try:
            return self.resolve_adb()
        except ScriptError:
            return None

    def resolve_python(self) -> tuple[str, ...]:
        if self._python_command:
            return self._python_command
        executable = sys.executable
        if not executable:
            resolved = self._resolve_from_path("python") or self._resolve_from_path("python3")
            if not resolved:  # pragma: no cover - running Python guarantees sys.executable
                raise ScriptError("no Python interpreter available for host scripts")
            return resolved
        return (executable,)

    def resolve_node(self) -> tuple[str, ...]:
        command = self._node_command or self._resolve_from_path("node")
        if not command:
            raise ScriptError(
                "node executable not found",
                hint="install Node.js 22+ (required by Appium 4) to run appium_js scripts",
            )
        return command

    def _resolve_from_path(self, name: str) -> tuple[str, ...] | None:
        found = self._which(name)
        return (str(found),) if found else None

    # -- planning ---------------------------------------------------------
    def build(
        self,
        script: ScriptDefinition,
        target: InstanceTarget,
        *,
        context: ScriptContext,
    ) -> list[PlannedCommand]:
        """Return the commands to run for *target*; raises :class:`ScriptError`."""
        output_dir = self._ensure_output_dir(context.work_dir)
        env = self._environment(script, target, context, output_dir)
        if script.type is ScriptType.ADB_SHELL:
            return [self._adb_shell(script, target, env)]
        if script.type is ScriptType.INLINE_SHELL:
            return [self._inline_shell(script, env)]
        if script.type is ScriptType.HOST_PYTHON:
            return [self._python_script(script, target, env, context, output_dir, appium=False)]
        if script.type is ScriptType.APPIUM_PYTHON:
            return [self._python_script(script, target, env, context, output_dir, appium=True)]
        if script.type is ScriptType.APPIUM_JS:
            return [self._js_script(script, target, env, context, output_dir)]
        raise ScriptError(f"unsupported script type: {script.type!r}")

    # -- environment ------------------------------------------------------
    def _environment(
        self,
        script: ScriptDefinition,
        target: InstanceTarget,
        context: ScriptContext,
        output_dir: Path,
    ) -> dict[str, str]:
        env: dict[str, str] = dict(os.environ)
        env.update(self._base_env)
        adb = self.try_resolve_adb()
        env.update(
            {
                "AVDFLEET_RUN_ID": context.run_id,
                "AVDFLEET_SCRIPT_ID": script.id,
                "AVDFLEET_SCRIPT_NAME": script.name,
                "AVDFLEET_INSTANCE": target.name,
                "AVDFLEET_SERIAL": target.serial,
                "AVDFLEET_UDID": target.serial,
                "AVDFLEET_OUTPUT_DIR": str(output_dir),
                "AVDFLEET_WORK_DIR": str(output_dir),
            }
        )
        if adb:
            env["AVDFLEET_ADB"] = adb[0]
            if len(adb) > 1:
                env["AVDFLEET_ADB_COMMAND"] = json.dumps(list(adb))
        env.update(script.env)
        env.update(context.request_env)
        env.update(target.env)
        # Target-internal identity must always win so a script cannot run
        # against the wrong emulator by overriding AVDFLEET_SERIAL.
        env["AVDFLEET_INSTANCE"] = target.name
        env["AVDFLEET_SERIAL"] = target.serial
        env["AVDFLEET_UDID"] = target.serial
        env["AVDFLEET_OUTPUT_DIR"] = str(output_dir)
        env["AVDFLEET_WORK_DIR"] = str(output_dir)
        return env

    def _appium_env(self, target: InstanceTarget) -> dict[str, str]:
        url = self._appium_url_resolver(target) if self._appium_url_resolver is not None else self._appium_url
        return {
            "APPIUM_URL": url,
            "UDID": target.serial,
            "APPIUM_UDID": target.serial,
            "DEVICE_NAME": target.name,
            "AVDFLEET_APPIUM_URL": url,
        }

    # -- individual script types -----------------------------------------
    def _adb_shell(
        self,
        script: ScriptDefinition,
        target: InstanceTarget,
        env: dict[str, str],
    ) -> PlannedCommand:
        adb = self.resolve_adb()
        body = script.content.strip()
        if not body and script.path:
            local = Path(script.path).expanduser()
            if local.is_file():
                body = local.read_text(encoding="utf-8")
            else:
                # Remote path: execute an existing shell script on the device.
                argv = (*adb, "-s", target.serial, "shell", "sh", script.path)
                return PlannedCommand(
                    argv=argv,
                    env=env,
                    display=self._display(argv),
                    cwd=None,
                )
        if not body:
            raise ScriptError(f"script {script.id!r} has no adb shell content")
        command = _expand_placeholders(body, env, quote=False, names=_INSTANCE_PLACEHOLDERS)
        argv = (*adb, "-s", target.serial, "shell", command)
        return PlannedCommand(argv=argv, env=env, display=self._display(argv))

    def _inline_shell(self, script: ScriptDefinition, env: dict[str, str]) -> PlannedCommand:
        body = self._body(script)
        command = _expand_placeholders(body, env, quote=True, names=None)
        display = f"shell: {command}"
        if not self._host_shell:  # pragma: no cover - diagnostic escape hatch
            raise ScriptError("host shell execution is disabled")
        return PlannedCommand(argv=(command,), env=env, shell=True, display=display)

    def _python_script(
        self,
        script: ScriptDefinition,
        target: InstanceTarget,
        env: dict[str, str],
        context: ScriptContext,
        output_dir: Path,
        *,
        appium: bool,
    ) -> PlannedCommand:
        body = self._body(script)
        python = self.resolve_python()
        if appium:
            env.update(self._appium_env(target))
        path = output_dir / f"script_{_safe_name(script.id)}.py"
        path.write_text(body, encoding="utf-8")
        argv = (*python, "-u", str(path))
        return PlannedCommand(
            argv=argv,
            env=env,
            cwd=str(output_dir),
            display=self._display(argv),
        )

    def _js_script(
        self,
        script: ScriptDefinition,
        target: InstanceTarget,
        env: dict[str, str],
        context: ScriptContext,
        output_dir: Path,
    ) -> PlannedCommand:
        body = self._body(script)
        node = self.resolve_node()
        env.update(self._appium_env(target))
        path = output_dir / f"script_{_safe_name(script.id)}.js"
        path.write_text(body, encoding="utf-8")
        argv = (*node, str(path))
        return PlannedCommand(
            argv=argv,
            env=env,
            cwd=str(output_dir),
            display=self._display(argv),
        )

    # -- helpers ----------------------------------------------------------
    def _body(self, script: ScriptDefinition) -> str:
        if script.content and script.content.strip():
            return script.content
        if script.path:
            source = Path(script.path).expanduser()
            if not source.is_file():
                raise ScriptError(
                    f"script file not found: {script.path}",
                    hint="provide inline content or an existing local file path",
                )
            return source.read_text(encoding="utf-8")
        raise ScriptError(f"script {script.id!r} has no content or path")

    @staticmethod
    def _ensure_output_dir(work_dir: Path) -> Path:
        work_dir.mkdir(parents=True, exist_ok=True)
        return work_dir

    def _display(self, argv: Sequence[str]) -> str:
        if self._is_windows:
            return subprocess.list2cmdline([str(part) for part in argv])
        return shlex.join([str(part) for part in argv])


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._") or "script"


def _expand_placeholders(
    text: str,
    env: Mapping[str, str],
    *,
    quote: bool,
    names: frozenset[str] | None,
) -> str:
    """Substitute ``${NAME}`` placeholders, optionally shell-quoting values."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if names is not None and name not in names:
            return match.group(0)
        if name not in env:
            return match.group(0)
        value = env[name]
        if not quote:
            return value
        if os.name == "nt":
            return '"' + value.replace('"', '\\"') + '"' if _needs_windows_quoting(value) else value
        return shlex.quote(value)

    return _SAFE_PLACEHOLDER.sub(replace, text)


def _needs_windows_quoting(value: str) -> bool:
    return any(char in value for char in ' \t"&|<>^')
