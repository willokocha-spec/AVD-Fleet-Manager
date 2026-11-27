"""Unit tests for :mod:`avdfleet.scripts` (definitions, builtins, service).

The tests use real fake executables in a temporary ``PATH`` (a fake ``adb`` and a
fake ``python`` shim that delegates to the current interpreter) and an isolated
work directory, so no Android SDK, emulator, Appium or network is required.  A
real SQLite store is used to prove persistence wiring; the same service also has
to work with its in-memory fallback.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest

from avdfleet.core.errors import NotFoundError, ValidationError
from avdfleet.core.events import EventBus, EventType
from avdfleet.core.models import (
    RunState,
    ScriptDefinition,
    ScriptRunRequest,
    ScriptType,
)
from avdfleet.scripts import (
    BUILTIN_IDS,
    BUILTIN_SCREENSHOT_ID,
    CommandBuilder,
    InstanceTarget,
    ScriptService,
)
from avdfleet.store import Store

PYTHON = sys.executable

SERIALS = {
    "avd-01": "emulator-5554",
    "avd-02": "emulator-5556",
    "avd-03": "emulator-5558",
}

FAKE_ADB_SOURCE = dedent(
    """
    import json
    import os
    import sys
    import time

    args = sys.argv[1:]
    log_path = os.environ.get("FAKE_ADB_LOG")
    if log_path:
        payload = (json.dumps({"argv": args}) + "\\n").encode("utf-8")
        # Concurrent fan-out processes append to the same log on Windows.  A
        # single O_APPEND write avoids the per-process text-buffer race that
        # could silently drop one event.
        for _attempt in range(100):
            try:
                fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
                try:
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                break
            except OSError:
                time.sleep(0.01)

    serial = None
    rest = list(args)
    while rest:
        if rest[0] == "-s" and len(rest) >= 2:
            serial = rest[1]
            rest = rest[2:]
            continue
        if rest[0] in {"-e", "-d"}:
            rest = rest[1:]
            continue
        break

    command = rest[0] if rest else ""
    if command == "shell":
        text = " ".join(rest[1:])
        if text == "pm list packages":
            print("package:com.android.settings")
            print("package:com.example.test")
        elif text == "dumpsys activity activities":
            print(
                "mResumedActivity: ActivityRecord{1 u0 "
                "com.google.android.setupwizard/.SetupWizardActivity t1}"
            )
        elif text.startswith("echo "):
            print(text[len("echo "):])
        elif text == "getprop":
            print("[sys.boot_completed]: [1]")
            print("[ro.build.version.sdk]: [35]")
            print("[ro.serialno]: [%s]" % (serial or "unknown"))
        elif "getprop" in text:
            values = {
                "sys.boot_completed": "1",
                "ro.build.version.sdk": "35",
                "ro.serialno": serial or "unknown",
            }
            for chunk in text.split(";"):
                chunk = chunk.strip()
                if not chunk.startswith("getprop"):
                    continue
                name = chunk[len("getprop"):].strip()
                print(values.get(name, ""))
        elif text.startswith("exit "):
            sys.exit(int(text.split()[1]))
        else:
            print("fake-adb-shell: " + text)
    elif command == "exec-out":
        png = bytes([0x89, 0x50, 0x4E, 0x47, 13, 10, 26, 10]) + b"fake-png-payload"
        sys.stdout.buffer.write(png)
        sys.stdout.buffer.flush()
    elif command in {"forward", "reverse"}:
        print("ok")
    else:
        sys.stderr.write("fake adb: unsupported command " + " ".join(args) + "\\n")
    """
).strip()

SIMPLE_SCRIPT = ScriptDefinition(
    id="hello-adb",
    name="Hello adb",
    type=ScriptType.ADB_SHELL,
    content="echo hello-fleet",
    timeout_s=30,
)


def _resolver(name: str) -> InstanceTarget:
    if name not in SERIALS:
        raise NotFoundError(f"instance {name!r} not found")
    return InstanceTarget(name=name, serial=SERIALS[name])


def _write_shim(bin_dir: Path, name: str, script_parts: list[str], *, log_env: str | None = None) -> Path:
    """Create an executable shim for the current OS that delegates to Python."""
    if os.name == "nt":
        lines = ["@echo off"]
        if log_env:
            lines.append(f'if defined {log_env} echo %*>>"%{log_env}%"')
        lines.append(subprocess.list2cmdline([PYTHON, *script_parts]) + " %*")
        path = bin_dir / f"{name}.cmd"
        path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        return path
    lines = ["#!/bin/sh"]
    if log_env:
        lines.append(f'[ -n "${log_env}" ] && echo "$@" >> "${log_env}"')
    command = " ".join(shlex.quote(part) for part in [PYTHON, *script_parts])
    lines.append(f'exec {command} "$@"')
    path = bin_dir / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture()
def fake_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A fake ``adb`` script plus the ``(python, script)`` command prefix.

    Tests inject this prefix instead of installing a shim on ``PATH``: launching
    a ``.cmd`` shim on Windows costs seconds per invocation, so only the
    dedicated PATH-resolution test below pays that price.
    """
    adb_script = tmp_path / "fake_adb.py"
    adb_script.write_text(FAKE_ADB_SOURCE, encoding="utf-8")
    adb_log = tmp_path / "adb-events.jsonl"
    monkeypatch.setenv("FAKE_ADB_LOG", str(adb_log))
    return SimpleNamespace(
        adb_script=adb_script,
        adb_command=(PYTHON, str(adb_script)),
        adb_log=adb_log,
    )


def _service(tmp_path: Path, **kwargs) -> ScriptService:
    kwargs.setdefault("work_dir", tmp_path / "work")
    kwargs.setdefault("instance_resolver", _resolver)
    return ScriptService(**kwargs)


def _adb_service(tmp_path: Path, fake_tools: SimpleNamespace, **kwargs) -> ScriptService:
    kwargs.setdefault("adb_command", fake_tools.adb_command)
    return _service(tmp_path, **kwargs)


def _requests(tmp_path: Path, script: ScriptDefinition, **kwargs) -> ScriptRunRequest:
    return ScriptRunRequest(script_id=script.id, **kwargs)


def _wait_for(predicate, *, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# Library CRUD and built-ins
# ---------------------------------------------------------------------------


def test_builtin_library_is_loaded_with_stable_ids(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        scripts = service.list_scripts()
        by_id = {script.id: script for script in scripts}
        assert set(BUILTIN_IDS) <= set(by_id)
        for script_id in BUILTIN_IDS:
            assert by_id[script_id].builtin is True
            assert by_id[script_id].content
    finally:
        service.close()


def test_script_crud_roundtrip_and_missing_errors(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        saved = service.save_script(SIMPLE_SCRIPT)
        assert saved.id == SIMPLE_SCRIPT.id
        fetched = service.get_script(SIMPLE_SCRIPT.id)
        assert fetched.content == "echo hello-fleet"
        assert {script.id for script in service.list_scripts()} == set(BUILTIN_IDS) | {SIMPLE_SCRIPT.id}
        service.delete_script(SIMPLE_SCRIPT.id)
        with pytest.raises(NotFoundError):
            service.get_script(SIMPLE_SCRIPT.id)
        with pytest.raises(NotFoundError):
            service.delete_script(SIMPLE_SCRIPT.id)
    finally:
        service.close()


def test_save_script_validates_identity(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        bad = ScriptDefinition.model_construct(id="  ", name="bad", content="x")
        with pytest.raises(ValidationError):
            service.save_script(bad)
    finally:
        service.close()


def test_builtin_loader_can_overwrite_edited_builtins(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        original = service.get_script(BUILTIN_IDS[0])
        edited = original.model_copy(update={"timeout_s": 999})
        service.save_script(edited)
        assert service.get_script(BUILTIN_IDS[0]).timeout_s == 999
        service.load_builtins(overwrite=True)
        assert service.get_script(BUILTIN_IDS[0]).timeout_s == original.timeout_s
    finally:
        service.close()


# ---------------------------------------------------------------------------
# adb_shell execution
# ---------------------------------------------------------------------------


def test_adb_shell_fan_out_uses_serial_and_never_a_host_shell(
    tmp_path: Path, fake_tools: SimpleNamespace
) -> None:
    service = _adb_service(tmp_path, fake_tools)
    try:
        script = service.save_script(SIMPLE_SCRIPT)
        run = service.run(
            ScriptRunRequest(script_id=script.id, instances=["avd-01", "avd-02"], concurrency=1)
        )
        assert run.state is RunState.SUCCEEDED
        assert [result.instance for result in run.results] == ["avd-01", "avd-02"]
        assert all(result.exit_code == 0 for result in run.results)
        assert "hello-fleet" in run.results[0].stdout
        # Concurrent fake adb processes append their events; on Windows the
        # second file handle can still be flushing right after the run returns.
        assert _wait_for(
            lambda: len(fake_tools.adb_log.read_text(encoding="utf-8").splitlines()) >= 2,
            timeout=10.0,
        )
        events = [
            json.loads(line)
            for line in fake_tools.adb_log.read_text(encoding="utf-8").splitlines()
        ]
        argvs = [event["argv"] for event in events]
        assert ["-s", "emulator-5554", "shell", "echo hello-fleet"] in argvs
        assert ["-s", "emulator-5556", "shell", "echo hello-fleet"] in argvs

        marker = tmp_path / "injected.txt"
        injection = service.save_script(
            ScriptDefinition(
                id="inject",
                name="Injection probe",
                type=ScriptType.ADB_SHELL,
                content=f'$(touch "{marker}")',
            )
        )
        injection_run = service.run(ScriptRunRequest(script_id=injection.id, instances=["avd-01"]))
        assert injection_run.state is RunState.SUCCEEDED
        injected_argv = [event["argv"] for event in (
            json.loads(line)
            for line in fake_tools.adb_log.read_text(encoding="utf-8").splitlines()
        )][-1]
        assert marker.exists() is False
        assert injected_argv == [
            "-s",
            "emulator-5554",
            "shell",
            f'$(touch "{marker}")',
        ]
    finally:
        service.close()


def test_adb_shell_builtin_getprop_summary(tmp_path: Path, fake_tools: SimpleNamespace) -> None:
    service = _adb_service(tmp_path, fake_tools)
    try:
        run = service.run(
            ScriptRunRequest(script_id="builtin.getprop-summary", instances=["avd-01"])
        )
        assert run.state is RunState.SUCCEEDED
        assert "[ro.build.version.sdk]: [35]" in run.results[0].stdout
    finally:
        service.close()


def test_host_shell_inline_script(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        script = service.save_script(
            ScriptDefinition(
                id="inline",
                name="Inline shell",
                type=ScriptType.INLINE_SHELL,
                content="echo inline-ok",
            )
        )
        run = service.run(ScriptRunRequest(script_id=script.id, instances=["avd-01"]))
        assert run.state is RunState.SUCCEEDED
        assert "inline-ok" in run.results[0].stdout
    finally:
        service.close()


# ---------------------------------------------------------------------------
# Host Python / Appium Python / Appium JS
# ---------------------------------------------------------------------------


def test_host_python_uses_interpreter_env_and_temp_file(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        script = service.save_script(
            ScriptDefinition(
                id="env-python",
                name="Env python",
                type=ScriptType.HOST_PYTHON,
                content=(
                    "import os\n"
                    "print('instance=' + os.environ['AVDFLEET_INSTANCE'])\n"
                    "print('serial=' + os.environ['AVDFLEET_SERIAL'])\n"
                    "print('run=' + os.environ['AVDFLEET_RUN_ID'])\n"
                ),
            )
        )
        run = service.run(
            ScriptRunRequest(script_id=script.id, instances=["avd-01"], concurrency=1)
        )
        assert run.state is RunState.SUCCEEDED
        stdout = run.results[0].stdout
        assert "instance=avd-01" in stdout
        assert "serial=emulator-5554" in stdout
        assert f"run={run.id}" in stdout
        artifacts = list((tmp_path / "work").rglob("script_env-python.py"))
        assert artifacts
    finally:
        service.close()


def test_appium_python_sets_appium_environment(tmp_path: Path) -> None:
    service = _service(tmp_path, appium_url="http://127.0.0.1:4799/wd/hub")
    try:
        script = service.save_script(
            ScriptDefinition(
                id="appium-py",
                name="Appium python",
                type=ScriptType.APPIUM_PYTHON,
                content=(
                    "import os\n"
                    "print('url=' + os.environ['APPIUM_URL'])\n"
                    "print('udid=' + os.environ['UDID'])\n"
                    "print('appium_udid=' + os.environ['APPIUM_UDID'])\n"
                ),
            )
        )
        run = service.run(ScriptRunRequest(script_id=script.id, instances=["avd-02"]))
        assert run.state is RunState.SUCCEEDED
        stdout = run.results[0].stdout
        assert "url=http://127.0.0.1:4799/wd/hub" in stdout
        assert "udid=emulator-5556" in stdout
        assert "appium_udid=emulator-5556" in stdout
    finally:
        service.close()


def test_appium_js_without_node_fails_per_instance(tmp_path: Path) -> None:
    builder = CommandBuilder(which=lambda _name: None)
    service = _service(tmp_path, command_builder=builder)
    try:
        script = service.save_script(
            ScriptDefinition(
                id="appium-js",
                name="Appium js",
                type=ScriptType.APPIUM_JS,
                content="console.log('not run');",
            )
        )
        run = service.run(ScriptRunRequest(script_id=script.id, instances=["avd-01"]))
        assert run.state is RunState.FAILED
        assert run.results[0].state is RunState.FAILED
        assert "node executable not found" in run.results[0].error
    finally:
        service.close()


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_returns_planned_commands_without_executing(
    tmp_path: Path, fake_tools: SimpleNamespace
) -> None:
    service = _adb_service(tmp_path, fake_tools)
    try:
        script = service.save_script(SIMPLE_SCRIPT)
        run = service.run(
            ScriptRunRequest(script_id=script.id, instances=["avd-01", "avd-02"], dry_run=True)
        )
        assert run.dry_run is True
        assert run.state is RunState.SUCCEEDED
        planned = run.request["planned_commands"]
        assert set(planned) == {"avd-01", "avd-02"}
        assert "emulator-5554" in " ".join(planned["avd-01"])
        assert "adb" in " ".join(planned["avd-01"]).lower()
        assert "hello-fleet" in run.results[0].stdout
        assert fake_tools.adb_log.exists() is False
    finally:
        service.close()


# ---------------------------------------------------------------------------
# Failure, timeout, cancellation and aggregation
# ---------------------------------------------------------------------------


def test_timeout_marks_run_timed_out(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        script = service.save_script(
            ScriptDefinition(
                id="slow",
                name="Slow",
                type=ScriptType.HOST_PYTHON,
                content="import time\ntime.sleep(30)\nprint('never')\n",
                timeout_s=30,
            )
        )
        started = time.monotonic()
        run = service.run(
            ScriptRunRequest(script_id=script.id, instances=["avd-01"], timeout_s=1)
        )
        elapsed = time.monotonic() - started
        assert elapsed < 15.0
        assert run.state is RunState.TIMED_OUT
        assert run.results[0].state is RunState.TIMED_OUT
        assert run.results[0].exit_code is None
    finally:
        service.close()


def test_failed_exit_code_aggregates_to_failed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        script = service.save_script(
            ScriptDefinition(
                id="fail",
                name="Fail",
                type=ScriptType.HOST_PYTHON,
                content="import sys\nsys.exit(3)\n",
            )
        )
        run = service.run(
            ScriptRunRequest(script_id=script.id, instances=["avd-01", "avd-02"], concurrency=2)
        )
        assert run.state is RunState.FAILED
        assert run.succeeded == 0
        assert run.failed == 2
        assert all(result.state is RunState.FAILED for result in run.results)
        assert all(result.exit_code == 3 for result in run.results)
        assert run.error
    finally:
        service.close()


def test_mixed_results_count_as_failed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        script = service.save_script(
            ScriptDefinition(
                id="mixed",
                name="Mixed",
                type=ScriptType.HOST_PYTHON,
                content=(
                    "import os\n"
                    "import sys\n"
                    "sys.exit(0 if os.environ['AVDFLEET_INSTANCE'] == 'avd-01' else 5)\n"
                ),
            )
        )
        run = service.run(
            ScriptRunRequest(script_id=script.id, instances=["avd-01", "avd-02"], concurrency=2)
        )
        assert run.state is RunState.FAILED
        assert run.succeeded == 1
        assert run.failed == 1
        by_name = {result.instance: result for result in run.results}
        assert by_name["avd-01"].state is RunState.SUCCEEDED
        assert by_name["avd-02"].state is RunState.FAILED
    finally:
        service.close()


def test_cancel_running_submitted_run(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        service.save_script(
            ScriptDefinition(
                id="long",
                name="Long",
                type=ScriptType.HOST_PYTHON,
                content="import time\ntime.sleep(30)\n",
                timeout_s=60,
            )
        )
        run = service.submit(
            ScriptRunRequest(script_id="long", instances=["avd-01"], concurrency=1)
        )
        assert _wait_for(lambda: service.get_run(run.id).state is RunState.RUNNING)
        started = time.monotonic()
        service.cancel(run.id)
        assert _wait_for(lambda: service.get_run(run.id).state.terminal)
        final = service.get_run(run.id)
        assert final.state is RunState.CANCELLED
        assert time.monotonic() - started < 15.0
    finally:
        service.close()


def test_cancel_before_start_does_not_execute(
    tmp_path: Path, fake_tools: SimpleNamespace
) -> None:
    service = _service(tmp_path)
    try:
        script = service.save_script(SIMPLE_SCRIPT)
        request = ScriptRunRequest(script_id=script.id, instances=["avd-01"])
        run, prepared = service._prepare_run(request)  # noqa: SLF001 - deterministic pre-start state
        cancelled = service.cancel(run.id)
        assert cancelled.state is RunState.CANCELLED
        service._execute(run, prepared, request)  # noqa: SLF001 - must be a no-op after cancel
        assert service.get_run(run.id).state is RunState.CANCELLED
        assert fake_tools.adb_log.exists() is False
    finally:
        service.close()


def test_unknown_instance_fails_but_others_run(tmp_path: Path, fake_tools: SimpleNamespace) -> None:
    service = _adb_service(tmp_path, fake_tools)
    try:
        script = service.save_script(SIMPLE_SCRIPT)
        run = service.run(
            ScriptRunRequest(
                script_id=script.id,
                instances=["avd-01", "avd-99"],
                concurrency=2,
            )
        )
        assert run.state is RunState.FAILED
        by_name = {result.instance: result for result in run.results}
        assert by_name["avd-01"].state is RunState.SUCCEEDED
        assert by_name["avd-99"].state is RunState.FAILED
        assert "not found" in by_name["avd-99"].error
    finally:
        service.close()


# ---------------------------------------------------------------------------
# Events, hooks and lifecycle
# ---------------------------------------------------------------------------


def test_events_bus_and_on_event_listener(tmp_path: Path, fake_tools: SimpleNamespace) -> None:
    bus = EventBus()
    service = _adb_service(tmp_path, fake_tools, event_bus=bus)
    received: list[tuple[str, dict]] = []
    unsubscribe = service.on_event(lambda event_type, payload: received.append((event_type, payload)))
    try:
        script = service.save_script(SIMPLE_SCRIPT)
        run = service.run(ScriptRunRequest(script_id=script.id, instances=["avd-01"]))
        assert run.state is RunState.SUCCEEDED
        types = [event_type for event_type, _ in received]
        assert types[0] == EventType.SCRIPT_RUN_STARTED
        assert EventType.SCRIPT_RUN_OUTPUT in types
        assert types[-1] == EventType.SCRIPT_RUN_FINISHED
        output_payloads = [
            payload for event_type, payload in received if event_type == EventType.SCRIPT_RUN_OUTPUT
        ]
        assert output_payloads
        assert set(output_payloads[0]) >= {"run_id", "instance", "stream", "line"}
        assert output_payloads[0]["line"] == "hello-fleet"
        bus_types = [event.type for event in bus.history()]
        assert EventType.SCRIPT_RUN_STARTED in bus_types
        assert EventType.SCRIPT_RUN_FINISHED in bus_types
        unsubscribe()
        received.clear()
        service.run(ScriptRunRequest(script_id=script.id, instances=["avd-01"]))
        assert received == []
    finally:
        service.close()


def test_before_and_after_run_hooks(tmp_path: Path, fake_tools: SimpleNamespace) -> None:
    service = _adb_service(tmp_path, fake_tools)
    calls: list[tuple[str, str, RunState]] = []
    service.add_hook("before_run", lambda run: calls.append(("before", run.id, run.state)))
    service.add_hook("after_run", lambda run: calls.append(("after", run.id, run.state)))
    try:
        script = service.save_script(SIMPLE_SCRIPT)
        run = service.run(ScriptRunRequest(script_id=script.id, instances=["avd-01"]))
        assert calls[0][0] == "before"
        assert calls[0][2] is RunState.QUEUED
        assert calls[1][0] == "after"
        assert calls[1][2] is RunState.SUCCEEDED
        assert calls[0][1] == calls[1][1] == run.id
        with pytest.raises(ValidationError):
            service.add_hook("nope", lambda run: None)
    finally:
        service.close()


def test_close_cancels_active_runs(tmp_path: Path) -> None:
    service = _service(tmp_path, load_builtins_on_init=False)
    script = service.save_script(
        ScriptDefinition(
            id="long2",
            name="Long 2",
            type=ScriptType.HOST_PYTHON,
            content="import time\ntime.sleep(30)\n",
            timeout_s=60,
        )
    )
    run = service.submit(ScriptRunRequest(script_id=script.id, instances=["avd-01"], concurrency=1))
    assert _wait_for(lambda: service.get_run(run.id).state is RunState.RUNNING)
    service.close()
    assert service.get_run(run.id).state is RunState.CANCELLED


# ---------------------------------------------------------------------------
# Persistence with the real SQLite store
# ---------------------------------------------------------------------------


def test_sqlite_persistence_of_scripts_and_runs(tmp_path: Path, fake_tools: SimpleNamespace) -> None:
    store = Store(tmp_path / "avdfleet.db")
    store.initialize()
    try:
        service = ScriptService(
            store=store,
            work_dir=tmp_path / "work",
            instance_resolver=_resolver,
            adb_command=fake_tools.adb_command,
        )
        script = service.save_script(SIMPLE_SCRIPT)
        run = service.run(ScriptRunRequest(script_id=script.id, instances=["avd-01"]))
        assert run.state is RunState.SUCCEEDED

        reloaded = ScriptService(store=store, work_dir=tmp_path / "work2", instance_resolver=_resolver)
        loaded_run = reloaded.get_run(run.id)
        assert loaded_run.state is RunState.SUCCEEDED
        assert loaded_run.script_id == script.id
        assert [result.instance for result in loaded_run.results] == ["avd-01"]
        assert loaded_run.results[0].state is RunState.SUCCEEDED
        assert {item.id for item in reloaded.list_scripts()} >= set(BUILTIN_IDS) | {SIMPLE_SCRIPT.id}
        assert [item.id for item in reloaded.list_runs(limit=10)][0] == run.id
        reloaded.close()
        service.close()
    finally:
        store.close()


def test_run_secrets_are_redacted_in_persistence_and_output(
    tmp_path: Path, fake_tools: SimpleNamespace
) -> None:
    secret = "sk-live-this-must-never-appear"
    store = Store(tmp_path / "avdfleet.db")
    store.initialize()
    try:
        service = ScriptService(store=store, work_dir=tmp_path / "work", instance_resolver=_resolver)
        script = service.save_script(
            ScriptDefinition(
                id="secret",
                name="Secret",
                type=ScriptType.HOST_PYTHON,
                content="import os\nprint('API_KEY=' + os.environ['API_KEY'])\n",
                env={"API_KEY": secret},
            )
        )
        run = service.run(
            ScriptRunRequest(
                script_id=script.id,
                instances=["avd-01"],
                env={"PASSWORD": secret},
            )
        )
        assert run.state is RunState.SUCCEEDED
        assert secret not in run.results[0].stdout
        assert "***" in run.results[0].stdout
        persisted = service.get_run(run.id)
        serialized = persisted.model_dump_json()
        assert secret not in serialized
        assert "supersecret" not in serialized
        service.close()
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Built-in host_python entries (screenshot + token URL)
# ---------------------------------------------------------------------------


def test_screenshot_builtin_writes_png_artifact(
    tmp_path: Path, fake_tools: SimpleNamespace
) -> None:
    service = _adb_service(tmp_path, fake_tools)
    try:
        run = service.run(
            ScriptRunRequest(script_id=BUILTIN_SCREENSHOT_ID, instances=["avd-01"])
        )
        assert run.state is RunState.SUCCEEDED
        assert "screenshot:" in run.results[0].stdout
        pngs = list((tmp_path / "work").rglob("screenshot-*.png"))
        assert len(pngs) == 1
        assert pngs[0].read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        service.close()


class _TokenHandler(BaseHTTPRequestHandler):
    token_body = b'{"token": "abc123"}'

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        body = self.token_body
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # pragma: no cover - silence server logging
        return


def test_ping_proxy_token_builtin(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TokenHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/proxy/token"
    service = _service(tmp_path)
    try:
        run = service.run(
            ScriptRunRequest(
                script_id="builtin.ping-proxy-token",
                instances=["avd-01"],
                env={"AVDFLEET_TOKEN_URL": url},
            )
        )
        assert run.state is RunState.SUCCEEDED
        stdout = run.results[0].stdout
        assert "token-url" in stdout
        assert "200" in stdout
        assert "abc123" not in stdout
        assert "***" in stdout
    finally:
        service.close()
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Validation and result helpers
# ---------------------------------------------------------------------------


def test_request_validation_errors(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        script = service.save_script(SIMPLE_SCRIPT)
        with pytest.raises(ValidationError):
            service.run(ScriptRunRequest(script_id=script.id))
        with pytest.raises(ValidationError):
            service.run(ScriptRunRequest(script_id=script.id, instances=["avd-01"], timeout_s=0))
        with pytest.raises(NotFoundError):
            service.run(ScriptRunRequest(script_id="missing-script", instances=["avd-01"]))
        with pytest.raises(NotFoundError):
            service.get_run("missing-run")
        with pytest.raises(ValidationError):
            service.list_runs(limit=0)
        with pytest.raises(ValueError):
            ScriptRunRequest(script_id=script.id, concurrency=0)
        with pytest.raises(ValueError):
            ScriptRunRequest(script_id=script.id, concurrency=65)
        with pytest.raises(NotFoundError):
            service.cancel("missing-run")
    finally:
        service.close()


def test_fake_adb_and_python_executables_resolved_from_temporary_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_tools: SimpleNamespace
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_shim(bin_dir, "adb", [str(fake_tools.adb_script)])
    python_shim = _write_shim(bin_dir, "python", [], log_env="FAKE_PYTHON_LOG")
    python_log = tmp_path / "python-events.log"
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("FAKE_PYTHON_LOG", str(python_log))
    service = _service(tmp_path, python_command=(str(python_shim),))
    try:
        adb_script = service.save_script(SIMPLE_SCRIPT)
        adb_run = service.run(ScriptRunRequest(script_id=adb_script.id, instances=["avd-01"]))
        assert adb_run.state is RunState.SUCCEEDED
        assert "hello-fleet" in adb_run.results[0].stdout

        python_script = service.save_script(
            ScriptDefinition(
                id="path-python",
                name="Path python",
                type=ScriptType.HOST_PYTHON,
                content="print('python-from-path')\n",
            )
        )
        python_run = service.run(ScriptRunRequest(script_id=python_script.id, instances=["avd-01"]))
        assert python_run.state is RunState.SUCCEEDED
        assert "python-from-path" in python_run.results[0].stdout
        assert "-u" in python_log.read_text(encoding="utf-8")
    finally:
        service.close()


def test_service_implements_script_runner_protocol(tmp_path: Path) -> None:
    from avdfleet.core.interfaces import ScriptRunner

    service = _service(tmp_path)
    try:
        assert isinstance(service, ScriptRunner)
    finally:
        service.close()
