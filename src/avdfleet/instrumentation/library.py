"""The central module library: import once, deploy to many AVDs.

Three package formats are handled and deliberately kept apart:

* **Xposed APK** - an Android app. Its Xposed nature is proven by markers inside
  the archive (legacy ``assets/xposed_init`` or the modern ``META-INF/xposed/``
  entries and manifest meta-data), never by the file extension.
* **Magisk ZIP** - a module archive with ``module.prop`` at its root.
* **Zygisk / LSPosed ZIP** - Magisk modules with Zygisk payloads; LSPosed is
  recognised by its module id, because it *is* a Magisk module.

Importing a package never activates it and never executes it: the archive is
hashed, inspected as inert bytes and stored.
"""

from __future__ import annotations

import hashlib
import io
import re
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError
from ..core.logging import get_logger
from ..core.models import utcnow
from .capabilities import classify_magisk_module, parse_module_prop
from .models import ModuleKind, ModuleRecord

logger = get_logger("avdfleet.instrumentation.library")

#: Hard ceiling for one imported artifact (compressed bytes).
MAX_MODULE_BYTES = 256 * 1024 * 1024
#: Entries larger than this inside an APK/ZIP are not read for markers.
_MAX_ENTRY_SCAN = 4096

_APK_MARKERS = (
    "assets/xposed_init",
    "meta-inf/xposed/module.prop",
    "meta-inf/xposed/scope.list",
)

_VERSION_CODE = re.compile(r"versionCode='(\d+)'")
_VERSION_NAME = re.compile(r"versionName='([^']*)'")
_PACKAGE_NAME = re.compile(r"package: name='([^']*)'")
_LABEL = re.compile(r"application-label:'([^']*)'")
_SDK_VERSION = re.compile(r"sdkVersion:'(\d+)'")


@dataclass
class AaptInfo:
    """The subset of ``aapt2 dump badging`` output the library needs."""

    package_name: str = ""
    version: str = ""
    version_code: int = 0
    label: str = ""
    min_sdk: int = 0
    raw: str = ""


def parse_badging(text: str) -> AaptInfo:
    """Parse ``aapt2 dump badging`` output.

    Kept as a pure function so it can be unit-tested without the SDK.
    """
    info = AaptInfo(raw=text or "")
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("package:"):
            match = _PACKAGE_NAME.search(stripped)
            if match:
                info.package_name = match.group(1)
            match = _VERSION_CODE.search(stripped)
            if match:
                info.version_code = int(match.group(1))
            match = _VERSION_NAME.search(stripped)
            if match:
                info.version = match.group(1)
        elif stripped.startswith("application-label:"):
            match = _LABEL.search(stripped)
            if match:
                info.label = match.group(1)
        elif stripped.startswith("sdkVersion:"):
            match = _SDK_VERSION.search(stripped)
            if match:
                info.min_sdk = int(match.group(1))
    return info


def find_aapt2(sdk_root: str | Path | None) -> str | None:
    """Locate the newest ``aapt2`` (or ``aapt``) under ``build-tools``."""
    if not sdk_root:
        return None
    build_tools = Path(sdk_root) / "build-tools"
    if not build_tools.is_dir():
        return None
    candidates: list[tuple[tuple[int, ...], Path]] = []
    for entry in build_tools.iterdir():
        if not entry.is_dir():
            continue
        for name in ("aapt2.exe", "aapt2", "aapt.exe", "aapt"):
            candidate = entry / name
            if candidate.is_file():
                digits = tuple(int(part) for part in re.findall(r"\d+", entry.name))
                candidates.append((digits, candidate))
                break
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return str(candidates[0][1])


#: Magisk module ids are directory names: letters, digits, dot, underscore, dash.
_MODULE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _safe_url(url: str) -> str:
    """Strip credentials and query values before a URL becomes provenance.

    ``source_url`` is persisted and served by the API, so a credentialed or
    signed URL must not be stored verbatim.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    query = "&".join(
        f"{item.split('=', 1)[0]}=<redacted>" for item in parts.query.split("&") if item
    )
    return urlunsplit((parts.scheme, host, parts.path, query, ""))


class ModuleLibrary:
    """Import, inspect and store instrumentation packages."""

    def __init__(
        self,
        root: Path,
        *,
        aapt_path: str | None = None,
        http_get: Any | None = None,
        allow_insecure_urls: bool = False,
    ) -> None:
        self._root = Path(root)
        self._aapt = aapt_path
        self._http_get = http_get
        self._allow_insecure = bool(allow_insecure_urls)

    # -- storage ---------------------------------------------------------
    @property
    def root(self) -> Path:
        return self._root

    def _store(self, data: bytes, filename: str, sha256: str) -> Path:
        target_dir = self._root / sha256
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_filename(filename) or "module.bin"
        target = target_dir / safe_name
        if not target.exists():
            target.write_bytes(data)
        return target

    def delete_file(self, record: ModuleRecord) -> None:
        """Remove the stored artifact (the AVD copies are unaffected)."""
        path = Path(record.file_path)
        try:
            if path.is_file():
                path.unlink()
            parent = path.parent
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError as exc:  # pragma: no cover - filesystem dependent
            logger.warning("could not delete module file %s: %s", path, exc)

    # -- inspection ------------------------------------------------------
    def inspect(self, data: bytes, filename: str) -> tuple[ModuleKind, dict[str, Any]]:
        """Classify *data* and extract whatever metadata the format exposes.

        Raises :class:`ValidationError` when the bytes are neither a readable ZIP
        (APK) nor a Magisk module archive.
        """
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ValidationError(
                f"{filename} is not a ZIP/APK archive",
                hint="import a .apk (Xposed module) or a .zip (Magisk/Zygisk module)",
            ) from exc

        with archive:
            names = [info.filename for info in archive.infolist() if not info.is_dir()]
            if len(names) > _MAX_ENTRY_SCAN:
                raise ValidationError(
                    f"{filename} contains too many entries ({len(names)})",
                    hint="that is not a module archive this build can inspect",
                )
            lowered = {name.lower(): name for name in names}
            metadata: dict[str, Any] = {"entries": len(names)}

            if "module.prop" in lowered:
                with archive.open(lowered["module.prop"]) as handle:
                    fields = parse_module_prop(handle.read(4096).decode("utf-8", "replace"))
                metadata["module_prop"] = fields
                has_zygisk = any(
                    name.lower().startswith(("zygisk/", "system/bin/")) for name in names
                )
                kind = classify_magisk_module(
                    fields.get("id", ""), fields, has_zygisk=has_zygisk
                )
                metadata["zygisk_payload"] = has_zygisk
                return kind, metadata

            if "androidmanifest.xml" in lowered:
                markers = [marker for marker in _APK_MARKERS if marker in lowered]
                xposed_entries = [
                    name for name in names if name.lower().startswith("meta-inf/xposed/")
                ]
                metadata["xposed_markers"] = markers + sorted(set(xposed_entries))
                if not markers and not xposed_entries:
                    # Modern modules declare <meta-data android:name="xposedmodule">.
                    if self._declares_xposed_meta(data):
                        metadata["xposed_meta_data"] = True
                    else:
                        metadata["note"] = (
                            "no Xposed markers found; this is an ordinary APK, so it"
                            " cannot be installed as an Xposed module"
                        )
                        return ModuleKind.UNKNOWN, metadata
                info = self._badging(data)
                if info is not None:
                    metadata["badging"] = {
                        "package_name": info.package_name,
                        "version": info.version,
                        "version_code": info.version_code,
                        "label": info.label,
                        "min_sdk": info.min_sdk,
                    }
                return ModuleKind.XPOSED_APK, metadata

        raise ValidationError(
            f"{filename} is neither an APK nor a Magisk module",
            hint="expected AndroidManifest.xml (APK) or module.prop (Magisk module)",
        )

    def _declares_xposed_meta(self, data: bytes) -> bool:
        """Whether the APK's manifest declares the ``xposedmodule`` meta-data."""
        if not self._aapt:
            return False
        import os
        import tempfile

        descriptor, name = tempfile.mkstemp(suffix=".apk")
        os.close(descriptor)
        temporary = Path(name)
        try:
            temporary.write_bytes(data)
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [
                    self._aapt,
                    "dump",
                    "xmltree",
                    "--file",
                    "AndroidManifest.xml",
                    str(temporary),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("aapt2 could not read the manifest: %s", exc)
            return False
        finally:
            temporary.unlink(missing_ok=True)
        if completed.returncode != 0:
            return False
        return "xposedmodule" in completed.stdout.lower()

    def _badging(self, data: bytes) -> AaptInfo | None:
        """Run ``aapt2 dump badging`` on the APK bytes, when aapt is available."""
        if not self._aapt:
            return None
        import os
        import tempfile

        descriptor, name = tempfile.mkstemp(suffix=".apk")
        # Close the descriptor first: an open handle makes the later unlink
        # fail on Windows (WinError 32) and the import then 500s.
        os.close(descriptor)
        temporary = Path(name)
        try:
            temporary.write_bytes(data)
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [self._aapt, "dump", "badging", str(temporary)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("aapt2 could not read the APK: %s", exc)
            return None
        finally:
            temporary.unlink(missing_ok=True)
        if completed.returncode != 0:
            logger.warning("aapt2 exited %s: %s", completed.returncode, completed.stderr[:400])
            return None
        return parse_badging(completed.stdout)

    # -- import ----------------------------------------------------------
    def build_record(
        self,
        data: bytes,
        filename: str,
        *,
        source: str = "upload",
        source_url: str = "",
    ) -> ModuleRecord:
        """Turn raw bytes into a library record (does not persist it)."""
        if not data:
            raise ValidationError(f"{filename} is empty")
        if len(data) > MAX_MODULE_BYTES:
            raise ValidationError(
                f"{filename} is larger than the {MAX_MODULE_BYTES // (1024 * 1024)} MiB limit"
            )
        sha256 = hashlib.sha256(data).hexdigest()
        kind, metadata = self.inspect(data, filename)
        badging = metadata.get("badging") or {}
        module_prop = metadata.get("module_prop") or {}
        record = ModuleRecord(
            id=f"mod_{sha256[:16]}",
            kind=kind,
            name=badging.get("label") or module_prop.get("name") or Path(filename).stem,
            package_name=badging.get("package_name") or module_prop.get("id", ""),
            version=badging.get("version") or module_prop.get("version", ""),
            version_code=int(badging.get("version_code") or module_prop.get("versionCode") or 0),
            min_sdk=int(badging.get("min_sdk") or 0),
            abi=str(module_prop.get("abi", "") or ""),
            description=module_prop.get("description", ""),
            sha256=sha256,
            size_bytes=len(data),
            source=source,
            source_url=source_url,
            metadata=metadata,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        record.file_path = str(self._store(data, filename, sha256))
        return record

    def import_file(self, path: str | Path) -> ModuleRecord:
        """Import a local APK/ZIP."""
        source_path = Path(path).expanduser()
        if not source_path.is_file():
            raise ValidationError(f"file not found: {source_path}")
        return self.build_record(
            source_path.read_bytes(), source_path.name, source="upload"
        )

    def import_url(self, url: str) -> ModuleRecord:
        """Download and import a package, preserving provenance."""
        parsed = url.strip()
        if not parsed.lower().startswith(("http://", "https://")):
            raise ValidationError(
                f"unsupported URL: {url}", hint="only http(s) URLs can be imported"
            )
        if parsed.lower().startswith("http://") and not self._allow_insecure:
            raise ValidationError(
                "refusing to import over plain HTTP",
                hint="use an https URL, or enable insecure URLs explicitly in the configuration",
            )
        fetch = self._http_get or _default_http_get
        data = fetch(parsed, MAX_MODULE_BYTES)
        filename = Path(parsed.split("?", 1)[0]).name or "module.bin"
        return self.build_record(
            data, filename, source="url", source_url=_safe_url(parsed)
        )


def _default_http_get(url: str, limit: int) -> bytes:  # pragma: no cover - network
    import httpx

    with (
        httpx.Client(follow_redirects=True, timeout=120.0) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        # The guard in import_url only sees the initial URL; httpx follows redirects,
        # so verify the URL actually being read did not downgrade the scheme.
        final = str(response.url)
        if url.lower().startswith("https://") and not final.lower().startswith("https://"):
            raise ValidationError(
                f"the download was redirected to an insecure URL: {final}",
                hint="nothing was stored; use an https source that does not redirect",
            )
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > limit:
                raise ValidationError(f"download exceeded the {limit} byte limit")
            chunks.append(chunk)
    return b"".join(chunks)


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name)
    return cleaned[:120]


__all__ = [
    "AaptInfo",
    "MAX_MODULE_BYTES",
    "ModuleLibrary",
    "find_aapt2",
    "parse_badging",
]
