"""Build and validate the portable runtime environment.

The portable edition must not depend on anything installed on the host: every
mutable directory, the Python import path, the Android SDK/AVD homes and the
toolchain ``PATH`` are derived from the bundle root.  The launchers call
:func:`build_environment` and export the returned mapping before starting the
daemon, so the daemon's ``core.paths.Paths`` and every child process resolve
inside the bundle.

When the bundle has been moved (USB stick, different drive letter) the recorded
root in ``avdfleet-portable.json`` / ``config/portable-state.json`` no longer
matches the current location.  :func:`detect_root_change` reports that and
``portable.relocate`` rewrites the affected files.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from avdfleet.core.errors import PortableError

from .model import (
    MANIFEST_FORMAT_VERSION,
    MARKER_NAME,
    STATE_FILE_NAME,
    PortableEnvironment,
    PortablePaths,
    PortableState,
    RootChange,
    RootMarker,
    RootValidation,
    host_arch_name,
    host_os_name,
    isolation_summary,
)

# ``RootMarker`` caps the recorded root at a string; these are the directory
# names tracked by the state file, used to infer old roots after a move.
_TRACKED_DIR_NAMES = frozenset({"data", "config", "cache", "logs", "sdk", "avd", "log"})

#: Environment variables that are removed before the portable values are applied.
_SCRUBBED_ENV = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONEXECUTABLE",
    "__PYVENV_LAUNCHER__",
    "VIRTUAL_ENV",
    "ANDROID_HOME",
    "ANDROID_SDK_ROOT",
    "ANDROID_USER_HOME",
    "ANDROID_PREFS_ROOT",
    "ANDROID_EMULATOR_HOME",
    "ANDROID_AVD_HOME",
    "ANDROID_SDK_HOME",
    "APPIUM_HOME",
    "STUDIO_JDK",
    "STUDIO_GRADLE_JDK",
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "UV_TOOL_DIR",
    "UV_TOOL_BIN_DIR",
    "NPM_CONFIG_CACHE",
    "GRADLE_USER_HOME",
    "STUDIO_PROPERTIES",
    "STUDIO_VM_OPTIONS",
    "AVDFLEET_PORTABLE_ROOT",
    "AVDFLEET_ROOT",
    "AVDFLEET_EDITION",
    "AVDFLEET_DATA_DIR",
    "AVDFLEET_CONFIG_DIR",
    "AVDFLEET_CACHE_DIR",
    "AVDFLEET_LOG_DIR",
    "AVDFLEET_SDK_ROOT",
    "AVDFLEET_AVD_HOME",
    "PIP_CONFIG_FILE",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _norm(value: str | Path, host_os: str) -> str:
    text = os.path.normpath(str(value))
    return text.lower() if host_os == "windows" else text


def _is_windows(host_os: str) -> bool:
    return host_os == "windows"


def _python_executable_name(host_os: str) -> str:
    return "python.exe" if _is_windows(host_os) else "python3"


def _read_marker_file(root: Path) -> RootMarker:
    marker = root / MARKER_NAME
    try:
        raw = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PortableError(
            f"Not a portable bundle root: {MARKER_NAME} is missing",
            hint="Run the bundle from the folder that contains avdfleet-portable.json.",
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise PortableError(f"Cannot read bundle marker {marker}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PortableError(
            f"Bundle marker {marker} is not valid JSON: {exc}",
            hint="The bundle is corrupt; rebuild it with 'avdfleet bundle'.",
        ) from exc
    if not isinstance(raw, dict):
        raise PortableError(f"Bundle marker {marker} must contain a JSON object")
    try:
        return RootMarker.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError, kept message-friendly
        raise PortableError(
            f"Bundle marker {marker} is invalid: {exc}",
            hint="Rebuild the bundle with 'avdfleet bundle'.",
        ) from exc


def read_root_marker(root: str | Path) -> RootMarker:
    """Read and validate the root marker (without checking the host)."""
    root_path = Path(root).expanduser().resolve()
    return _read_marker_file(root_path)


def discover_root(
    start: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    max_depth: int = 12,
) -> Path | None:
    """Locate a bundle root by walking up from *start* (or the current directory).

    ``AVDFLEET_PORTABLE_ROOT`` wins when it names a real bundle; this lets the
    daemon (started by the launcher) rediscover the root from its environment.
    Returns ``None`` when no marker is found; use :func:`validate_root` to raise.
    """
    environ = os.environ if env is None else env
    for variable in ("AVDFLEET_PORTABLE_ROOT", "AVDFLEET_ROOT"):
        candidate = environ.get(variable)
        if candidate:
            path = Path(candidate).expanduser()
            if (path / MARKER_NAME).is_file():
                return path.resolve()
    current = Path(start).expanduser() if start is not None else Path.cwd()
    if current.is_file():
        current = current.parent
    try:
        current = current.resolve()
    except OSError:  # pragma: no cover - unreadable path
        return None
    for _ in range(max_depth):
        if (current / MARKER_NAME).is_file():
            return current
        if current.parent == current:
            return None
        current = current.parent
    return None


def validate_root(
    root: str | Path,
    *,
    host_os: str | None = None,
    host_arch: str | None = None,
) -> RootValidation:
    """Validate a portable bundle root marker for this host.

    Raises :class:`PortableError` when the directory is not a bundle, the marker
    schema version is unsupported or the bundle was built for another OS/arch.
    A root that simply moved returns a warning instead of an error.
    """
    root_path = Path(root).expanduser()
    if not root_path.is_dir():
        raise PortableError(
            f"Portable bundle root does not exist: {root_path}",
            hint="Insert the USB disk or point AVDFLEET_PORTABLE_ROOT at the bundle folder.",
        )
    root_path = root_path.resolve()
    marker = _read_marker_file(root_path)
    if not marker.format_version:
        raise PortableError(
            f"Bundle marker in {root_path} has no format_version",
            hint="Rebuild the bundle with 'avdfleet bundle'.",
        )
    if marker.format_version != MANIFEST_FORMAT_VERSION:
        raise PortableError(
            f"Unsupported bundle format_version {marker.format_version!r} "
            f"(expected {MANIFEST_FORMAT_VERSION!r})",
            hint="Use the launcher from the matching avdfleet version.",
        )
    if not marker.name:
        raise PortableError(f"Bundle marker in {root_path} has no bundle name")

    expected_os = host_os if host_os is not None else host_os_name()
    expected_arch = host_arch if host_arch is not None else host_arch_name()
    warnings: list[str] = []
    if marker.host_os in {"windows", "macos", "linux"}:
        if expected_os in {"windows", "macos", "linux"} and marker.host_os != expected_os:
            raise PortableError(
                f"This bundle was built for {marker.host_os}, but the host is {expected_os}",
                hint="Portable bundles are host-specific; rebuild with 'avdfleet bundle' "
                "on the target machine.",
            )
    else:
        warnings.append("Bundle marker has no usable host_os; portability across hosts is unverified")
    if marker.host_arch in {"x86_64", "arm64"}:
        if expected_arch in {"x86_64", "arm64"} and marker.host_arch != expected_arch:
            raise PortableError(
                f"This bundle was built for {marker.host_arch}, but the host is {expected_arch}",
                hint="Portable bundles are host-specific; rebuild on the target machine.",
            )
    elif expected_arch != "unknown":
        warnings.append("Bundle marker has no usable host_arch; native binaries may not run")
    if marker.root and _norm(marker.root, marker.host_os or expected_os) != _norm(
        root_path, expected_os
    ):
        warnings.append(
            f"Bundle root moved from {marker.root!r} to {str(root_path)!r}; "
            "run relocation before starting the fleet."
        )
    return RootValidation(root=root_path, marker=marker, warnings=warnings)


def _read_pyvenv_version(venv: Path) -> str:
    cfg = venv / "pyvenv.cfg"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() in {"version_info", "version"}:
            return value.strip()
    return ""


def detect_python_version(paths: PortablePaths, *, host_os: str | None = None) -> str:
    """Best-effort Python version discovery that never executes a binary."""
    version = _read_pyvenv_version(paths.venv)
    if version:
        return version
    for pattern in ("lib/python3.*",):
        for candidate in sorted(paths.python.glob(pattern)):
            if candidate.is_dir():
                return candidate.name.removeprefix("python")
    if host_os == "windows":
        for candidate in sorted(paths.python.glob("python3*.dll")):
            return candidate.stem.removeprefix("python")
    return ""


def find_python_executable(
    paths: PortablePaths,
    *,
    host_os: str,
) -> Path | None:
    """Return the preferred bundled interpreter (venv first, then runtime)."""
    candidates: list[Path] = []
    if host_os == "windows":
        candidates.extend(
            [
                paths.venv / "Scripts" / "python.exe",
                paths.python / "python.exe",
            ]
        )
    else:
        candidates.extend(
            [
                paths.venv / "bin" / "python3",
                paths.venv / "bin" / "python",
                paths.python / "bin" / "python3",
                paths.python / "bin" / "python",
            ]
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _venv_site_packages(paths: PortablePaths, *, host_os: str, python_version: str) -> list[str]:
    if host_os == "windows":
        return [str(paths.venv / "Lib" / "site-packages")]
    results = [str(path) for path in sorted(paths.venv.glob("lib/python*/site-packages")) if path.is_dir()]
    if not results and python_version:
        results.append(str(paths.venv / "lib" / f"python{python_version}" / "site-packages"))
    return results


def _runtime_site_packages(paths: PortablePaths, *, host_os: str, python_version: str) -> list[str]:
    if host_os == "windows":
        return [str(paths.python / "Lib" / "site-packages")]
    results = [str(path) for path in sorted(paths.python.glob("lib/python*/site-packages")) if path.is_dir()]
    if not results and python_version:
        results.append(str(paths.python / "lib" / f"python{python_version}" / "site-packages"))
    return results


def _venv_present(paths: PortablePaths, *, host_os: str) -> bool:
    if (paths.venv / "pyvenv.cfg").is_file():
        return True
    executable = find_python_executable(paths, host_os=host_os)
    return executable is not None and paths.venv in executable.parents


def _dedupe(entries: list[str], *, case_insensitive: bool = False) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in entries:
        if not raw:
            continue
        key = os.path.normpath(raw)
        if case_insensitive:
            key = key.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(raw)
    return result


def _join_path(entries: list[str], separator: str, *, case_insensitive: bool = False) -> str:
    return separator.join(_dedupe(entries, case_insensitive=case_insensitive))


def _prepend_path(
    paths: PortablePaths,
    *,
    host_os: str,
    base_path: str,
    has_venv: bool,
) -> str:
    separator = ";" if host_os == "windows" else ":"
    bundled = [str(path) for path in paths.executable_dirs(host_os)]
    # ``bin`` and the interpreter are always on PATH even before the bundle is
    # fully populated, so helper shims resolve once they are written.
    mandatory = [str(paths.bin)]
    if has_venv:
        venv_bin = paths.venv / ("Scripts" if host_os == "windows" else "bin")
        mandatory.insert(0, str(venv_bin))
    for entry in mandatory:
        if entry not in bundled:
            bundled.append(entry)
    # Keep the intended priority: venv/bin, bin, python, sdk, node, jdk.
    ranked: list[str] = []
    prefixes = (
        str(paths.venv),
        str(paths.bin),
        str(paths.python),
        str(paths.sdk),
        str(paths.node),
        str(paths.jdk),
    )
    for prefix in prefixes:
        ranked.extend(entry for entry in bundled if entry.startswith(prefix) and entry not in ranked)
    ranked.extend(entry for entry in bundled if entry not in ranked)
    base_entries = [entry for entry in base_path.split(separator) if entry]
    return _join_path(
        ranked + base_entries, separator, case_insensitive=host_os == "windows"
    )


def build_environment(
    root: str | Path,
    *,
    host_os: str | None = None,
    host_arch: str | None = None,
    python_version: str | None = None,
    base_env: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
    include_studio: bool = True,
) -> PortableEnvironment:
    """Build the complete child-process environment for a portable bundle.

    ``base_env`` (default ``os.environ``) is the process environment being
    extended; ``environ`` is accepted as an alias for readability at call sites.
    Tests pass an explicit mapping so the result does not depend on the host.
    """
    if base_env is not None and environ is not None:
        raise ValueError("pass either base_env or environ, not both")
    source = os.environ if base_env is None and environ is None else (base_env or environ or {})
    os_name = host_os or host_os_name()
    arch = host_arch or host_arch_name()
    validation = validate_root(root, host_os=os_name, host_arch=arch)
    paths = PortablePaths.for_root(validation.root)
    warnings = list(validation.warnings)
    notes = isolation_summary(os_name)

    has_venv = _venv_present(paths, host_os=os_name)
    detected_version = python_version or detect_python_version(paths, host_os=os_name)
    executable = find_python_executable(paths, host_os=os_name)
    if executable is None:
        warnings.append(
            "Bundled Python runtime not found; the launcher will fall back to the system python"
        )
    if not paths.sdk.is_dir():
        warnings.append("Bundled Android SDK not found under the bundle root")
    if not paths.ui.is_dir():
        warnings.append("Bundled web UI not found; the daemon will run API-only")

    env: dict[str, str] = {key: str(value) for key, value in source.items()}
    for variable in _SCRUBBED_ENV:
        env.pop(variable, None)

    # -- avdfleet state: everything inside the root -------------------------
    env["AVDFLEET_PORTABLE_ROOT"] = str(paths.root)
    env["AVDFLEET_ROOT"] = str(paths.root)
    env["AVDFLEET_EDITION"] = "portable"
    env["AVDFLEET_DATA_DIR"] = str(paths.data)
    env["AVDFLEET_CONFIG_DIR"] = str(paths.config)
    env["AVDFLEET_CACHE_DIR"] = str(paths.cache)
    env["AVDFLEET_LOG_DIR"] = str(paths.logs)
    env["AVDFLEET_SDK_ROOT"] = str(paths.sdk)
    env["AVDFLEET_AVD_HOME"] = str(paths.avd)

    # -- Android tooling: SDK + user home isolated --------------------------
    env["ANDROID_HOME"] = str(paths.sdk)
    env["ANDROID_SDK_ROOT"] = str(paths.sdk)
    env["ANDROID_USER_HOME"] = str(paths.android)
    # ANDROID_PREFS_ROOT is the legacy emulator prefs root; modern emulators read
    # ANDROID_USER_HOME while older ones still honour this variable.
    env["ANDROID_PREFS_ROOT"] = str(paths.android)
    env["ANDROID_EMULATOR_HOME"] = str(paths.android)
    env["ANDROID_AVD_HOME"] = str(paths.avd)
    # Deprecated legacy variable: older tools (< Android Studio 4.3) create
    # `<ANDROID_SDK_HOME>/.android`; pointing it at the bundle keeps those
    # writes inside the root instead of the user profile (see research/R7).
    env["ANDROID_SDK_HOME"] = str(paths.root)

    # -- caches and temporary files stay inside the bundle ------------------
    env["TMP"] = str(paths.tmp)
    env["TEMP"] = str(paths.tmp)
    env["TMPDIR"] = str(paths.tmp)
    # Runtime caches live under data/ (not the checksummed cache/ tree, which is
    # reserved for the shipped offline cache), so package-manager writes never
    # appear as undocumented files in 'portable verify'.
    env["PIP_CACHE_DIR"] = str(paths.data / "cache" / "pip")
    env["UV_CACHE_DIR"] = str(paths.data / "cache" / "uv")
    env["UV_PYTHON_INSTALL_DIR"] = str(paths.data / "uv" / "python")
    env["UV_TOOL_DIR"] = str(paths.data / "uv" / "tools")
    env["UV_TOOL_BIN_DIR"] = str(paths.data / "uv" / "bin")
    env["NPM_CONFIG_CACHE"] = str(paths.data / "cache" / "npm")
    env["GRADLE_USER_HOME"] = str(paths.data / "cache" / "gradle")
    env["APPIUM_HOME"] = str(paths.data / "appium")
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    pip_config = paths.config / "pip.conf"
    if pip_config.is_file():
        env["PIP_CONFIG_FILE"] = str(pip_config)

    # -- Python: no user site, deterministic UTF-8, bundle-local imports ----
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Bytecode caches would otherwise appear inside the checksummed tree.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if has_venv:
        env["VIRTUAL_ENV"] = str(paths.venv)
        # PYTHONHOME must stay unset for a venv: pyvenv.cfg already records the
        # base interpreter and overriding the prefix breaks sys.path.
    elif paths.python.is_dir():
        env["PYTHONHOME"] = str(paths.python)
    python_path_parts = [str(paths.lib)]
    if has_venv:
        python_path_parts.extend(_venv_site_packages(paths, host_os=os_name, python_version=detected_version))
    else:
        python_path_parts.extend(
            _runtime_site_packages(paths, host_os=os_name, python_version=detected_version)
        )
    env["PYTHONPATH"] = _join_path(
        python_path_parts,
        ";" if os_name == "windows" else ":",
        case_insensitive=os_name == "windows",
    )

    env["PATH"] = _prepend_path(
        paths, host_os=os_name, base_path=source.get("PATH", ""), has_venv=has_venv
    )

    # -- optional runtimes --------------------------------------------------
    java_home = _java_home(paths, host_os=os_name)
    if java_home is not None:
        env["JAVA_HOME"] = str(java_home)
        env["STUDIO_JDK"] = str(java_home)
        env["STUDIO_GRADLE_JDK"] = str(java_home)
    if include_studio and paths.studio_properties.is_file():
        env["STUDIO_PROPERTIES"] = str(paths.studio_properties)
        if paths.studio_vmoptions.is_file():
            env["STUDIO_VM_OPTIONS"] = str(paths.studio_vmoptions)
    elif include_studio:
        notes.append(
            "Android Studio portable properties not generated yet; run "
            "'avdfleet portable studio' or rebuild the bundle to redirect Studio config."
        )

    return PortableEnvironment(
        root=paths.root,
        marker=validation.marker,
        paths=paths,
        env=env,
        host_os=os_name,
        host_arch=arch,
        python_version=detected_version,
        warnings=warnings,
        notes=notes,
    )


def _java_home(paths: PortablePaths, *, host_os: str) -> Path | None:
    candidates: list[Path] = []
    if host_os == "macos":
        candidates.append(paths.jdk / "Contents" / "Home")
    candidates.append(paths.jdk)
    for candidate in candidates:
        if (candidate / "bin" / ("java.exe" if host_os == "windows" else "java")).is_file():
            return candidate
    return None


def ensure_environment_dirs(environment: PortableEnvironment) -> list[Path]:
    """Create the writable directories a portable launch needs (idempotent)."""
    paths = environment.paths
    created: list[Path] = []
    for directory in (
        paths.data,
        paths.config,
        paths.cache,
        paths.logs,
        paths.tmp,
        paths.avd,
        paths.android,
        paths.studio,
        paths.data / "cache" / "pip",
        paths.data / "cache" / "uv",
        paths.data / "cache" / "npm",
        paths.data / "cache" / "gradle",
        paths.data / "uv" / "python",
        paths.data / "uv" / "tools",
        paths.data / "uv" / "bin",
        paths.data / "appium",
    ):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PortableError(f"Cannot create portable directory {directory}: {exc}") from exc
        created.append(directory)
    return created


# ---------------------------------------------------------------------------
# Root-change detection
# ---------------------------------------------------------------------------


def state_path(root: str | Path) -> Path:
    return PortablePaths.for_root(root).state_file


def load_root_state(root: str | Path) -> PortableState | None:
    """Load the last-launch state; a missing or corrupt file means "unknown"."""
    path = state_path(root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return PortableState.model_validate(raw)
    except Exception:
        return None


def save_root_state(
    environment: PortableEnvironment,
    *,
    when: datetime | None = None,
) -> Path:
    """Persist the current root/paths atomically for later relocation detection."""
    path = environment.paths.state_file
    state = PortableState(
        format_version="1",
        recorded_at=(when or _now()).isoformat(),
        root=str(environment.root),
        host_os=environment.host_os,
        host_arch=environment.host_arch,
        paths={name: str(directory) for name, directory in environment.paths.core_paths().items()},
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # pragma: no cover - best effort cleanup
                pass
    return path


def recorded_old_roots(
    environment: PortableEnvironment,
    *,
    state: PortableState | None = None,
) -> list[str]:
    """All absolute roots recorded in the marker/state that differ from the current one."""
    state = state if state is not None else load_root_state(environment.root)
    candidates: list[str] = []
    if environment.marker.root:
        candidates.append(environment.marker.root)
    if state is not None:
        if state.root:
            candidates.append(state.root)
        for recorded in state.paths.values():
            path = Path(recorded)
            if path.name in _TRACKED_DIR_NAMES:
                candidates.append(str(path.parent))
    current = _norm(environment.root, environment.host_os)
    result: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        if _norm(candidate, environment.host_os) == current:
            continue
        if candidate not in result:
            result.append(candidate)
    return result


def detect_root_change(
    environment: PortableEnvironment,
    *,
    state: PortableState | None = None,
) -> RootChange:
    """Compare the recorded root/paths with the live bundle location."""
    state = state if state is not None else load_root_state(environment.root)
    recorded_root = (state.root if state and state.root else None) or (environment.marker.root or None)
    relocated = bool(
        recorded_root
        and _norm(recorded_root, environment.host_os) != _norm(environment.root, environment.host_os)
    )
    changed_paths: dict[str, str] = {}
    if state is not None:
        current = environment.paths.core_paths()
        for name, recorded in state.paths.items():
            expected = current.get(name)
            if expected is not None and _norm(recorded, environment.host_os) != _norm(
                expected, environment.host_os
            ):
                changed_paths[name] = recorded
    warnings: list[str] = []
    if relocated:
        warnings.append(
            f"Bundle root changed from {recorded_root!r} to {str(environment.root)!r}; "
            "AVD definitions, config and the virtualenv will be rewritten by relocation."
        )
    for name, recorded in sorted(changed_paths.items()):
        warnings.append(
            f"Recorded {name} path {recorded!r} is outside the current bundle root "
            f"({environment.paths.core_paths().get(name)})"
        )
    return RootChange(
        current_root=environment.root,
        recorded_root=recorded_root,
        state_file=environment.paths.state_file,
        relocated=relocated,
        changed_paths=changed_paths,
        warnings=warnings,
    )


def relative_launcher_context(root: str | Path) -> dict[str, str]:
    """Placeholder-free launcher constants shared by the templates.

    Launchers derive the root from their own location (``$0`` / ``%~dp0``) so
    this only documents the expected layout for generated scripts and tests.
    """
    paths = PortablePaths.for_root(root)
    return {
        "marker": MARKER_NAME,
        "state_file": STATE_FILE_NAME,
        "root": str(paths.root),
        "python": str(paths.python),
        "venv": str(paths.venv),
    }


__all__ = [
    "build_environment",
    "detect_python_version",
    "detect_root_change",
    "discover_root",
    "ensure_environment_dirs",
    "find_python_executable",
    "load_root_state",
    "read_root_marker",
    "recorded_old_roots",
    "relative_launcher_context",
    "save_root_state",
    "state_path",
    "validate_root",
]
