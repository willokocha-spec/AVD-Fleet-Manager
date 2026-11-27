"""Shared plumbing for the Android SDK tooling backends.

Both toolchains that avdfleet supports implement the
:class:`avdfleet.core.interfaces.SdkBackend` protocol:

* ``legacy`` -- the long-lived Android command-line tools
  (``sdkmanager``/``avdmanager``/``adb``/``emulator``), and
* ``androidcli`` -- the newer ``android`` command-line interface.

Everything that is toolchain independent lives here: process-boundary
execution (:class:`ToolResult`, :func:`run_tool`), executable discovery for
installed and portable SDK layouts, canonical output parsing, the system-image
catalogue merge and the :func:`get_backend` selector.

Design rules (see ``BRIEF.md`` §3.3 and ``contracts/fakes-spec.md`` §4):

* every external process is executed with an argv list -- never a shell
  string -- and with an explicit timeout, environment and stdin;
* secrets (proxy credentials, API tokens) are redacted before they reach a log
  sink, an error message or an ``OperationResult`` payload;
* both toolchains are exercised by the same unit tests through temporary
  executable shims, so no method may assume a particular binary is installed
  at runtime.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from avdfleet.core.childenv import child_environment
from avdfleet.core.config import Config
from avdfleet.core.errors import CommandError, ConfigError, ToolError
from avdfleet.core.logging import get_logger, redact
from avdfleet.core.models import (
    DeviceProfile,
    ImageKind,
    OperationResult,
    SdkPackage,
    SdkPaths,
    SystemImage,
)

logger = get_logger("platform.sdk")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_S = 120.0
LIST_TIMEOUT_S = 240.0
INSTALL_TIMEOUT_S = 1800.0
LICENSE_TIMEOUT_S = 300.0
VERSION_TIMEOUT_S = 60.0
MIN_ANDROIDCLI_VERSION: tuple[int, int, int] = (1, 0, 0)

#: A ``y`` answer per license prompt.  Extra answers after the last prompt are
#: harmless for both the real tools and the fakes.
LICENSE_PROMPT_REPLY = "y\n" * 64

#: avdmanager asks "Do you wish to create a custom hardware profile? [no]"
#: when a device profile is not supplied.  Non-interactive callers must answer
#: ``no`` or stdin EOF makes the tool fail.
CUSTOM_HARDWARE_REPLY = "no\n"

_IS_WINDOWS = sys.platform == "win32"
_TOOL_SUFFIXES: tuple[str, ...] = (".exe", ".bat", ".cmd", "") if _IS_WINDOWS else ("",)

_IMAGE_ID_RE = re.compile(r"^system-images;([^;]+);([^;]+);([^;]+)$")
_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_BASED_ON_API_RE = re.compile(r"(?:API|Android)\s+(\d+)", re.IGNORECASE)
_TAG_ABI_RE = re.compile(r"Tag/ABI:\s*([^/\s]+)\s*/\s*([^\s,;]+)", re.IGNORECASE)
_BLOCK_SEPARATOR_RE = re.compile(r"^-{3,}\s*$")
_KEY_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 /_-]*?):\s*(.*)$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SdkError(ToolError):
    """Base class for SDK backend failures."""

    code = "sdk_error"


class ToolNotFoundError(SdkError):
    """The requested executable does not exist in the SDK or on ``PATH``."""

    code = "tool_not_found"
    http_status = 500

    def __init__(self, tool: str, *, searched: Iterable[str] = (), hint: str | None = None) -> None:
        self.tool = tool
        self.searched = [str(item) for item in searched]
        if hint is None and tool == "android":
            hint = (
                "Install the new Android CLI or set sdk_paths.android_cli; "
                "the legacy sdkmanager/avdmanager backend remains available."
            )
        super().__init__(f"{tool} executable was not found", hint=hint, details={"tool": tool})


# ---------------------------------------------------------------------------
# Process execution
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    """Outcome of one external process invocation.

    ``stdout``/``stderr``/``argv`` are stored raw so callers can parse them;
    use :meth:`to_dict`, :meth:`summary`, :meth:`redacted_stdout` and
    :meth:`redacted_stderr` whenever the value crosses a log/API boundary.
    """

    argv: list[str] = field(default_factory=list)
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def combined(self) -> str:
        if self.stdout and self.stderr:
            return f"{self.stdout.rstrip()}\n{self.stderr.rstrip()}"
        return (self.stdout or self.stderr).strip()

    def redacted_argv(self) -> list[str]:
        return [str(redact(part)) for part in self.argv]

    def redacted_stdout(self, *, limit: int = 8000) -> str:
        return str(redact(self._truncate(self.stdout, limit)))

    def redacted_stderr(self, *, limit: int = 8000) -> str:
        return str(redact(self._truncate(self.stderr, limit)))

    def summary(self, *, limit: int = 2000) -> str:
        """One redacted, trimmed string suitable for an error message or log."""
        text = self.combined
        return str(redact(self._truncate(text, limit)))

    def to_dict(self, *, limit: int = 8000) -> dict[str, Any]:
        """JSON-safe, redacted representation for ``OperationResult.data``."""
        return {
            "argv": self.redacted_argv(),
            "returncode": self.returncode,
            "stdout": self.redacted_stdout(limit=limit),
            "stderr": self.redacted_stderr(limit=limit),
            "duration_s": round(self.duration_s, 3),
            "timed_out": self.timed_out,
        }

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        if limit <= 0 or len(text) <= limit:
            return text
        return text[:limit] + "… [truncated]"


Runner = Callable[..., ToolResult]


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_tool(
    executable: str | Path,
    args: Sequence[str] = (),
    *,
    stdin: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
) -> ToolResult:
    """Run ``executable args...`` without a shell and return a :class:`ToolResult`.

    ``stdin`` is written to the child process; when it is ``None`` the child
    gets ``DEVNULL`` so a prompt can never hang a daemon thread.  A timeout
    kills the child and returns ``timed_out=True`` instead of raising, which
    lets callers report a structured failure; a missing/permission-denied
    executable raises :class:`ToolNotFoundError`/``SdkError`` because that is
    an environment problem, not a tool outcome.
    """
    argv = [str(executable), *(str(item) for item in args)]
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": max(0.001, float(timeout_s)),
        "cwd": str(cwd) if cwd is not None else None,
        "env": dict(env) if env is not None else None,
        "check": False,
    }
    if stdin is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin
    logger.debug("exec %s", redact(argv))
    started = time.monotonic()
    try:
        completed = subprocess.run(argv, **kwargs)
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - started
        logger.warning("tool timed out after %.2fs: %s", duration, redact(argv[0]))
        return ToolResult(
            argv=argv,
            returncode=-9,
            stdout=_coerce_text(exc.stdout),
            stderr=_coerce_text(exc.stderr),
            duration_s=duration,
            timed_out=True,
        )
    except FileNotFoundError as exc:
        raise ToolNotFoundError(Path(argv[0]).name, searched=[argv[0]]) from exc
    except PermissionError as exc:
        raise SdkError(
            f"not permitted to execute {Path(argv[0]).name}",
            hint="Check the executable bit / file ACLs of the Android SDK tools.",
        ) from exc
    except OSError as exc:
        raise SdkError(
            f"could not execute {Path(argv[0]).name}: {redact(str(exc))}",
            hint="Repair or reinstall the Android SDK command-line tools.",
        ) from exc
    duration = time.monotonic() - started
    result = ToolResult(
        argv=argv,
        returncode=completed.returncode,
        stdout=_coerce_text(completed.stdout),
        stderr=_coerce_text(completed.stderr),
        duration_s=duration,
    )
    logger.debug(
        "exit %s in %.3fs: %s",
        result.returncode,
        duration,
        redact(argv[0]),
    )
    return result


def failed_command_error(result: ToolResult, action: str) -> CommandError:
    """Build a redacted :class:`CommandError` for a failed :class:`ToolResult`."""
    if result.timed_out:
        message = f"{action} timed out after {result.duration_s:.1f}s"
    else:
        message = f"{action} failed with exit code {result.returncode}"
    return CommandError(
        message,
        argv=result.redacted_argv(),
        exit_code=result.returncode,
        stdout=result.redacted_stdout(limit=4000),
        stderr=result.redacted_stderr(limit=4000),
        hint=None,
    )


def require_ok(result: ToolResult, action: str) -> ToolResult:
    """Return *result* when successful, otherwise raise a redacted ``CommandError``."""
    if not result.ok:
        raise failed_command_error(result, action)
    return result


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------


def parse_version(text: str) -> str:
    """Extract the first ``X.Y[.Z]`` version token from *text* (``""`` if absent)."""
    match = _VERSION_RE.search(text or "")
    return match.group(0) if match else ""


def parse_version_tuple(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.search(text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def version_at_least(text: str, minimum: Sequence[int]) -> bool:
    parsed = parse_version_tuple(text)
    if parsed is None:
        return False
    padded = tuple(minimum) + (0,) * max(0, 3 - len(minimum))
    return parsed >= padded


# ---------------------------------------------------------------------------
# Executable resolution
# ---------------------------------------------------------------------------

_EXPLICIT_FIELDS: dict[str, str] = {
    "sdkmanager": "cmdline_tools",
    "avdmanager": "cmdline_tools",
    "adb": "platform_tools",
    "emulator": "emulator",
    "android": "android_cli",
    "aapt": "build_tools",
    "aapt2": "build_tools",
    "apksigner": "build_tools",
}


def _candidate_variants(path: Path) -> list[Path]:
    """Expand a bare executable path to the platform-specific file names."""
    if path.suffix.lower() in {".exe", ".bat", ".cmd", ".sh"}:
        return [path]
    variants: list[Path] = []
    for suffix in _TOOL_SUFFIXES:
        variants.append(path.with_name(path.name + suffix) if suffix else path)
    return variants


class ToolResolver:
    """Resolve Android SDK executables from :class:`SdkPaths`, layout and ``PATH``.

    Installed and portable SDK layouts differ (portable bundles relocate the
    whole tree), so resolution is deliberately probe-based: explicit
    ``SdkPaths`` fields win, then the canonical layout under ``sdk_root``,
    then ``PATH``.  Nothing is cached: the installer creates tools at runtime
    and the next lookup must see them.
    """

    def __init__(self, paths: SdkPaths, *, environ: Mapping[str, str] | None = None) -> None:
        self.paths = paths
        self.environ = dict(os.environ if environ is None else environ)

    # -- public ---------------------------------------------------------
    def candidates(self, tool: str) -> list[Path]:
        root = Path(self.paths.sdk_root) if self.paths.sdk_root else None
        found: list[Path] = []
        seen: set[str] = set()

        def add(candidate: Path) -> None:
            for variant in _candidate_variants(candidate):
                key = str(variant)
                if key not in seen:
                    seen.add(key)
                    found.append(variant)

        explicit = getattr(self.paths, _EXPLICIT_FIELDS.get(tool, ""), None) if tool in _EXPLICIT_FIELDS else None
        if explicit:
            explicit_path = Path(str(explicit))
            add(explicit_path)
            add(explicit_path / "bin" / tool)
            add(explicit_path / tool)
        if root is not None:
            for directory in self._standard_dirs(tool, root):
                add(directory / tool)
        return found

    def find(self, tool: str) -> Path | None:
        for candidate in self.candidates(tool):
            if self._is_executable(candidate):
                return candidate
        which = shutil.which(tool, path=self.environ.get("PATH", ""))
        if which:
            candidate = Path(which)
            if candidate.is_file():
                return candidate
        return None

    def require(self, tool: str) -> Path:
        found = self.find(tool)
        if found is not None:
            return found
        raise ToolNotFoundError(tool, searched=[str(item) for item in self.candidates(tool)][:10])

    # -- internals ------------------------------------------------------
    def _standard_dirs(self, tool: str, root: Path) -> list[Path]:
        directories: list[Path] = []
        if tool in {"sdkmanager", "avdmanager", "android"}:
            directories.append(root / "cmdline-tools" / "latest" / "bin")
            versioned = sorted(
                (item for item in (root / "cmdline-tools").glob("*") if item.is_dir()),
                key=lambda item: item.name,
                reverse=True,
            )
            directories.extend(item / "bin" for item in versioned)
            if tool == "android":
                directories.extend(
                    [
                        root / "tools",
                        root / "tools" / "bin",
                        root / "android" / "bin",
                        root / "android",
                    ]
                )
            else:
                directories.append(root / "tools" / "bin")
        if tool == "adb":
            directories.append(root / "platform-tools")
        if tool == "emulator":
            directories.append(root / "emulator")
        if tool in {"aapt", "aapt2", "apksigner"}:
            build_tools = root / "build-tools"
            directories.extend(
                sorted(
                    (item for item in build_tools.glob("*") if item.is_dir()),
                    key=lambda item: item.name,
                    reverse=True,
                )
            )
        return directories

    @staticmethod
    def _is_executable(path: Path) -> bool:
        try:
            if not path.is_file():
                return False
        except OSError:
            return False
        if _IS_WINDOWS:
            return True
        return os.access(path, os.X_OK)


# ---------------------------------------------------------------------------
# SDK paths resolution (config / host / environment)
# ---------------------------------------------------------------------------


def _pick(*values: str | None) -> str | None:
    for value in values:
        if value:
            return str(value)
    return None


def _paths_from_host(host: Any) -> SdkPaths | None:
    """Accept SdkPaths, ``HostProvider``-style objects or duck-typed paths."""
    if host is None:
        return None
    if isinstance(host, SdkPaths):
        return host
    getter = getattr(host, "sdk_paths", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:  # noqa: BLE001 - probe only, never fail selection
            value = None
        if isinstance(value, SdkPaths):
            return value
    elif isinstance(getter, SdkPaths):
        return getter
    attributes = {
        name: getattr(host, name)
        for name in SdkPaths.model_fields
        if getattr(host, name, None) not in (None, "", {})
    }
    if attributes:
        try:
            return SdkPaths(**attributes)
        except Exception:  # noqa: BLE001 - ignore malformed host objects
            return None
    return None


def resolve_sdk_paths(
    config: Config | None = None,
    host: Any = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> SdkPaths:
    """Compute the effective :class:`SdkPaths` for a backend.

    Precedence: explicit ``Config`` overrides > host-provided ``SdkPaths`` >
    ``ANDROID_SDK_ROOT``/``ANDROID_HOME``/``ANDROID_AVD_HOME`` environment >
    empty.  No directory is created; probing is side-effect free.
    """
    env = dict(os.environ if environ is None else environ)
    base = _paths_from_host(host)
    if base is None:
        base = SdkPaths(
            sdk_root=_pick(
                config.sdk_root if config is not None else None,
                env.get("ANDROID_SDK_ROOT"),
                env.get("ANDROID_HOME"),
                "",
            )
            or "",
            avd_home=_pick(
                config.avd_home if config is not None else None,
                env.get("ANDROID_AVD_HOME"),
            ),
        )
    else:
        base = base.model_copy(deep=True)
        if config is not None:
            if config.sdk_root:
                base.sdk_root = config.sdk_root
            if config.avd_home:
                base.avd_home = config.avd_home
        if not base.sdk_root:
            base.sdk_root = _pick(env.get("ANDROID_SDK_ROOT"), env.get("ANDROID_HOME"), "") or ""
        if not base.avd_home:
            base.avd_home = _pick(env.get("ANDROID_AVD_HOME"))
    if base.env is None:
        base.env = {}
    return base


def sdk_bin_dirs(paths: SdkPaths) -> list[str]:
    """Existing SDK tool directories, in ``PATH``-precedence order."""
    root = Path(paths.sdk_root) if paths.sdk_root else None
    if root is None:
        return []
    candidates: list[Path] = [
        root / "cmdline-tools" / "latest" / "bin",
        root / "platform-tools",
        root / "emulator",
    ]
    versioned = sorted(
        (item for item in (root / "cmdline-tools").glob("*") if item.is_dir()),
        key=lambda item: item.name,
        reverse=True,
    )
    candidates.extend(item / "bin" for item in versioned)
    candidates.append(root / "tools" / "bin")
    directories: list[str] = []
    for candidate in candidates:
        try:
            if candidate.is_dir():
                text = str(candidate)
                if text not in directories:
                    directories.append(text)
        except OSError:
            continue
    return directories


def build_environment(paths: SdkPaths, *, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment variables an Android tool process should see."""
    env: dict[str, str] = {}
    if paths.sdk_root:
        env["ANDROID_SDK_ROOT"] = str(paths.sdk_root)
        env["ANDROID_HOME"] = str(paths.sdk_root)
    if paths.avd_home:
        env["ANDROID_AVD_HOME"] = str(paths.avd_home)
    directories = sdk_bin_dirs(paths)
    if directories:
        existing = (base or {}).get("PATH") or os.environ.get("PATH", "")
        env["PATH"] = os.pathsep.join([*directories, *([existing] if existing else [])])
    env.update({str(key): str(value) for key, value in (paths.env or {}).items()})
    return env


def default_avd_home(paths: SdkPaths | None = None) -> Path:
    """Best-effort AVD home: configured, environment, then ``~/.android/avd``."""
    if paths is not None and paths.avd_home:
        return Path(paths.avd_home)
    configured = os.environ.get("ANDROID_AVD_HOME")
    if configured:
        return Path(configured)
    return Path.home() / ".android" / "avd"


# ---------------------------------------------------------------------------
# Canonical output parsing shared by both toolchains
# ---------------------------------------------------------------------------


def parse_package_table(text: str) -> tuple[list[SdkPackage], list[SdkPackage]]:
    """Parse the canonical ``sdkmanager --list`` table.

    Returns ``(installed, available)`` lists.  The parser is intentionally
    lenient: it skips banners/warnings, tolerates a missing ``Location``
    column and de-duplicates package ids.  Both the legacy tool and the new
    Android CLI fall back to this table format (``contracts/fakes-spec.md``
    §4.1/§4.5).
    """
    installed: dict[str, SdkPackage] = {}
    available: dict[str, SdkPackage] = {}
    section: str | None = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith("installed packages"):
            section = "installed"
            continue
        if lowered.startswith("available packages") or lowered.startswith("available updates"):
            section = "available"
            continue
        if section is None or "|" not in line:
            continue
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 2:
            continue
        identifier = cells[0]
        if not identifier or set(identifier) <= {"-", " "}:
            continue
        if identifier.lower() == "path":
            continue
        version = cells[1] if len(cells) > 1 else ""
        description = cells[2] if len(cells) > 2 else ""
        location = cells[3] if len(cells) > 3 else ""
        package = SdkPackage(
            id=identifier,
            version=version,
            description=description,
            installed=section == "installed",
            path=location or None,
        )
        if section == "installed":
            installed.setdefault(identifier, package)
        else:
            available.setdefault(identifier, package)
    return list(installed.values()), list(available.values())


def parse_key_value_blocks(text: str) -> list[dict[str, str]]:
    """Split ``avdmanager``-style ``Key: value`` blocks separated by dashes."""
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _BLOCK_SEPARATOR_RE.match(line):
            if current:
                blocks.append(current)
            current = {}
            continue
        match = _KEY_LINE_RE.match(line)
        if match:
            current[match.group(1).strip().lower()] = match.group(2).strip()
    if current:
        blocks.append(current)
    return blocks


@dataclass(frozen=True)
class AvdEntry:
    """One AVD as reported by ``avdmanager list avd`` / ``android avd list``."""

    name: str
    path: str = ""
    target: str = ""
    based_on: str = ""
    api_level: int = 0
    tag: str = ""
    abi: str = ""

    @property
    def image_package(self) -> str:
        if self.api_level and self.tag and self.abi:
            return f"system-images;android-{self.api_level};{self.tag};{self.abi}"
        return ""


def parse_based_on(value: str) -> tuple[int, str, str]:
    """``"Android API 35 Tag/ABI: google_apis_playstore/x86_64"`` -> ``(35, tag, abi)``."""
    api_match = _BASED_ON_API_RE.search(value or "")
    api_level = int(api_match.group(1)) if api_match else 0
    tag_abi = _TAG_ABI_RE.search(value or "")
    tag = tag_abi.group(1) if tag_abi else ""
    abi = tag_abi.group(2) if tag_abi else ""
    return api_level, tag, abi


def parse_avd_entries(text: str) -> list[AvdEntry]:
    """Parse ``avdmanager list avd`` output into :class:`AvdEntry` records."""
    entries: list[AvdEntry] = []
    for block in parse_key_value_blocks(text):
        name = block.get("name", "").strip()
        if not name:
            continue
        based_on = block.get("based on", "")
        api_level, tag, abi = parse_based_on(based_on)
        entries.append(
            AvdEntry(
                name=name,
                path=block.get("path", ""),
                target=block.get("target", ""),
                based_on=based_on,
                api_level=api_level,
                tag=tag,
                abi=abi,
            )
        )
    return entries


def parse_device_profiles(text: str) -> list[dict[str, str]]:
    """Parse ``avdmanager list device`` blocks into dictionaries with ``id``/``name``."""
    profiles: list[dict[str, str]] = []
    for block in parse_key_value_blocks(text):
        raw_id = block.get("id", "").strip()
        if not raw_id:
            continue
        # Fake/real formats: ``id: 7 or "pixel_7"`` / ``id: pixel_7``.
        quoted = re.findall(r'"([^"]+)"', raw_id)
        identifier = quoted[0] if quoted else (raw_id.split(" or ")[-1].strip().strip('"') or raw_id)
        name = block.get("name", "").strip() or identifier
        profiles.append(
            {
                "id": identifier,
                "name": name,
                "tag": block.get("tag", ""),
                "resolution": block.get("resolution", ""),
            }
        )
    return profiles


# ---------------------------------------------------------------------------
# System images
# ---------------------------------------------------------------------------


def target_api_level(target: str) -> int:
    """``android-35`` / ``android-35-ext14`` -> ``35``."""
    match = re.search(r"(\d+)", target or "")
    return int(match.group(1)) if match else 0


def image_kind_for_tag(tag: str) -> ImageKind:
    """Map an SDK image tag to the domain :class:`ImageKind`."""
    lowered = (tag or "").strip().lower()
    if "playstore" in lowered:
        return ImageKind.GOOGLE_APIS_PLAYSTORE
    if lowered.startswith("google"):
        return ImageKind.GOOGLE_APIS
    if lowered.startswith("aosp"):
        return ImageKind.AOSP
    if not lowered or lowered == "default":
        return ImageKind.DEFAULT
    return ImageKind.UNKNOWN


def image_display_name(api_level: int, tag: str, abi: str) -> str:
    label = {
        "google_apis_playstore": "Google Play",
        "google_apis": "Google APIs",
        "aosp_atd": "AOSP ATD",
        "google_atd": "Google ATD",
        "default": "AOSP Default",
    }.get(tag, tag.replace("_", " ").title())
    base = f"Android API {api_level}" if api_level else "Android system image"
    return f"{base} {label} ({abi})"


def parse_image_package_id(package_id: str) -> tuple[str, str, str] | None:
    """Return ``(target, tag, abi)`` for a ``system-images;`` package id."""
    match = _IMAGE_ID_RE.match((package_id or "").strip())
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def build_system_image(
    package_id: str,
    *,
    installed: bool = False,
    sdk_root: str | Path | None = None,
    version: str = "",
    local_path: str | None = None,
) -> SystemImage | None:
    """Build a :class:`SystemImage` from a system-image SDK package id."""
    parsed = parse_image_package_id(package_id)
    if parsed is None:
        return None
    target, tag, abi = parsed
    api_level = target_api_level(target)
    path = local_path
    if path is None and sdk_root:
        path = str(Path(sdk_root) / "system-images" / target / tag / abi)
    metadata: dict[str, Any] = {"target": target}
    if version:
        metadata["version"] = version
    return SystemImage(
        id=package_id.strip(),
        api_level=api_level,
        tag=tag,
        abi=abi,
        kind=image_kind_for_tag(tag),
        display_name=image_display_name(api_level, tag, abi),
        package=package_id.strip(),
        installed=installed,
        local_path=path,
        metadata=metadata,
    )


def _image_dir_looks_installed(directory: Path) -> bool:
    # A partial/aborted sdkmanager download leaves source.properties and other
    # metadata behind; only the bootable-image markers prove an install.  The
    # previous "any file" fallback made every catalogue directory look installed.
    for name in (
        "system.img",
        "system.img.sparse",
        "system-qemu.img",
        "system.img.qcow2",
        "system.img.raw",
        "ramdisk.img",
        "package.xml",
    ):
        try:
            if (directory / name).exists():
                return True
        except OSError:
            return False
    return False


def scan_installed_images(sdk_root: str | Path | None) -> dict[str, SystemImage]:
    """Scan ``<sdk>/system-images/<target>/<tag>/<abi>`` on disk, keyed by package id."""
    images: dict[str, SystemImage] = {}
    if not sdk_root:
        return images
    root = Path(sdk_root) / "system-images"
    try:
        if not root.is_dir():
            return images
        target_dirs = sorted(item for item in root.iterdir() if item.is_dir())
    except OSError:
        return images
    for target_dir in target_dirs:
        try:
            tag_dirs = sorted(item for item in target_dir.iterdir() if item.is_dir())
        except OSError:
            continue
        for tag_dir in tag_dirs:
            try:
                abi_dirs = sorted(item for item in tag_dir.iterdir() if item.is_dir())
            except OSError:
                continue
            for abi_dir in abi_dirs:
                if not _image_dir_looks_installed(abi_dir):
                    continue
                package_id = f"system-images;{target_dir.name};{tag_dir.name};{abi_dir.name}"
                image = build_system_image(package_id, installed=True, local_path=str(abi_dir))
                if image is not None:
                    images[image.id] = image
    return images


def sort_system_images(images: Iterable[SystemImage]) -> list[SystemImage]:
    """Deterministic catalogue order: API level, tag, ABI, id (all ascending)."""
    return sorted(images, key=lambda image: (image.api_level, image.tag, image.abi, image.id))


def merge_system_images(
    catalogue: Iterable[SystemImage],
    scanned: Mapping[str, SystemImage],
) -> list[SystemImage]:
    """Merge catalogue entries with the installed-directory scan."""
    merged: dict[str, SystemImage] = {image.id: image for image in catalogue}
    for identifier, scanned_image in scanned.items():
        existing = merged.get(identifier)
        if existing is None:
            merged[identifier] = scanned_image
            continue
        merged[identifier] = existing.model_copy(
            update={
                "installed": True,
                "local_path": existing.local_path or scanned_image.local_path,
            }
        )
    return sort_system_images(merged.values())


def images_from_packages(packages: Sequence[SdkPackage], sdk_root: str | Path | None) -> list[SystemImage]:
    """Build + scan + merge the system-image list from a package catalogue."""
    catalogue: list[SystemImage] = []
    for package in packages:
        image = build_system_image(
            package.id,
            installed=package.installed,
            sdk_root=sdk_root,
            version=package.version,
        )
        if image is not None:
            catalogue.append(image)
    return merge_system_images(catalogue, scan_installed_images(sdk_root))


# ---------------------------------------------------------------------------
# AVD config helpers (A1 owns richer config.ini editing; the backend only
# applies creation-time overrides and reports where the AVD was written).
# ---------------------------------------------------------------------------


def read_ini_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def update_ini_file(path: Path, updates: Mapping[str, Any]) -> Path:
    """Idempotently apply ``key=value`` overrides, preserving unrelated lines."""
    existing_lines: list[str] = []
    try:
        existing_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        existing_lines = []
    applied: set[str] = set()
    output: list[str] = []
    for raw_line in existing_lines:
        stripped = raw_line.strip()
        if stripped and not stripped.startswith(("#", ";")) and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                output.append(f"{key}={updates[key]}")
                applied.add(key)
                continue
        output.append(raw_line)
    for key, value in updates.items():
        if key not in applied:
            output.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(output)
    if text:
        text += "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


# ---------------------------------------------------------------------------
# Device profiles
# ---------------------------------------------------------------------------

#: Fallback catalogue used when ``avdmanager list device`` is unavailable (for
#: example an Android CLI-only install).  The ids match
#: ``contracts/fakes-spec.md`` §6 so templates created against fakes stay valid
#: on a real host.
_DEFAULT_DEVICE_PROFILE_SPECS: tuple[tuple[str, str, str, int, int], ...] = (
    ("pixel_9", "Pixel 9", "1080x2400", 420, 4096),
    ("pixel_8", "Pixel 8", "1080x2400", 420, 4096),
    ("pixel_7", "Pixel 7", "1080x2400", 420, 4096),
    ("pixel_6", "Pixel 6", "1080x2400", 420, 4096),
    ("pixel_tablet", "Pixel Tablet", "2560x1600", 320, 4096),
    ("pixel_2", "Pixel 2", "1080x1920", 420, 2048),
    ("Nexus 5X", "Nexus 5X", "1080x1920", 420, 2048),
)


def default_device_profiles() -> list[DeviceProfile]:
    """Built-in device profile catalogue (stable order)."""
    return [
        DeviceProfile(id=identifier, name=name, resolution=resolution, density=density, ram_mb=ram_mb)
        for identifier, name, resolution, density, ram_mb in _DEFAULT_DEVICE_PROFILE_SPECS
    ]


# ---------------------------------------------------------------------------
# Base backend
# ---------------------------------------------------------------------------


class BaseSdkBackend:
    """Shared implementation details for both toolchains.

    Subclasses implement the :class:`~avdfleet.core.interfaces.SdkBackend`
    protocol methods that actually differ (package listing/installation, AVD
    management, availability); this base class supplies process execution,
    environment construction, image catalogue merge, AVD path handling and the
    common ``adb``/``emulator`` helpers used by A1/N1.
    """

    name: str = "base"
    fallback_reason: str = ""

    def __init__(
        self,
        config: Config | None = None,
        sdk_paths: SdkPaths | None = None,
        *,
        host: Any = None,
        environ: Mapping[str, str] | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.config = config
        self.host = host
        self._environ = {**os.environ, **{str(key): str(value) for key, value in (environ or {}).items()}}
        self._runner: Runner = runner or run_tool
        self.sdk_paths = (
            sdk_paths if sdk_paths is not None else resolve_sdk_paths(config, host, environ=self._environ)
        )
        self._resolver = ToolResolver(self.sdk_paths, environ=self._environ)

    # -- environment / executables -------------------------------------
    @property
    def sdk_root(self) -> Path | None:
        return Path(self.sdk_paths.sdk_root) if self.sdk_paths.sdk_root else None

    @property
    def avd_home(self) -> Path:
        return default_avd_home(self.sdk_paths)

    def find_executable(self, tool: str) -> Path | None:
        return self._resolver.find(tool)

    def executable(self, tool: str) -> Path:
        return self._resolver.require(tool)

    def environment(self) -> dict[str, str]:
        """Environment variables to configure for the Android tools."""
        return build_environment(self.sdk_paths, base=self._environ)

    def process_env(self) -> dict[str, str]:
        """Full subprocess environment (ambient environment + SDK variables).

        Daemon secrets are stripped and the adb transport limit is raised so
        tools started here inherit the same safe environment as other children.
        """
        return child_environment({**self._environ, **self.environment()})

    def _run(
        self,
        tool: str | Path,
        args: Sequence[str] = (),
        *,
        stdin: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        cwd: str | Path | None = None,
    ) -> ToolResult:
        executable = tool if isinstance(tool, Path) else self.executable(tool)
        return self._runner(
            executable,
            [str(item) for item in args],
            stdin=stdin,
            timeout_s=timeout_s,
            env=self.process_env(),
            cwd=cwd,
        )

    def available(self) -> bool:  # pragma: no cover - overridden by androidcli
        """Whether the toolchain can be selected.  Legacy is always selectable."""
        return True

    def supports(self, feature: str) -> bool:  # pragma: no cover - overridden
        """Whether a named optional feature is supported by this toolchain."""
        return False

    # -- AVD paths / config --------------------------------------------
    def avd_dir(self, name: str) -> Path:
        return self.avd_home / f"{name}.avd"

    def avd_config_path(self, name: str) -> Path:
        return self.avd_dir(name) / "config.ini"

    def avd_ini_path(self, name: str) -> Path:
        return self.avd_home / f"{name}.ini"

    def read_avd_config(self, name: str) -> dict[str, str]:
        return read_ini_file(self.avd_config_path(name))

    def write_avd_config(self, name: str, overrides: Mapping[str, Any]) -> Path:
        """Apply config.ini overrides after AVD creation and return the path."""
        return update_ini_file(self.avd_config_path(name), overrides)

    # -- device profiles ------------------------------------------------
    def list_device_profiles(self) -> list[DeviceProfile]:
        """Device profiles from a legacy ``avdmanager`` when available, else built-ins."""
        executable = self.find_executable("avdmanager")
        if executable is not None:
            try:
                result = self._runner(
                    executable,
                    ["list", "device"],
                    stdin=None,
                    timeout_s=LIST_TIMEOUT_S,
                    env=self.process_env(),
                )
            except (ToolError, OSError):
                result = None
            if result is not None and result.ok:
                profiles: list[DeviceProfile] = []
                for entry in parse_device_profiles(result.combined):
                    identifier = entry.get("id", "").strip()
                    if not identifier:
                        continue
                    profiles.append(
                        DeviceProfile(
                            id=identifier,
                            name=entry.get("name") or identifier,
                            tags=[entry["tag"]] if entry.get("tag") else [],
                        )
                    )
                if profiles:
                    return profiles
        return default_device_profiles()

    # -- system images --------------------------------------------------
    def list_system_images(self) -> list[SystemImage]:
        raise NotImplementedError

    # -- shared adb helpers ---------------------------------------------
    def adb_executable(self) -> Path:
        return self.executable("adb")

    def emulator_executable(self) -> Path:
        return self.executable("emulator")

    def adb_devices(self) -> list[Any]:
        """Return parsed ``adb devices -l`` entries (``AdbDevice`` records)."""
        from .legacy.adb import Adb

        return Adb(
            self.adb_executable(),
            runner=self._runner,
            environ=self.process_env(),
        ).devices()

    def adb_device_serials(self) -> list[str]:
        return [device.serial for device in self.adb_devices()]

    def build_launch_argv(self, avd_name: str, **kwargs: Any) -> list[str]:
        """Build an emulator launch argv; see :meth:`legacy.emulator.Emulator.build_launch_argv`."""
        from .legacy.emulator import Emulator

        emulator = Emulator(
            self.emulator_executable(),
            avd_home=self.avd_home,
            runner=self._runner,
            environ=self.process_env(),
        )
        return emulator.build_launch_argv(avd_name, **kwargs)

    def emulator_version(self) -> str:
        from .legacy.emulator import Emulator

        return Emulator(
            self.emulator_executable(),
            avd_home=self.avd_home,
            runner=self._runner,
            environ=self.process_env(),
        ).version()

    def wait_for_device(self, serial: str | None = None, *, timeout_s: float = 120.0) -> str:
        """Block until ``adb`` reports *serial* (or any device) as ``device``."""
        from .legacy.adb import Adb

        adb = Adb(self.adb_executable(), runner=self._runner, environ=self.process_env())
        return adb.wait_for_device(serial=serial, timeout_s=timeout_s)

    def launch_emulator(
        self,
        avd_name: str,
        *,
        log_path: str | Path | None = None,
        **kwargs: Any,
    ) -> tuple[Any, list[str]]:
        """Build the launch argv and start the emulator; returns ``(process, argv)``.

        The returned ``Popen`` is owned by the caller (N1) for supervision.
        """
        from .legacy.emulator import Emulator

        emulator = Emulator(
            self.emulator_executable(),
            avd_home=self.avd_home,
            runner=self._runner,
            environ=self.process_env(),
        )
        return emulator.launch_avd(avd_name, log_path=log_path, **kwargs)

    def stop_emulator(self, *, serial: str | None = None) -> OperationResult:
        """Kill one emulator through ``adb emu kill`` (graceful, no signal)."""
        from .legacy.adb import Adb

        try:
            Adb(
                self.adb_executable(),
                serial=serial,
                runner=self._runner,
                environ=self.process_env(),
            ).kill_emulator()
        except ToolError as exc:
            return OperationResult(ok=False, message=str(exc), data={"serial": serial or ""})
        return OperationResult(ok=True, message="emulator killed", data={"serial": serial or ""})

    def adb_avd_name(self, *, serial: str | None = None) -> str:
        """Return the AVD name for an ``emulator-<port>`` adb serial."""
        from .legacy.adb import Adb

        return Adb(
            self.adb_executable(),
            serial=serial,
            runner=self._runner,
            environ=self.process_env(),
        ).avd_name()

    def open_console(
        self,
        port: int,
        *,
        token: str | None = None,
        home: str | Path | None = None,
        timeout_s: float = 5.0,
    ) -> Any:
        """Connect an :class:`EmulatorConsole` to *port* and return it.

        Caller owns the object; use it as a context manager or call ``close()``.
        """
        from .console import EmulatorConsole

        console = EmulatorConsole(token=token, home=home, environ=self._environ)
        console.connect(port, timeout_s=timeout_s)
        return console


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


_AUTO_ALIASES = {"auto", "", "default", "best"}
_ANDROIDCLI_ALIASES = {"androidcli", "android", "cli", "new"}
_LEGACY_ALIASES = {"legacy", "sdkmanager", "old", "classic"}


def get_backend(
    config: Config | None = None,
    host: Any = None,
    prefer: str = "auto",
) -> BaseSdkBackend:
    """Select an :class:`~avdfleet.core.interfaces.SdkBackend` implementation.

    ``prefer="auto"`` (the default) uses the new ``android`` CLI when its
    executable is present and its version is supported, otherwise the legacy
    ``sdkmanager``/``avdmanager`` toolchain.  ``prefer="androidcli"`` honours
    an explicit request but still degrades to legacy (recording
    ``fallback_reason``) when the CLI is missing/unsupported, so an installer
    can always make progress.  ``prefer="legacy"`` is honoured unconditionally.

    The returned object exposes ``.name`` and ``.fallback_reason``.
    """
    from .androidcli.backend import AndroidCliBackend
    from .legacy.backend import LegacySdkBackend

    key = (prefer or "auto").strip().lower()
    if key not in _AUTO_ALIASES | _ANDROIDCLI_ALIASES | _LEGACY_ALIASES:
        raise ConfigError(
            f"unknown SDK backend preference: {prefer!r}",
            hint="Use 'auto', 'androidcli' or 'legacy'.",
        )
    paths = resolve_sdk_paths(config, host)
    legacy = LegacySdkBackend(config, paths, host=host)
    if key in _LEGACY_ALIASES:
        return legacy
    androidcli = AndroidCliBackend(config, paths, host=host)
    if key in _ANDROIDCLI_ALIASES:
        if androidcli.available():
            return androidcli
        legacy.fallback_reason = androidcli.unavailable_reason or "android CLI is unavailable"
        return legacy
    if androidcli.available():
        return androidcli
    legacy.fallback_reason = androidcli.unavailable_reason or "android CLI is unavailable"
    return legacy


__all__ = [
    "AvdEntry",
    "BaseSdkBackend",
    "CUSTOM_HARDWARE_REPLY",
    "DEFAULT_TIMEOUT_S",
    "INSTALL_TIMEOUT_S",
    "LICENSE_PROMPT_REPLY",
    "LICENSE_TIMEOUT_S",
    "LIST_TIMEOUT_S",
    "MIN_ANDROIDCLI_VERSION",
    "Runner",
    "SdkError",
    "ToolNotFoundError",
    "ToolResolver",
    "ToolResult",
    "VERSION_TIMEOUT_S",
    "build_environment",
    "build_system_image",
    "default_avd_home",
    "default_device_profiles",
    "failed_command_error",
    "get_backend",
    "image_display_name",
    "image_kind_for_tag",
    "images_from_packages",
    "merge_system_images",
    "parse_avd_entries",
    "parse_based_on",
    "parse_device_profiles",
    "parse_image_package_id",
    "parse_key_value_blocks",
    "parse_package_table",
    "parse_version",
    "parse_version_tuple",
    "read_ini_file",
    "require_ok",
    "resolve_sdk_paths",
    "run_tool",
    "scan_installed_images",
    "sdk_bin_dirs",
    "sort_system_images",
    "target_api_level",
    "update_ini_file",
    "version_at_least",
]
