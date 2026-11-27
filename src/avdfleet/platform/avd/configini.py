"""Typed parsing and rendering of the Android emulator's ``config.ini``.

An AVD is a pair of INI files: ``<name>.ini`` (the AVD manager's pointer) and
``<name>.avd/config.ini`` (the hardware profile).  Like a Java ``Properties``
file, ``config.ini`` has no sections, only ``key=value`` lines, ``#``/``;``
comments and blank lines.  This module is the single place that reads, validates,
edits and writes that file for the rest of avdfleet.

Design rules:

* user content is preserved: comments, ordering and keys avdfleet does not know
  about survive an edit (the emulator understands dozens of keys we must not
  drop);
* known keys are typed: RAM/CPU/heap are bounded integers, GPU mode and ABI are
  enumerations, ``PlayStore.enabled`` is a boolean, ``disk.dataPartition.size``
  is a size literal, paths/ids are format checked;
* writes are atomic and a ``.bak`` copy is kept so a bad edit is recoverable;
* values are quoted only when they need to be (leading/trailing whitespace or
  embedded quotes/backslashes).  Newlines and NUL bytes are always rejected so a
  value can never inject a second line;
* nothing here imports another avdfleet subsystem - the store, API and runtime
  all use these helpers.

The module is intentionally tolerant on *read* (a hand-edited file yields
``ConfigIniIssue`` records rather than an exception, except when the caller asks
for strict reading) and strict on *write* (invalid typed values raise
:class:`ConfigValidationError` before anything touches the disk).
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from avdfleet.core.errors import InstanceError, ValidationError
from avdfleet.core.models import GpuMode, Template

CONFIG_FILENAME = "config.ini"
BACKUP_SUFFIX = ".bak"

MIN_RAM_MB = 512
MAX_RAM_MB = 65536
MIN_HEAP_MB = 16
MAX_HEAP_MB = 4096
MIN_CPU_CORES = 1
MAX_CPU_CORES = 16
MIN_LCD_DIMENSION = 240
MAX_LCD_DIMENSION = 8192
MIN_LCD_DENSITY = 120
MAX_LCD_DENSITY = 1000
DEFAULT_DATA_PARTITION = "6G"

#: Keys a newly rendered AVD profile must contain.
REQUIRED_CREATE_KEYS: tuple[str, ...] = ("image.sysdir.1", "tag.id", "abi.type")

#: Keys that hold bucket credentials or other secrets must never be written to
#: ``config.ini``; the config file is world-readable inside the AVD directory.
SECRET_KEY_MARKERS: tuple[str, ...] = (
    "password",
    "passwd",
    "token",
    "secret",
    "api_key",
    "apikey",
    "credential",
)

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_INT_RE = re.compile(r"^-?\d+$")
_SIZE_RE = re.compile(r"^(\d+)\s*([KMGTP]?)B?$", re.IGNORECASE)
_ABI_CHOICES = ("x86", "x86_64", "arm64-v8a", "armeabi-v7a", "arm64")
_ABI_NORMALISE = {"arm64": "arm64-v8a", "arm64-v8": "arm64-v8a"}
_CPU_ARCH_CHOICES = ("x86", "x86_64", "arm", "arm64", "arm64-v8a")
_GPU_CHOICES = tuple(mode.value for mode in GpuMode)
_BOOL_VALUES = {
    "true": "true",
    "yes": "yes",
    "1": "true",
    "on": "true",
    "false": "false",
    "no": "no",
    "0": "false",
    "off": "false",
}
_SAFE_DEVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,63}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_IMAGE_SYSDIR_RE = re.compile(r"^[^\r\n\x00]+$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConfigIniError(InstanceError):
    """The AVD config.ini could not be read or written."""

    code = "config_ini_error"


class ConfigFileError(ConfigIniError):
    """The AVD config.ini is missing or unreadable/unwritable."""

    code = "config_file_error"


class ConfigValidationError(ValidationError):
    """One or more config.ini keys have an invalid type or value."""

    code = "config_validation_error"

    def __init__(self, errors: Iterable[str], *, hint: str | None = None) -> None:
        messages = [str(error) for error in errors]
        super().__init__(
            "; ".join(messages) or "invalid AVD configuration",
            hint=hint or "Fix the highlighted config.ini values and retry.",
            details={"errors": messages},
        )
        self.errors = messages


# ---------------------------------------------------------------------------
# Issues (tolerant read diagnostics)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigIniIssue:
    """A non-fatal (or fatal) problem found while inspecting a config.ini."""

    severity: str  # "error" | "warning"
    message: str
    key: str = ""
    line: int = 0

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def as_text(self) -> str:
        location = f"line {self.line}: " if self.line else ""
        key = f"{self.key}: " if self.key else ""
        return f"{location}{key}{self.message}"


# ---------------------------------------------------------------------------
# Validation specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _KeySpec:
    """Type/range description for one well-known ``config.ini`` key."""

    kind: str
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()
    default: str | None = None


KNOWN_CONFIG_KEYS: dict[str, _KeySpec] = {
    "hw.ramSize": _KeySpec("int", MIN_RAM_MB, MAX_RAM_MB),
    "hw.cpu.ncore": _KeySpec("int", MIN_CPU_CORES, MAX_CPU_CORES),
    "vm.heapSize": _KeySpec("int", MIN_HEAP_MB, MAX_HEAP_MB),
    "hw.lcd.width": _KeySpec("int", MIN_LCD_DIMENSION, MAX_LCD_DIMENSION),
    "hw.lcd.height": _KeySpec("int", MIN_LCD_DIMENSION, MAX_LCD_DIMENSION),
    "hw.lcd.density": _KeySpec("int", MIN_LCD_DENSITY, MAX_LCD_DENSITY),
    "disk.dataPartition.size": _KeySpec("size"),
    "hw.gpu.mode": _KeySpec("choice", choices=_GPU_CHOICES),
    "abi.type": _KeySpec("abi"),
    "hw.cpu.arch": _KeySpec("cpu_arch"),
    "hw.device.name": _KeySpec("device_name"),
    "image.sysdir.1": _KeySpec("sysdir"),
    "tag.id": _KeySpec("tag"),
    "tag.display": _KeySpec("text"),
    "PlayStore.enabled": _KeySpec("bool"),
    "fastboot.forceColdBoot": _KeySpec("bool"),
    "fastboot.forceFastBoot": _KeySpec("bool"),
    "avd.ini.displayname": _KeySpec("text"),
}


# ---------------------------------------------------------------------------
# Value quoting and normalisation
# ---------------------------------------------------------------------------


def is_secret_key(key: str) -> bool:
    """True when *key* looks like it would carry a credential."""
    lowered = (key or "").strip().lower().replace("-", "_")
    return any(marker in lowered for marker in SECRET_KEY_MARKERS)


def quote_value(value: str) -> str:
    """Return a single-line representation of *value*.

    Values are written verbatim unless they have leading/trailing whitespace that
    would otherwise be lost.  Backslashes and embedded quotes are **not** escaped:
    ``config.ini`` values are raw strings (Windows paths must survive unchanged),
    not Java properties with backslash escapes.
    """
    text = str(value)
    if "\n" in text or "\r" in text or "\x00" in text:
        raise ConfigValidationError(["config values must not contain newlines or NUL bytes"])
    needs_quotes = text != text.strip() and '"' not in text and "\\" not in text
    return f'"{text}"' if needs_quotes else text


def unquote_value(value: str) -> str:
    """Inverse of :func:`quote_value` (tolerant of a missing closing quote)."""
    text = value
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        inner = text[1:-1]
        if '"' not in inner and "\\" not in inner:
            return inner
    return text


def _validate_key(key: str) -> str:
    candidate = (key or "").strip()
    if not candidate:
        raise ConfigValidationError(["config key must not be empty"])
    if not _KEY_RE.match(candidate):
        raise ConfigValidationError(
            [f"invalid config key {candidate!r}: use letters, digits, '.', '_' and '-'"]
        )
    return candidate


def _normalise_int(key: str, value: str, spec: _KeySpec) -> str:
    text = value.strip()
    if not _INT_RE.match(text):
        raise ConfigValidationError([f"{key} must be an integer (got {value!r})"])
    number = int(text)
    if spec.minimum is not None and number < spec.minimum:
        raise ConfigValidationError([f"{key} must be >= {spec.minimum} (got {number})"])
    if spec.maximum is not None and number > spec.maximum:
        raise ConfigValidationError([f"{key} must be <= {spec.maximum} (got {number})"])
    return str(number)


def _normalise_size(key: str, value: str) -> str:
    text = value.strip().upper().replace(" ", "")
    match = _SIZE_RE.match(text)
    if match is None:
        raise ConfigValidationError(
            [f"{key} must be a size literal such as 6G or 2048M (got {value!r})"]
        )
    number, suffix = match.groups()
    if int(number) <= 0:
        raise ConfigValidationError([f"{key} must be greater than zero"])
    return f"{int(number)}{suffix}"


def _normalise_choice(key: str, value: str, choices: Iterable[str]) -> str:
    text = value.strip().lower()
    allowed = tuple(choices)
    if text not in allowed:
        raise ConfigValidationError(
            [f"{key} must be one of {', '.join(allowed)} (got {value!r})"]
        )
    return text


def _normalise_bool(key: str, value: str) -> str:
    text = value.strip().lower()
    if text not in _BOOL_VALUES:
        raise ConfigValidationError(
            [f"{key} must be a boolean (true/false, yes/no, 1/0; got {value!r})"]
        )
    return _BOOL_VALUES[text]


def _normalise_abi(key: str, value: str) -> str:
    text = value.strip().lower()
    if text not in _ABI_CHOICES:
        raise ConfigValidationError(
            [f"{key} must be one of {', '.join(_ABI_CHOICES)} (got {value!r})"]
        )
    return _ABI_NORMALISE.get(text, text)


def _normalise_cpu_arch(key: str, value: str) -> str:
    text = value.strip().lower()
    if text not in _CPU_ARCH_CHOICES:
        raise ConfigValidationError(
            [f"{key} must be one of {', '.join(_CPU_ARCH_CHOICES)} (got {value!r})"]
        )
    return text


def _normalise_device_name(key: str, value: str) -> str:
    text = value.strip()
    if not _SAFE_DEVICE_RE.match(text):
        raise ConfigValidationError(
            [f"{key} must be a device profile id or name (got {value!r})"]
        )
    return text


def _normalise_sysdir(key: str, value: str) -> str:
    text = value.strip()
    if not text or not _IMAGE_SYSDIR_RE.match(text):
        raise ConfigValidationError([f"{key} must be a non-empty system-image path"])
    normalised_parts = text.replace("\\", "/").split("/")
    if ".." in normalised_parts:
        raise ConfigValidationError([f"{key} must not contain '..' path segments"])
    return text


def _normalise_tag(key: str, value: str) -> str:
    text = value.strip()
    if not _TAG_RE.match(text):
        raise ConfigValidationError(
            [f"{key} must be a tag id (letters, digits, '.', '_', '-'; got {value!r})"]
        )
    return text


def normalise_value(key: str, value: str) -> str:
    """Validate and normalise one ``key=value`` pair for a ``config.ini``.

    Unknown keys are accepted (the emulator keeps many undocumented settings) but
    always have to be single-line, non-secret text.  Known keys are type checked.
    """
    key = _validate_key(key)
    if is_secret_key(key):
        raise ConfigValidationError(
            [f"refusing to store a credential in config.ini: {key!r}"],
            hint="Proxy credentials belong in the encrypted vault, never in an AVD config file.",
        )
    text = str(value)
    spec = KNOWN_CONFIG_KEYS.get(key)
    if spec is None:
        if "\n" in text or "\r" in text or "\x00" in text:
            raise ConfigValidationError([f"{key} must not contain newlines or NUL bytes"])
        return text.strip()
    if spec.kind == "int":
        return _normalise_int(key, text, spec)
    if spec.kind == "size":
        return _normalise_size(key, text)
    if spec.kind == "choice":
        return _normalise_choice(key, text, spec.choices)
    if spec.kind == "bool":
        return _normalise_bool(key, text)
    if spec.kind == "abi":
        return _normalise_abi(key, text)
    if spec.kind == "cpu_arch":
        return _normalise_cpu_arch(key, text)
    if spec.kind == "device_name":
        return _normalise_device_name(key, text)
    if spec.kind == "sysdir":
        return _normalise_sysdir(key, text)
    if spec.kind == "tag":
        return _normalise_tag(key, text)
    # Plain text (e.g. tag.display, avd.ini.displayname): preserve meaningful
    # whitespace; only control characters are forbidden.
    if "\n" in text or "\r" in text or "\x00" in text:
        raise ConfigValidationError([f"{key} must not contain newlines or NUL bytes"])
    return text


def validate_config_values(values: Mapping[str, Any]) -> dict[str, str]:
    """Validate/normalise a mapping of config keys; raises on the first batch of errors.

    All errors are collected before raising so ``update_config`` can report every
    bad key at once.
    """
    errors: list[str] = []
    normalised: dict[str, str] = {}
    for key, value in values.items():
        try:
            normalised[_validate_key(str(key))] = normalise_value(str(key), str(value))
        except ConfigValidationError as exc:
            errors.extend(exc.errors)
    if errors:
        raise ConfigValidationError(errors)
    return normalised


def format_line(key: str, value: str) -> str:
    """Render one canonical ``key=value`` line."""
    return f"{_validate_key(key)}={quote_value(value)}"


# ---------------------------------------------------------------------------
# ConfigIni document
# ---------------------------------------------------------------------------


@dataclass
class _Entry:
    key: str
    value: str


@dataclass
class _Raw:
    text: str


@dataclass
class ConfigIni:
    """A ``config.ini`` document that preserves comments and unknown keys.

    Lines are stored in order; ``_Entry`` lines are editable, ``_Raw`` lines are
    comments/blank/corrupt lines that are re-emitted verbatim.
    """

    lines: list[_Entry | _Raw] = field(default_factory=list)
    source: str = "<memory>"

    # -- constructors ---------------------------------------------------
    @classmethod
    def parse(cls, text: str, *, source: str = "<memory>") -> ConfigIni:
        document = cls(source=source)
        for raw_line in (text or "").splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith(("#", ";")):
                document.lines.append(_Raw(raw_line))
                continue
            if "=" not in stripped:
                document.lines.append(_Raw(raw_line))
                continue
            key, _, raw_value = stripped.partition("=")
            key = key.strip()
            if not key:
                document.lines.append(_Raw(raw_line))
                continue
            document.lines.append(_Entry(key=key, value=unquote_value(raw_value.strip())))
        return document

    @classmethod
    def from_values(cls, values: Mapping[str, Any], *, source: str = "<values>") -> ConfigIni:
        document = cls(source=source)
        for key, value in validate_config_values(values).items():
            document.lines.append(_Entry(key=key, value=value))
        return document

    @classmethod
    def read(cls, path: str | Path, *, strict: bool = True) -> ConfigIni:
        target = Path(path)
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError as exc:
            if strict:
                raise ConfigFileError(
                    f"AVD configuration file is missing: {target}",
                    hint="Recreate the instance or regenerate the AVD with avdfleet.",
                ) from exc
            return cls(source=str(target))
        except OSError as exc:
            raise ConfigFileError(
                f"could not read AVD configuration {target}: {exc}",
                hint="Check file permissions and that the AVD directory is writable.",
            ) from exc
        return cls.parse(text, source=str(target))

    # -- queries --------------------------------------------------------
    def entries(self) -> list[tuple[str, str]]:
        return [(line.key, line.value) for line in self.lines if isinstance(line, _Entry)]

    def keys(self) -> list[str]:
        return [key for key, _ in self.entries()]

    def comments(self) -> list[str]:
        return [line.text for line in self.lines if isinstance(line, _Raw)]

    def get(self, key: str, default: str | None = None) -> str | None:
        values = self.to_dict()
        return values.get(key, default)

    def to_dict(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in self.lines:
            if isinstance(line, _Entry):
                values[line.key] = line.value
        return values

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key in self.to_dict()

    def __getitem__(self, key: str) -> str:
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def issues(self, *, required_keys: Iterable[str] = ()) -> list[ConfigIniIssue]:
        """Return structural + typed-value problems without raising."""
        issues: list[ConfigIniIssue] = []
        seen: dict[str, int] = {}
        for index, line in enumerate(self.lines, start=1):
            if isinstance(line, _Raw):
                stripped = line.text.strip()
                if not stripped or stripped.startswith(("#", ";")):
                    continue
                if "=" not in stripped:
                    issues.append(
                        ConfigIniIssue("error", "malformed line (expected key=value)", line=index)
                    )
                elif not stripped.partition("=")[0].strip():
                    issues.append(
                        ConfigIniIssue("error", "malformed line (empty key)", line=index)
                    )
                continue
            if line.key in seen:
                issues.append(
                    ConfigIniIssue(
                        "warning",
                        f"duplicate key (first seen on line {seen[line.key]})",
                        key=line.key,
                        line=index,
                    )
                )
            else:
                seen[line.key] = index
            try:
                normalise_value(line.key, line.value)
            except ConfigValidationError as exc:
                for message in exc.errors:
                    issues.append(ConfigIniIssue("error", message, key=line.key, line=index))
        for key in required_keys:
            if key not in seen:
                issues.append(ConfigIniIssue("error", "required key is missing", key=key))
        return issues

    # -- mutation -------------------------------------------------------
    def set(self, key: str, value: str, *, validate: bool = True) -> ConfigIni:
        resolved_key = _validate_key(key)
        resolved_value = normalise_value(resolved_key, value) if validate else str(value)
        for line in self.lines:
            if isinstance(line, _Entry) and line.key == resolved_key:
                line.value = resolved_value
                return self
        self.lines.append(_Entry(key=resolved_key, value=resolved_value))
        return self

    def remove(self, key: str) -> bool:
        before = len(self.lines)
        self.lines = [
            line for line in self.lines if not (isinstance(line, _Entry) and line.key == key)
        ]
        return len(self.lines) != before

    def update(self, values: Mapping[str, Any]) -> ConfigIni:
        for key, value in validate_config_values(values).items():
            self.set(key, value, validate=False)
        self.deduplicate()
        return self

    def deduplicate(self) -> ConfigIni:
        """Keep only the first occurrence of each key (comments are preserved)."""
        seen: set[str] = set()
        kept: list[_Entry | _Raw] = []
        for line in self.lines:
            if isinstance(line, _Entry):
                if line.key in seen:
                    continue
                seen.add(line.key)
            kept.append(line)
        self.lines = kept
        return self

    # -- serialisation --------------------------------------------------
    def render(self) -> str:
        output: list[str] = []
        for line in self.lines:
            if isinstance(line, _Entry):
                output.append(format_line(line.key, line.value))
            else:
                output.append(line.text)
        text = "\n".join(output)
        if text and not text.endswith("\n"):
            text += "\n"
        return text

    def write(
        self,
        path: str | Path,
        *,
        backup: bool = True,
        dry_run: bool = False,
    ) -> Path:
        """Atomically write the document (with a ``.bak`` copy when it existed)."""
        target = Path(path)
        if dry_run:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        if backup and target.exists():
            try:
                shutil.copy2(target, target.with_name(target.name + BACKUP_SUFFIX))
            except OSError as exc:
                raise ConfigFileError(
                    f"could not back up AVD configuration {target}: {exc}"
                ) from exc
        temporary = target.with_name(f"{target.name}.tmp-{os.getpid()}")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(self.render())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ConfigFileError(
                f"could not write AVD configuration {target}: {exc}",
                hint="Check that the AVD home is writable and not on a read-only path.",
            ) from exc
        return target

    def __len__(self) -> int:
        return len(self.lines)


# ---------------------------------------------------------------------------
# File helpers used by the AVD manager and the runtime
# ---------------------------------------------------------------------------


def config_path_for(avd_home: str | Path, name: str) -> Path:
    """``<avd_home>/<name>.avd/config.ini``."""
    return Path(avd_home) / f"{name}.avd" / CONFIG_FILENAME


def ini_path_for(avd_home: str | Path, name: str) -> Path:
    """``<avd_home>/<name>.ini``."""
    return Path(avd_home) / f"{name}.ini"


def avd_dir_for(avd_home: str | Path, name: str) -> Path:
    """``<avd_home>/<name>.avd``."""
    return Path(avd_home) / f"{name}.avd"


def load_config(path: str | Path, *, strict: bool = False) -> ConfigIni:
    """Read a config.ini, returning an empty document when it is missing."""
    return ConfigIni.read(path, strict=strict)


def load_values(path: str | Path) -> dict[str, str]:
    """Tolerant ``path -> {key: value}`` used for status/list operations."""
    return ConfigIni.read(path, strict=False).to_dict()


def read_config(path: str | Path) -> ConfigIni:
    """Strict read: raises :class:`ConfigFileError` when the file is absent."""
    return ConfigIni.read(path, strict=True)


def detect_config_issues(
    path: str | Path,
    *,
    required_keys: Iterable[str] = (),
    require_file: bool = True,
) -> list[ConfigIniIssue]:
    """Inspect an AVD config file on disk and report missing/corrupt content."""
    target = Path(path)
    if not target.exists():
        if require_file:
            return [ConfigIniIssue("error", f"configuration file is missing: {target}")]
        return []
    try:
        document = ConfigIni.read(target, strict=False)
    except ConfigFileError as exc:
        return [ConfigIniIssue("error", str(exc))]
    return document.issues(required_keys=required_keys)


def apply_config_overrides(
    path: str | Path,
    values: Mapping[str, Any],
    *,
    dry_run: bool = False,
    create_if_missing: bool = False,
    backup: bool = True,
    required_keys: Iterable[str] = (),
) -> ConfigIni:
    """Validate and merge *values* into an existing AVD config.ini.

    ``create_if_missing`` is used right after ``avdmanager create`` in case the
    tool produced no file (some custom images do); the normal edit path requires
    the file so a typo cannot silently create a brand-new AVD profile.  The whole
    batch is validated before the file is read/written.
    """
    normalised = validate_config_values(values)
    target = Path(path)
    if target.exists():
        document = ConfigIni.read(target, strict=True)
    elif create_if_missing:
        document = ConfigIni(source=str(target))
    else:
        missing = [key for key in required_keys if key not in normalised]
        if missing:
            raise ConfigFileError(
                f"AVD configuration file is missing: {target}",
                hint="Recreate the instance so the emulator tooling writes config.ini.",
            )
        raise ConfigFileError(
            f"AVD configuration file is missing: {target}",
            hint="The AVD was not created correctly; recreate it from the template.",
        )
    document.update(normalised)
    if not dry_run:
        document.write(target, backup=backup)
    return document


# ---------------------------------------------------------------------------
# Rendering from a template
# ---------------------------------------------------------------------------


def image_sysdir(image_id: str, *, local_path: str | None = None) -> str:
    """Return the ``image.sysdir.1`` value for a system-image id/path.

    Stock Android images are SDK-relative (``system-images/android-35/...``);
    custom imported images may live outside the SDK, in which case the absolute
    local path is used.
    """
    candidate = (image_id or "").strip().replace("\\", "/")
    if candidate.startswith("system-images;"):
        parts = candidate.split(";")
        return "system-images/" + "/".join(parts[1:]) + "/"
    if local_path:
        return str(local_path).replace("\\", "/")
    return candidate


def abi_for_image(image_id: str, *, fallback: str = "x86_64") -> str:
    """Best-effort ABI from a ``system-images;<target>;<tag>;<abi>`` package id."""
    parts = (image_id or "").split(";")
    if len(parts) == 4 and parts[0] == "system-images":
        return _ABI_NORMALISE.get(parts[3].lower(), parts[3].lower())
    return fallback


def tag_for_image(image_id: str, *, fallback: str = "default") -> str:
    """Best-effort tag id from a system-image package id."""
    parts = (image_id or "").split(";")
    if len(parts) == 4 and parts[0] == "system-images":
        return parts[2]
    return fallback


def is_playstore_image(image: Any) -> bool:
    """Whether a :class:`SystemImage`-like object represents a Play Store image."""
    if image is None:
        return False
    kind = getattr(image, "kind", None)
    kind_value = getattr(kind, "value", kind)
    if str(kind_value) == "google_apis_playstore":
        return True
    return "playstore" in str(getattr(image, "tag", "")).lower()


def render_template_config(
    template: Template,
    *,
    image: Any | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Build the typed ``config.ini`` mapping for a new instance of *template*.

    The template's free-form ``config`` mapping is the base; typed fields and the
    resolved system image override it.  ``extra`` supplies per-instance overrides
    from ``create_instance``/``bulk_deploy``.
    """
    values: dict[str, str] = {str(key): str(value) for key, value in template.config.items()}
    values.update(
        {
            "image.sysdir.1": image_sysdir(
                template.image_id,
                local_path=getattr(image, "local_path", None),
            ),
            "tag.id": tag_for_image(template.image_id),
            "abi.type": abi_for_image(template.image_id),
            "hw.device.name": template.device_id,
            "hw.ramSize": str(template.ram_mb),
            "vm.heapSize": str(template.heap_mb),
            "hw.cpu.ncore": str(template.cpu_cores),
            "hw.gpu.mode": str(getattr(template.gpu_mode, "value", template.gpu_mode)),
            "hw.cpu.arch": "arm64" if abi_for_image(template.image_id) == "arm64-v8a" else "x86_64",
        }
    )
    if image is not None:
        values["tag.id"] = str(getattr(image, "tag", "") or values["tag.id"])
        values["abi.type"] = str(getattr(image, "abi", "") or values["abi.type"])
        values.setdefault("PlayStore.enabled", "true" if is_playstore_image(image) else "false")
        if getattr(image, "local_path", None) and not str(image.id).startswith("system-images;"):
            values["image.sysdir.1"] = image_sysdir(str(image.id), local_path=image.local_path)
    if template.resolution:
        width, _, height = template.resolution.lower().partition("x")
        if width.strip().isdigit():
            values["hw.lcd.width"] = width.strip()
        if height.strip().isdigit():
            values["hw.lcd.height"] = height.strip()
    if template.density is not None:
        values["hw.lcd.density"] = str(template.density)
    values.setdefault("disk.dataPartition.size", DEFAULT_DATA_PARTITION)
    if extra:
        values.update({str(key): str(value) for key, value in extra.items()})
    return validate_config_values(values)


__all__ = [
    "BACKUP_SUFFIX",
    "CONFIG_FILENAME",
    "ConfigFileError",
    "ConfigIni",
    "ConfigIniError",
    "ConfigIniIssue",
    "ConfigValidationError",
    "DEFAULT_DATA_PARTITION",
    "KNOWN_CONFIG_KEYS",
    "MAX_CPU_CORES",
    "MAX_HEAP_MB",
    "MAX_RAM_MB",
    "MIN_CPU_CORES",
    "MIN_HEAP_MB",
    "MIN_RAM_MB",
    "REQUIRED_CREATE_KEYS",
    "SECRET_KEY_MARKERS",
    "abi_for_image",
    "apply_config_overrides",
    "avd_dir_for",
    "config_path_for",
    "detect_config_issues",
    "format_line",
    "image_sysdir",
    "ini_path_for",
    "is_playstore_image",
    "is_secret_key",
    "load_config",
    "load_values",
    "normalise_value",
    "quote_value",
    "read_config",
    "render_template_config",
    "tag_for_image",
    "unquote_value",
    "validate_config_values",
]
