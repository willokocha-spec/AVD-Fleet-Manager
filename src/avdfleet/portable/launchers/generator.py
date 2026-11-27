"""Launcher generation and the bundled Python launch helper.

The shell/batch launchers are thin wrappers around ``python -m
avdfleet.portable.launchers.generator start``, which implements everything the scripts
cannot do portably:

1. resolve and validate the bundle root,
2. build the portable environment (``env.builder``),
3. detect a moved bundle and run relocation,
4. run the fast preflight checks and refuse FAT32/noexec volumes,
5. start ``avdfleet serve --portable`` with the environment,
6. poll ``/health`` and only then open the browser,
7. keep the console alive while the daemon runs and pause/show a dialog on error.

All side effects are injectable (spawn/probe/opener/clock/sleep) so tests run
without starting a server or opening a window.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from avdfleet.core.config import APP_VERSION
from avdfleet.core.errors import PortableError
from avdfleet.core.ports import is_port_free
from avdfleet.portable.env.builder import (
    build_environment,
    detect_root_change,
    discover_root,
    find_python_executable,
    save_root_state,
)
from avdfleet.portable.env.model import PortableEnvironment, host_os_name

from .templates import (
    DEFAULT_PORT,
    HEALTH_PATH,
    launcher_for_host,
    validate_launcher,
)

USAGE = f"""AVD Fleet portable launcher

Usage: avdfleet.portable.launchers.generator start [options] [-- extra daemon options]

Options:
  --port <n>      daemon port to use (default {DEFAULT_PORT}; a free port is chosen if busy)
  --no-browser    do not open the web UI after the health check
  --timeout <s>   seconds to wait for the daemon health check (default 120)
  --no-relocate   do not rewrite AVD/config paths when the bundle moved
  --no-preflight  skip the preflight checks (filesystem/noexec/space)
  -h, --help      show this help

The launcher starts: avdfleet serve --portable
"""

#: Exit codes are documented so the .cmd/.sh wrappers can propagate them.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MISSING_RUNTIME = 3
EXIT_PREFLIGHT = 4
EXIT_SERVER = 5
EXIT_INTERRUPTED = 130


class LauncherUsageError(PortableError):
    """Bad launcher arguments (mapped to exit code 2)."""


@dataclass(frozen=True)
class LaunchOptions:
    root: Path | None = None
    port: int | None = None
    no_browser: bool = False
    timeout_s: float = 120.0
    relocate: bool = True
    preflight: bool = True
    extra_args: tuple[str, ...] = ()
    help: bool = False


@dataclass(frozen=True)
class LaunchPlan:
    root: Path
    environment: PortableEnvironment
    argv: list[str]
    env: dict[str, str]
    port: int
    health_url: str
    browser_url: str
    no_browser: bool = False


@dataclass(frozen=True)
class HealthWait:
    healthy: bool
    attempts: int
    elapsed_s: float
    last_error: str = ""


def parse_launcher_args(argv: Sequence[str]) -> LaunchOptions:
    """Parse launcher options; unknown arguments are forwarded to ``serve``."""
    args = list(argv)
    if args and args[0] == "start":
        args = args[1:]
    port: int | None = None
    no_browser = False
    relocate = True
    preflight = True
    timeout_s = 120.0
    extra: list[str] = []
    index = 0
    while index < len(args):
        argument = args[index]
        if argument == "--":
            extra.extend(args[index + 1 :])
            break
        if argument in {"-h", "--help"}:
            return LaunchOptions(help=True)
        if argument == "--no-browser":
            no_browser = True
            index += 1
            continue
        if argument == "--no-relocate":
            relocate = False
            index += 1
            continue
        if argument == "--no-preflight":
            preflight = False
            index += 1
            continue
        if argument == "--port" or argument.startswith("--port="):
            if argument.startswith("--port="):
                value = argument.split("=", 1)[1]
                index += 1
            else:
                if index + 1 >= len(args):
                    raise LauncherUsageError("--port requires a value")
                value = args[index + 1]
                index += 2
            try:
                port = int(value)
            except ValueError as exc:
                raise LauncherUsageError(f"invalid --port value {value!r}") from exc
            if not 1 <= port <= 65535:
                raise LauncherUsageError(f"--port must be between 1 and 65535, got {port}")
            continue
        if argument == "--timeout" or argument.startswith("--timeout="):
            if argument.startswith("--timeout="):
                value = argument.split("=", 1)[1]
                index += 1
            else:
                if index + 1 >= len(args):
                    raise LauncherUsageError("--timeout requires a value")
                value = args[index + 1]
                index += 2
            try:
                timeout_s = float(value)
            except ValueError as exc:
                raise LauncherUsageError(f"invalid --timeout value {value!r}") from exc
            if timeout_s <= 0:
                raise LauncherUsageError("--timeout must be positive")
            continue
        if argument.startswith("-"):
            raise LauncherUsageError(f"unknown launcher option {argument!r}")
        extra.append(argument)
        index += 1
    return LaunchOptions(
        port=port,
        no_browser=no_browser,
        timeout_s=timeout_s,
        relocate=relocate,
        preflight=preflight,
        extra_args=tuple(extra),
    )


def choose_port(
    preferred: int = DEFAULT_PORT,
    *,
    is_free: Callable[[int], bool] | None = None,
    attempts: int = 50,
) -> int:
    """Return *preferred* when free, else the next free port."""
    checker = is_free or (lambda port: is_port_free(port))
    for offset in range(attempts):
        candidate = preferred + offset
        if candidate > 65535:
            break
        if candidate < 1:
            continue
        if checker(candidate):
            return candidate
    raise PortableError(
        f"No free TCP port found near {preferred}",
        hint="Close the process using the port or pass --port <free-port>.",
    )


def serve_command(
    environment: PortableEnvironment,
    *,
    python_executable: str | Path | None = None,
    host_os: str | None = None,
) -> list[str]:
    """Resolve the command that starts the daemon (console script or ``-m``)."""
    paths = environment.paths
    host = host_os or environment.host_os
    console = (
        paths.venv / "Scripts" / "avdfleet.exe"
        if host == "windows"
        else paths.venv / "bin" / "avdfleet"
    )
    if console.is_file():
        return [str(console)]
    if python_executable:
        return [str(python_executable), "-m", "avdfleet"]
    bundled = find_python_executable(paths, host_os=host)
    if bundled is not None:
        return [str(bundled), "-m", "avdfleet"]
    return [sys.executable, "-m", "avdfleet"]


def build_launch_plan(
    root: str | Path,
    *,
    port: int | None = None,
    no_browser: bool = False,
    timeout_s: float = 120.0,
    extra_args: Sequence[str] = (),
    host_os: str | None = None,
    base_env: dict[str, str] | None = None,
    python_executable: str | Path | None = None,
    is_free: Callable[[int], bool] | None = None,
) -> LaunchPlan:
    """Build the daemon argv/env/URLs without starting anything."""
    root_path = Path(root).expanduser().resolve()
    environment = build_environment(root_path, host_os=host_os, base_env=base_env)
    chosen = choose_port(
        port if port is not None else DEFAULT_PORT,
        is_free=is_free,
    )
    command = serve_command(environment, python_executable=python_executable, host_os=host_os)
    argv = [*command, "serve", "--portable", "--port", str(chosen), *extra_args]
    host = "127.0.0.1"
    return LaunchPlan(
        root=root_path,
        environment=environment,
        argv=argv,
        env=dict(environment.env),
        port=chosen,
        health_url=f"http://{host}:{chosen}{HEALTH_PATH}",
        browser_url=f"http://{host}:{chosen}/",
        no_browser=no_browser,
    )


def http_health_probe(url: str, *, timeout_s: float = 2.0) -> tuple[bool, str]:
    """Default health probe: HTTP GET that must return 200."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:  # noqa: S310 - loopback URL
            status = getattr(response, "status", 200)
            if status == 200:
                return True, ""
            return False, f"HTTP {status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, str(exc)


def wait_for_health(
    url: str,
    *,
    timeout_s: float = 120.0,
    interval_s: float = 0.5,
    probe: Callable[[str], tuple[bool, str] | bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> HealthWait:
    """Poll the health endpoint until it answers or the timeout expires."""
    checker = probe or http_health_probe
    started = clock()
    attempts = 0
    last_error = ""
    max_attempts = max(1, int(timeout_s / max(interval_s, 0.01)) + 2)
    while attempts < max_attempts:
        attempts += 1
        try:
            result = checker(url)
        except Exception as exc:  # pragma: no cover - injectable probes should not raise
            result = (False, str(exc))
        healthy, error = (result, "") if isinstance(result, bool) else result
        if healthy:
            return HealthWait(True, attempts, clock() - started)
        last_error = error
        elapsed = clock() - started
        if elapsed >= timeout_s:
            break
        sleep(min(interval_s, max(0.0, timeout_s - elapsed)))
    return HealthWait(False, attempts, clock() - started, last_error)


def default_spawn(plan: LaunchPlan) -> Any:
    """Start the daemon process with inherited stdio and the portable env."""
    try:
        return subprocess.Popen(  # noqa: S603 - argv built from the bundle itself
            plan.argv,
            cwd=str(plan.root),
            env=plan.env,
        )
    except OSError as exc:
        raise PortableError(f"Cannot start the AVD Fleet daemon: {exc}") from exc


def open_ui(url: str, *, opener: Callable[[str], bool] | None = None) -> bool:
    """Open the web UI in the default browser (best effort)."""
    try:
        return bool((opener or webbrowser.open)(url))
    except Exception:  # pragma: no cover - webbrowser backends can raise
        return False


def _bundle_token(plan: LaunchPlan) -> str | None:
    """Read the portable daemon's token from the bundle data directory."""
    try:
        value = (plan.environment.paths.data / "token").read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


def _bootstrap_browser_url(plan: LaunchPlan) -> tuple[str, str]:
    """Return ``(url, warning)`` for a one-time browser sign-in link.

    The portable bundle uses the same one-time-code flow as ``avdfleet gui``:
    the daemon token stays in local storage while the URL carries only a
    single-use code in its fragment.  Falls back to the ordinary UI URL
    (Connect screen) when the daemon/token is not ready.
    """
    token = _bundle_token(plan)
    if not token:
        return plan.browser_url, "automatic sign-in unavailable; the Connect screen is shown"
    endpoint = plan.browser_url.rstrip("/") + "/api/v1/auth/bootstrap-code"
    request = urllib.request.Request(
        endpoint,
        data=b"{}",
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
        code = payload.get("code") if isinstance(payload, dict) else None
        if isinstance(code, str) and code.strip():
            return (
                plan.browser_url.rstrip("/") + "/auth/bootstrap#code=" + code.strip(),
                "",
            )
    except Exception:  # noqa: BLE001 - manual login remains available
        pass
    return plan.browser_url, "automatic sign-in unavailable; the Connect screen is shown"


def show_error_dialog(message: str, *, title: str = "AVD Fleet", dialog: Any = None) -> bool:
    """Show a native error dialog on Windows (no-op elsewhere)."""
    if dialog is not None:
        try:
            dialog(message)
            return True
        except Exception:  # pragma: no cover - injected dialog failure
            return False
    if sys.platform != "win32":  # pragma: no cover - exercised on Windows in tests only
        return False
    try:  # pragma: no cover - platform UI call
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
        return True
    except Exception:  # pragma: no cover
        return False


def pause_console(*, input_func: Callable[[str], str] | None = None, stream: Any = None) -> None:
    """Pause so a double-clicked launcher window does not vanish on error."""
    reader = input_func or input
    target = sys.stdin if stream is None else stream
    try:
        if not target.isatty():
            return
        reader("[AVD Fleet] Press Enter to close...")
    except (EOFError, OSError, KeyboardInterrupt):  # pragma: no cover - interactive only
        return


def report_failure(
    message: str,
    *,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] | None = None,
    dialog: Any = None,
    pause: Any = None,
    interactive: bool | None = None,
) -> None:
    """Print an error and keep the console/dialog visible for a human."""
    error_writer = err or (lambda text: print(text, file=sys.stderr))
    error_writer(message)
    shown = show_error_dialog(message, dialog=dialog)
    if interactive is None:
        try:
            interactive = sys.stdin.isatty()
        except (AttributeError, OSError):
            interactive = False
    if pause is not None:
        pause()
    elif interactive and not shown:
        pause_console()


def _run_relocation(plan: LaunchPlan, out: Callable[[str], None]) -> None:
    """Rewrite stale absolute paths after the bundle moved (best effort)."""
    environment = plan.environment
    change = detect_root_change(environment)
    if not change.relocated:
        return
    for warning in change.warnings:
        out(f"[AVD Fleet] {warning}")
    try:
        from avdfleet.portable.relocate import relocate_bundle

        result = relocate_bundle(environment.root, environment=environment)
        out(f"[AVD Fleet] Relocated {result.changed_files} file(s) after the bundle move.")
    except Exception as exc:  # relocation must never prevent the daemon from starting
        out(f"[AVD Fleet] WARNING: automatic relocation failed: {exc}")


def _terminate(process: Any) -> None:
    try:
        process.terminate()
    except Exception:  # pragma: no cover - already exited
        return
    try:
        process.wait(timeout=10)
    except Exception:  # pragma: no cover - stubborn child
        try:
            process.kill()
        except Exception:
            pass


def run_launcher(
    options: LaunchOptions,
    *,
    spawn: Callable[[LaunchPlan], Any] | None = None,
    probe: Callable[[str], tuple[bool, str] | bool] | None = None,
    opener: Callable[[str], bool] | None = None,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] | None = None,
    dialog: Any = None,
    pause: Any = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    preflight_runner: Callable[[Path], Any] | None = None,
    is_free: Callable[[int], bool] | None = None,
    environ: dict[str, str] | None = None,
    host_os: str | None = None,
    python_executable: str | Path | None = None,
) -> int:
    """Launch the portable daemon end-to-end and return a process exit code."""
    host = host_os or host_os_name()
    error_writer = err or (lambda text: print(text, file=sys.stderr))
    try:
        root = resolve_root(options.root, environ=environ)
        plan = build_launch_plan(
            root,
            port=options.port,
            no_browser=options.no_browser,
            timeout_s=options.timeout_s,
            extra_args=options.extra_args,
            host_os=host,
            base_env=dict(os.environ) if environ is None else environ,
            python_executable=python_executable,
            is_free=is_free,
        )
    except PortableError as exc:
        report_failure(str(exc), out=out, err=error_writer, dialog=dialog, pause=pause)
        return EXIT_MISSING_RUNTIME

    if options.relocate:
        _run_relocation(plan, out)
    try:
        save_root_state(plan.environment)
    except OSError as exc:  # pragma: no cover - readonly bundle caught by preflight
        out(f"[AVD Fleet] WARNING: could not record launch state: {exc}")
    out(f"[AVD Fleet] Portable bundle: {plan.root}")

    if options.preflight:
        runner = preflight_runner or _default_preflight
        try:
            report = runner(plan.root)
        except Exception as exc:  # pragma: no cover - checks are defensive already
            report = None
            out(f"[AVD Fleet] WARNING: preflight checks failed to run: {exc}")
        if report is not None:
            for warning in getattr(report, "warnings", []):
                out(f"[AVD Fleet] WARNING [{warning.id}]: {warning.detail}")
            if getattr(report, "refuse", False):
                failures = "; ".join(
                    f"{check.id}: {check.detail}" for check in getattr(report, "failures", [])
                )
                report_failure(
                    "[AVD Fleet] Refusing to start on this volume: " + (failures or "preflight failed"),
                    out=out,
                    err=error_writer,
                    dialog=dialog,
                    pause=pause,
                )
                return EXIT_PREFLIGHT

    spawner = spawn or default_spawn
    try:
        process = spawner(plan)
    except PortableError as exc:
        report_failure(str(exc), out=out, err=error_writer, dialog=dialog, pause=pause)
        return EXIT_SERVER
    out(f"[AVD Fleet] Starting daemon: {' '.join(plan.argv)}")
    health = wait_for_health(
        plan.health_url,
        timeout_s=options.timeout_s,
        probe=probe,
        sleep=sleeper,
        clock=clock,
    )
    if not health.healthy:
        _terminate(process)
        detail = f" (last error: {health.last_error})" if health.last_error else ""
        report_failure(
            f"[AVD Fleet] The daemon did not become healthy at {plan.health_url} "
            f"after {health.elapsed_s:.1f}s{detail}.",
            out=out,
            err=error_writer,
            dialog=dialog,
            pause=pause,
        )
        return EXIT_SERVER
    out(f"[AVD Fleet] Daemon healthy after {health.elapsed_s:.1f}s: {plan.browser_url}")
    if not options.no_browser:
        browser_url, auth_warning = _bootstrap_browser_url(plan)
        if auth_warning:
            out(f"[AVD Fleet] {auth_warning}")
        if open_ui(browser_url, opener=opener):
            if not auth_warning:
                out("[AVD Fleet] Opened the web UI with a one-time sign-in link.")
            else:
                out("[AVD Fleet] Opened the web UI in the default browser.")
        else:
            out(f"[AVD Fleet] Open this URL manually: {plan.browser_url}")
    out("[AVD Fleet] Press Ctrl+C to stop the daemon.")
    try:
        returncode = process.wait()
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        out("[AVD Fleet] Stopping the daemon...")
        _terminate(process)
        return EXIT_INTERRUPTED
    except AttributeError:  # injected fake without wait()
        return EXIT_OK
    return int(returncode) if isinstance(returncode, int) else EXIT_OK


def _default_preflight(root: Path) -> Any:
    from avdfleet.portable.checks import run_preflight

    return run_preflight(
        root,
        include_write_speed=False,
        include_quarantine=False,
        include_hypervisor=False,
    )


def resolve_root(
    root: str | Path | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> Path:
    """Locate and validate the bundle root used by the launcher."""
    from avdfleet.portable.env.builder import validate_root

    if root is not None:
        found = Path(root).expanduser().resolve()
    else:
        discovered = discover_root(env=environ)
        if discovered is None:
            raise PortableError(
                "Could not find a portable bundle (avdfleet-portable.json missing)",
                hint="Run the launcher from inside the bundle folder or set AVDFLEET_PORTABLE_ROOT.",
            )
        found = discovered
    validate_root(found)
    return found


def write_launchers(
    root: str | Path,
    *,
    host_os: str | None = None,
    version: str = APP_VERSION,
    dry_run: bool = False,
    include_launchers_dir: bool = True,
) -> list[Path]:
    """Write the host launcher at the bundle root (and under ``launchers/``)."""
    spec = launcher_for_host(host_os, version=version)
    problems = validate_launcher(spec)
    if problems:
        raise PortableError(f"Generated {spec.platform} launcher is invalid: {'; '.join(problems)}")
    base = Path(root)
    targets = [base / spec.filename]
    if include_launchers_dir:
        targets.append(base / "launchers" / spec.filename)
    written: list[Path] = []
    for target in targets:
        written.append(target)
        if dry_run:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(spec.bytes)
        if spec.executable and os.name != "nt":
            try:
                mode = target.stat().st_mode
                target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except OSError:  # pragma: no cover - filesystem without POSIX modes
                pass
    return written


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m avdfleet.portable.launchers``."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        options = parse_launcher_args(arguments)
    except PortableError as exc:
        print(USAGE, file=sys.stderr)
        print(f"[AVD Fleet] {exc}", file=sys.stderr)
        return EXIT_USAGE
    if options.help:
        print(USAGE)
        return EXIT_OK
    return run_launcher(options, python_executable=sys.executable)


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess
    raise SystemExit(main())
