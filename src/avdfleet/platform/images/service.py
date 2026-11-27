"""Image service: download, custom import and OOBE-matrix persistence.

Implements the frozen :class:`avdfleet.core.interfaces.ImageCatalog` protocol on top
of :class:`~avdfleet.platform.images.catalogue.ImageCatalogue`.

Downloads are delegated to a duck-typed ``SdkBackend.install_packages`` call; this
module never downloads over HTTP itself, which keeps unit tests offline.  Custom
image import copies an operator-supplied directory into the SDK layout without ever
moving the original files.
"""

from __future__ import annotations

import inspect
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ...core.config import Config
from ...core.errors import AvdFleetError, NotFoundError, ToolError
from ...core.logging import get_logger
from ...core.models import (
    OobeMatrix,
    OobeRecord,
    OobeStatus,
    SystemImage,
    sha256_file,
    utcnow,
)
from ...core.paths import Paths
from .catalogue import (
    ABI_VALUES,
    API_DIRECTORY_RE,
    API_PATH_RE,
    DEFAULT_MAX_IMPORT_BYTES,
    DEFAULT_SEED_MATRIX_PATH,
    IMAGE_ID_RE,
    IMPORTABLE_SUFFIXES,
    OPTIONAL_IMAGE_FILENAMES,
    PRIMARY_IMAGE_FILENAMES,
    ImageCatalogue,
    ImageError,
    ImageNotFoundError,
    OobeMatrixStore,
    _is_link,
    image_kind_for_tag,
    package_id_for,
    parse_properties,
    primary_image_file,
    sanitize_tag,
)

_ABI_ALIASES = {
    "arm64": "arm64-v8a",
    "aarch64": "arm64-v8a",
    "arm64-v8a": "arm64-v8a",
    "x86_64": "x86_64",
    "x86-64": "x86_64",
    "amd64": "x86_64",
    "x86": "x86",
    "i386": "x86",
    "i686": "x86",
    "armeabi-v7a": "armeabi-v7a",
    "armv7l": "armeabi-v7a",
    "riscv64": "riscv64",
}

_CUSTOM_TAG = "avdfleet_custom"


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _invoke(function: Any, positional: list[Any], kwargs: dict[str, Any]) -> Any:
    """Call *function* passing only the keyword arguments its signature accepts.

    SDK backend implementations in the wild (and in tests/fakes) range from the full
    :class:`~avdfleet.core.interfaces.SdkBackend` signature to a two-line fake; this
    keeps the service backend-agnostic without swallowing real ``TypeError``s.
    """
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):  # pragma: no cover - builtins / odd callables
        return function(*positional, **kwargs)
    parameters = signature.parameters
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return function(*positional, **kwargs)
    accepted = {
        name: value
        for name, value in kwargs.items()
        if name in parameters
        and parameters[name].kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return function(*positional, **accepted)


def _prop(properties: dict[str, str], *names: str) -> str | None:
    """Case-insensitive ``key=value`` lookup over parsed properties."""
    lowered = {key.lower(): value for key, value in properties.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value:
            return value
    return None


def _to_int(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(r"\s*(\d{1,3})\s*", value)
    if match is None:
        return None
    number = int(match.group(1))
    return number if 1 <= number <= 99 else None


def _normalize_abi(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip().lower()
    normalized = _ABI_ALIASES.get(candidate)
    return normalized if normalized in ABI_VALUES else None


def _validate_abi(value: str) -> str:
    normalized = _normalize_abi(value)
    if normalized is None:
        raise ImageError(
            f"unsupported image ABI {value!r}",
            hint=f"supported ABIs: {', '.join(ABI_VALUES)}",
        )
    return normalized


def _validate_image_id(value: str) -> str:
    candidate = str(value).strip()
    if not candidate:
        raise ImageError("image_id must not be empty")
    if "/" in candidate or "\\" in candidate or ".." in candidate:
        raise ImageError(
            f"invalid image id {candidate!r}",
            hint="image ids must not contain path separators or '..'",
        )
    if IMAGE_ID_RE.match(candidate) is None:
        raise ImageError(
            f"invalid image id {candidate!r}",
            hint="image ids are lowercase letters/digits with '.', '_', '-', ';' or '+'",
        )
    return candidate


def _infer_api_level(
    source: Path,
    properties: dict[str, str],
    build_properties: dict[str, str],
) -> int | None:
    for candidate in (
        _prop(properties, "AndroidVersion.ApiLevel", "SystemImage.ApiLevel"),
        _prop(build_properties, "ro.build.version.sdk"),
    ):
        number = _to_int(candidate)
        if number is not None:
            return number
    match = API_PATH_RE.search(source.as_posix())
    return int(match.group(1)) if match is not None else None


def _infer_abi(
    source: Path,
    properties: dict[str, str],
    build_properties: dict[str, str],
) -> str | None:
    for candidate in (
        _prop(properties, "SystemImage.Abi"),
        _prop(build_properties, "ro.product.cpu.abi"),
        (_prop(build_properties, "ro.product.cpu.abilist") or "").split(",")[0],
    ):
        normalized = _normalize_abi(candidate)
        if normalized is not None:
            return normalized
    if API_PATH_RE.search(source.as_posix()) is None:
        return None
    for part in reversed(source.as_posix().split("/")):
        normalized = _normalize_abi(part)
        if normalized is not None:
            return normalized
    return None


def _infer_tag(source: Path, properties: dict[str, str]) -> str | None:
    explicit = _prop(properties, "SystemImage.TagId")
    if explicit:
        return explicit
    match = re.search(r"system-images[/\\]android-\d{1,3}[/\\]([^/\\]+)", source.as_posix())
    if match is not None:
        return match.group(1)
    if API_PATH_RE.search(source.as_posix()) is None:
        return None
    parts = source.as_posix().split("/")
    for index in range(len(parts) - 1, 0, -1):
        if _normalize_abi(parts[index]) is None:
            continue
        candidate = parts[index - 1]
        if candidate and candidate != "system-images" and API_DIRECTORY_RE.match(candidate) is None:
            return candidate
        return None
    return None


def _is_importable_file(name: str) -> bool:
    lowered = name.lower()
    if name in OPTIONAL_IMAGE_FILENAMES or name in PRIMARY_IMAGE_FILENAMES:
        return True
    if lowered.endswith(IMPORTABLE_SUFFIXES):
        return True
    return lowered.startswith("kernel")


def _collect_import_files(source: Path, max_bytes: int, sink: Any) -> list[Path]:
    try:
        entries = sorted(source.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise ImageError(f"cannot list image directory {source}: {exc}") from exc
    files: list[Path] = []
    total = 0
    for entry in entries:
        if _is_link(entry):
            raise ImageError(
                f"refusing to import symbolic link {entry.name!r}",
                hint="copy real files into the image directory first",
                details={"entry": str(entry)},
            )
        if entry.is_dir():
            sink.add(f"ignoring nested directory while importing the image: {entry.name}")
            continue
        if not entry.is_file():
            sink.add(f"ignoring special file while importing the image: {entry.name}")
            continue
        if not _is_importable_file(entry.name):
            sink.add(f"ignoring unrecognised file while importing the image: {entry.name}")
            continue
        try:
            size = entry.stat().st_size
        except OSError as exc:
            raise ImageError(f"cannot stat image file {entry.name}: {exc}") from exc
        if size > max_bytes or total + size > max_bytes:
            raise ImageError(
                f"image directory is too large: {entry.name} would exceed {_format_bytes(max_bytes)}",
                hint="import a smaller image or raise the limit deliberately",
            )
        total += size
        files.append(entry)
    return files


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"  # pragma: no cover - unreachable


def _resolve_display_name(
    name: str | None,
    tag_display: str | None,
    tag: str,
    api_level: int,
    abi: str,
) -> str:
    if name is not None:
        cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", str(name)).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        if not cleaned:
            raise ImageError("image display name must not be empty")
        return cleaned[:160]
    if tag_display:
        return f"{tag_display} API {api_level} ({abi})"[:160]
    if tag == _CUSTOM_TAG:
        return f"Custom image API {api_level} ({abi})"
    return f"{tag.replace('_', ' ').title()} API {api_level} ({abi})"[:160]


def _write_source_properties(
    target: Path,
    *,
    api_level: int,
    tag: str,
    abi: str,
    display_name: str,
) -> None:
    lines = [
        "# Generated by avdfleet custom image import.",
        f"Pkg.Desc={display_name}",
        "Pkg.Revision=1",
        f"AndroidVersion.ApiLevel={api_level}",
        f"SystemImage.Abi={abi}",
        f"SystemImage.TagId={tag}",
        f"SystemImage.TagDisplay={display_name}",
        f"Pkg.Path={package_id_for(api_level, tag, abi)}",
    ]
    try:
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ImageError(f"cannot write {target.name}: {exc}") from exc


def _image_directory_for_package(sdk_root: Path, package: str) -> Path | None:
    parts = [part for part in package.split(";") if part]
    if len(parts) != 4 or parts[0] != "system-images":
        return None
    candidate = Path(sdk_root) / "system-images" / parts[1] / parts[2] / parts[3]
    return candidate if candidate.is_dir() else None


def _sort_oobe_records(records: list[OobeRecord]) -> list[OobeRecord]:
    return sorted(
        records,
        key=lambda record: (int(record.api_level), record.tag, record.abi, record.image_id),
    )


def _matches_image(image: SystemImage, query: str) -> bool:
    normalized = query.strip().casefold()
    if not normalized:
        return False
    candidates = {
        image.id,
        image.package,
        f"android-{image.api_level};{image.tag};{image.abi}",
        f"android-{image.api_level}/{image.tag}/{image.abi}",
        f"{image.api_level};{image.tag};{image.abi}",
    }
    for candidate in candidates:
        if not candidate:
            continue
        value = candidate.strip().casefold()
        if value == normalized:
            return True
        if value.removeprefix("system-images;") == normalized.removeprefix("system-images;"):
            return True
    return False


# ---------------------------------------------------------------------------
# ImageService
# ---------------------------------------------------------------------------


class ImageService:
    """Concrete ``ImageCatalog`` used by the API, CLI and fleet reconciler.

    Parameters
    ----------
    paths:
        Resolved :class:`~avdfleet.core.paths.Paths`.  A plain directory is accepted
        for convenience and treated as a portable-style root.
    config:
        Optional :class:`~avdfleet.core.config.Config`; ``oobe_matrix_path`` is
        honoured when set.
    sdk:
        Duck-typed ``SdkBackend``.  ``list_system_images`` supplies installed
        packages, ``install_packages`` performs downloads and an optional
        ``refresh`` is called by :meth:`refresh`.
    host:
        Optional duck-typed ``HostProvider``; only used to recover ``sdk_root``
        and the host ABI when ``Paths`` does not provide them.
    catalogue:
        Pre-built catalogue (tests/fakes may inject one).
    """

    def __init__(
        self,
        paths: Paths | str | Path,
        config: Config | None = None,
        *,
        sdk: Any | None = None,
        host: Any | None = None,
        catalogue: ImageCatalogue | None = None,
        catalogue_path: str | Path | None = None,
        custom_registry_path: str | Path | None = None,
        matrix_path: str | Path | None = None,
        seed_matrix_path: str | Path | None = None,
        max_import_bytes: int = DEFAULT_MAX_IMPORT_BYTES,
        accept_licenses: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        if isinstance(paths, Paths):
            self.paths = paths
        else:
            root = Path(paths).expanduser()
            self.paths = Paths(
                root=root,
                data_dir=root / "data",
                config_dir=root / "config",
                cache_dir=root / "cache",
                log_dir=root / "logs",
                sdk_root=root / "sdk",
                avd_home=root / "avd",
            )
        if config is not None and not isinstance(config, Config) and sdk is None:
            # Be forgiving for callers that pass the SDK backend positionally.
            sdk = config
            config = None
        self.config = config
        self.host = host
        self._logger = logger or get_logger("avdfleet.images")
        self._sdk: Any | None = sdk
        self._catalogue = catalogue
        self._catalogue_injected = catalogue is not None
        self._catalogue_path = catalogue_path
        self._custom_registry_path = custom_registry_path
        self._matrix_path = matrix_path
        self._seed_matrix_path = seed_matrix_path
        self._max_import_bytes = int(max_import_bytes)
        if self._max_import_bytes <= 0:
            raise ValueError("max_import_bytes must be positive")
        self._accept_licenses = bool(accept_licenses)
        self._oobe_store: OobeMatrixStore | None = None
        self._warnings: list[str] = []
        self._import_sink = _ServiceWarningSink(self._warn)

    # -- properties ---------------------------------------------------------
    @property
    def sdk(self) -> Any | None:
        return self._sdk

    @sdk.setter
    def sdk(self, value: Any | None) -> None:
        self._sdk = value
        if not self._catalogue_injected:
            # Drop a self-built catalogue so the next access binds the new backend.
            self._catalogue = None

    @property
    def backend(self) -> Any | None:
        """Alias for the duck-typed SDK backend (kept for container wiring)."""
        return self._sdk

    @property
    def catalogue(self) -> ImageCatalogue:
        if self._catalogue is None:
            self._catalogue = ImageCatalogue(
                self.paths,
                catalogue_path=self._catalogue_path,
                custom_registry_path=self._custom_registry_path,
                sdk=self._sdk,
                logger=self._logger,
            )
        return self._catalogue

    @property
    def warnings(self) -> list[str]:
        """Non-fatal problems encountered while listing/importing/refreshing."""
        collected = list(self._warnings)
        for source in (self._catalogue, self._oobe_store):
            if source is None:
                continue
            for message in source.warnings:
                if message not in collected:
                    collected.append(message)
        return collected

    # -- protocol: listing --------------------------------------------------
    def list_images(
        self,
        *,
        include_remote: bool = True,
        installed_only: bool = False,
    ) -> list[SystemImage]:
        images = self._attach_oobe(self.catalogue.all_images())
        if installed_only or not include_remote:
            images = [image for image in images if image.installed]
        return images

    def get_image(self, image_id: str) -> SystemImage:
        query = (image_id or "").strip()
        if not query:
            raise NotFoundError("an image id is required")
        for image in self.list_images(include_remote=True):
            if _matches_image(image, query):
                return image
        raise ImageNotFoundError(
            f"system image not found: {query}",
            hint="list the catalogue and call refresh() if the SDK changed",
        )

    def refresh(self, *, timeout_s: int = 60) -> list[SystemImage]:
        backend = self._sdk
        refresher = getattr(backend, "refresh", None) if backend is not None else None
        if callable(refresher):
            try:
                _invoke(refresher, [], {"timeout_s": timeout_s})
            except AvdFleetError as exc:
                raise ToolError(f"SDK system-image refresh failed: {exc.message}", hint=exc.hint) from exc
            except Exception as exc:
                raise ToolError(f"SDK system-image refresh failed: {exc}") from exc
        self.catalogue.reload()
        return self.list_images(include_remote=True)

    # -- protocol: download -------------------------------------------------
    def download(self, image_id: str, *, dry_run: bool = False) -> SystemImage:
        image = self.get_image(image_id)
        if image.installed:
            return image
        package = (image.package or "").strip()
        if not package and image.api_level and image.tag and image.abi:
            package = package_id_for(image.api_level, image.tag, image.abi)
        if not package:
            raise ImageError(
                f"image {image.id!r} has no installable SDK package",
                hint="register or import the image instead of downloading it",
            )
        if image.custom:
            raise ImageError(
                f"custom image {image.id!r} is not installed at its registered path",
                hint="re-import the image directory",
            )
        if dry_run:
            return image.model_copy(
                update={
                    "metadata": {**image.metadata, "dry_run": True, "package": package},
                    "notes": image.notes,
                }
            )
        backend = self._sdk
        installer = getattr(backend, "install_packages", None) if backend is not None else None
        if not callable(installer):
            raise ToolError(
                "no Android SDK backend is configured to install system images",
                hint="run `avdfleet install` first, then retry the download",
            )
        try:
            result = _invoke(
                installer,
                [[package]],
                {"accept_licenses": self._accept_licenses, "dry_run": False},
            )
        except AvdFleetError:
            raise
        except Exception as exc:
            raise ToolError(f"installing system image package {package!r} failed: {exc}") from exc
        if result is not None and getattr(result, "ok", True) is False:
            message = getattr(result, "message", "") or "the SDK backend reported a failure"
            raise ToolError(
                f"installing system image package {package!r} failed: {message}",
                details={"package": package},
            )
        return self._after_install(image, package)

    def _after_install(self, image: SystemImage, package: str) -> SystemImage:
        refreshed: SystemImage | None = None
        for candidate in self.list_images(include_remote=True):
            if candidate.installed and package in {candidate.package, candidate.id}:
                refreshed = candidate
                break
        if refreshed is None:
            local = None
            sdk_root = self._resolve_sdk_root()
            if sdk_root is not None:
                local = _image_directory_for_package(sdk_root, package)
            self._warn(
                f"the SDK backend reported {package!r} installed but no local files were found; "
                "call refresh() after the SDK finishes writing them"
            )
            refreshed = image.model_copy(
                update={
                    "installed": True,
                    "local_path": str(local) if local is not None else image.local_path,
                    "metadata": {**image.metadata, "installed_package": package},
                }
            )
        if refreshed.sha256:
            self._verify_checksum(refreshed)
        return refreshed

    def _verify_checksum(self, image: SystemImage) -> None:
        local = Path(image.local_path).expanduser() if image.local_path else None
        primary = primary_image_file(local) if local is not None else None
        if primary is None:
            self._warn(f"cannot verify the SHA-256 of {image.id!r}: no local system image file was found")
            return
        expected = (image.sha256 or "").strip().lower()
        if not expected:
            return
        actual = sha256_file(str(primary))
        if actual != expected:
            raise ImageError(
                f"SHA-256 mismatch for image {image.id!r}",
                hint="the downloaded image is corrupt; remove it and download again",
                details={"expected": expected, "actual": actual},
            )

    # -- protocol: custom import -------------------------------------------
    def import_directory(
        self,
        path: str | Path,
        *,
        name: str | None = None,
        image_id: str | None = None,
        api_level: int | None = None,
        abi: str | None = None,
        force: bool = False,
    ) -> SystemImage:
        source = self._validated_source(path)
        primary = primary_image_file(source)
        if primary is None:
            raise ImageError(
                f"no system.img found in {source}",
                hint="a custom image directory must contain system.img (or system.img.qcow2)",
            )
        properties = parse_properties(source / "source.properties")
        build_properties = parse_properties(source / "build.prop")

        resolved_api = api_level if api_level is not None else _infer_api_level(source, properties, build_properties)
        if resolved_api is None:
            raise ImageError(
                f"cannot determine the API level for {source}",
                hint="pass api_level= or include source.properties/build.prop metadata",
            )
        if not isinstance(resolved_api, int) or isinstance(resolved_api, bool) or not 1 <= resolved_api <= 99:
            raise ImageError(f"invalid API level {resolved_api!r}; expected an integer from 1 to 99")

        resolved_abi = abi if abi is not None else _infer_abi(source, properties, build_properties)
        if resolved_abi is None:
            resolved_abi = self._host_abi()
            self._warn(
                f"ABI could not be inferred for {source.name}; defaulting to the host ABI {resolved_abi}"
            )
        resolved_abi = _validate_abi(resolved_abi)

        raw_tag = _infer_tag(source, properties) or ""
        resolved_tag = sanitize_tag(raw_tag)
        if raw_tag and resolved_tag != raw_tag.strip().lower():
            self._warn(f"sanitised image tag {raw_tag!r} to {resolved_tag!r}")
        elif not raw_tag:
            self._warn(f"image tag could not be inferred for {source.name}; using {resolved_tag!r}")

        tag_display = _prop(properties, "SystemImage.TagDisplay")
        resolved_name = _resolve_display_name(name, tag_display, resolved_tag, resolved_api, resolved_abi)
        package = package_id_for(resolved_api, resolved_tag, resolved_abi)
        resolved_id = _validate_image_id(image_id) if image_id is not None else package
        resolved_kind = image_kind_for_tag(resolved_tag, custom=(resolved_tag == _CUSTOM_TAG))

        sdk_root = self._resolve_sdk_root()
        if sdk_root is None:
            raise ImageError(
                "the Android SDK root is not configured",
                hint="set Paths.sdk_root or export ANDROID_SDK_ROOT",
            )
        system_images_root = Path(sdk_root) / "system-images"
        destination = system_images_root / f"android-{resolved_api}" / resolved_tag / resolved_abi
        self._ensure_inside_sdk(destination, system_images_root)
        if source == destination or source in destination.parents or destination in source.parents:
            raise ImageError(
                "the image source and the SDK destination overlap",
                hint="import the image from a directory outside the SDK system-images tree",
            )

        files = _collect_import_files(source, self._max_import_bytes, self._import_sink)
        if not force:
            self._check_registry_conflict(resolved_id, package)

        copied_bytes, created = self._install_image_files(
            source=source,
            destination=destination,
            files=files,
            force=force,
            api_level=resolved_api,
            tag=resolved_tag,
            abi=resolved_abi,
            display_name=resolved_name,
        )
        copied_primary = destination / primary.name
        if not copied_primary.is_file():
            if created:
                shutil.rmtree(destination, ignore_errors=True)
            raise ImageError(f"the primary system image file is missing after copying: {copied_primary.name}")
        checksum = sha256_file(str(copied_primary))

        imported = SystemImage(
            id=resolved_id,
            api_level=resolved_api,
            tag=resolved_tag,
            abi=resolved_abi,
            kind=resolved_kind,
            display_name=resolved_name,
            package=package,
            sha256=checksum,
            size_bytes=copied_bytes,
            installed=True,
            local_path=str(destination),
            oobe_status=OobeStatus.UNVERIFIED,
            custom=True,
            notes=f"Imported from {source}",
            metadata={
                "source": "import",
                "source_path": str(source),
                "primary_file": primary.name,
                "imported_at": utcnow().isoformat(),
            },
        )
        try:
            registered = self.catalogue.register_custom_image(imported, force=force)
        except AvdFleetError:
            if created:
                shutil.rmtree(destination, ignore_errors=True)
            raise
        return self._attach_oobe([registered])[0]

    def _validated_source(self, path: str | Path) -> Path:
        if path is None or not str(path).strip():
            raise ImageError(
                "an image directory path is required",
                hint="pass an existing directory containing system.img",
            )
        candidate = Path(path).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ImageError(
                f"image directory not found: {path}",
                hint="pass an existing directory containing system.img",
            ) from exc
        except OSError as exc:
            raise ImageError(f"cannot access image directory {path}: {exc}") from exc
        if _is_link(resolved):
            raise ImageError(
                f"refusing to import a symbolic link: {resolved}",
                hint="copy the image to a real directory first",
            )
        if not resolved.is_dir():
            raise ImageError(f"not a directory: {resolved}")
        if resolved.parent == resolved:
            raise ImageError("refusing to import a filesystem root as a system image")
        return resolved

    def _install_image_files(
        self,
        *,
        source: Path,
        destination: Path,
        files: list[Path],
        force: bool,
        api_level: int,
        tag: str,
        abi: str,
        display_name: str,
    ) -> tuple[int, bool]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if _is_link(destination):
                raise ImageError(f"refusing to overwrite symbolic link {destination}")
            if not destination.is_dir():
                raise ImageError(f"cannot import the image: {destination} exists and is not a directory")
            if not force:
                raise ImageError(
                    f"a system image already exists at {destination}",
                    hint="pass force=True to overwrite it",
                )
            copied = 0
            for item in files:
                target = destination / item.name
                if _is_link(target):
                    raise ImageError(f"refusing to overwrite symbolic link {target}")
                try:
                    shutil.copy2(item, target)
                    copied += target.stat().st_size
                except OSError as exc:
                    raise ImageError(f"failed to copy {item.name} into {destination}: {exc}") from exc
            if not (destination / "source.properties").is_file():
                _write_source_properties(
                    destination / "source.properties",
                    api_level=api_level,
                    tag=tag,
                    abi=abi,
                    display_name=display_name,
                )
            return copied, False

        staging = Path(tempfile.mkdtemp(prefix=".avdfleet-import-", dir=str(destination.parent)))
        try:
            copied = 0
            for item in files:
                target = staging / item.name
                shutil.copy2(item, target)
                copied += target.stat().st_size
            if not (staging / "source.properties").is_file():
                _write_source_properties(
                    staging / "source.properties",
                    api_level=api_level,
                    tag=tag,
                    abi=abi,
                    display_name=display_name,
                )
            os.replace(staging, destination)
        except ImageError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise ImageError(
                f"failed to copy the image into {destination}: {exc}",
                hint="check free disk space and write permissions",
            ) from exc
        return copied, True

    def _check_registry_conflict(self, image_id: str, package: str) -> None:
        for record in self.catalogue.custom_images():
            if record.id == image_id or (record.package and record.package == package):
                raise ImageError(
                    f"image {record.id!r} is already registered",
                    hint="pass force=True to replace the existing registration",
                )

    def _ensure_inside_sdk(self, destination: Path, root: Path) -> None:
        resolved_root = Path(os.path.abspath(root))
        resolved_destination = Path(os.path.abspath(destination))
        if resolved_destination != resolved_root and resolved_root not in resolved_destination.parents:
            raise ImageError(
                f"refusing to write outside the SDK system-images directory: {destination}",
                hint="use a valid API level, tag and ABI",
            )

    # -- protocol: OOBE -----------------------------------------------------
    def oobe_matrix(self) -> OobeMatrix:
        matrix = self._matrix_store.load()
        records = list(matrix.records)
        known_ids = {record.image_id for record in records if record.image_id}
        known_coordinates = {(record.api_level, record.tag, record.abi) for record in records}
        for image in self.catalogue.all_images():
            coordinate = (image.api_level, image.tag, image.abi)
            if image.id in known_ids or coordinate in known_coordinates:
                continue
            records.append(
                OobeRecord(
                    image_id=image.id,
                    api_level=image.api_level,
                    tag=image.tag,
                    abi=image.abi,
                    status=OobeStatus.UNVERIFIED,
                    notes="not verified yet: no evidence recorded",
                )
            )
            known_ids.add(image.id)
        return matrix.model_copy(update={"records": _sort_oobe_records(records)})

    def update_oobe_record(self, record: OobeRecord) -> None:
        self._matrix_store.update(record)
        if record.status is OobeStatus.FAILED:
            message = f"OOBE verification failed for image {record.image_id}"
            if record.notes:
                message = f"{message}: {record.notes}"
            self._warn(message)

    # -- internal helpers ---------------------------------------------------
    def _warn(self, message: str) -> None:
        if message not in self._warnings:
            self._warnings.append(message)
        self._logger.warning("%s", message)

    @property
    def _matrix_store(self) -> OobeMatrixStore:
        if self._oobe_store is None:
            seed_path = (
                Path(self._seed_matrix_path).expanduser()
                if self._seed_matrix_path is not None
                else DEFAULT_SEED_MATRIX_PATH
            )
            self._oobe_store = OobeMatrixStore(
                self._matrix_path_value(),
                seed_path=seed_path,
                logger=self._logger,
            )
        return self._oobe_store

    def _matrix_path_value(self) -> Path:
        if self._matrix_path is not None:
            return Path(self._matrix_path).expanduser()
        config_path = getattr(self.config, "oobe_matrix_path", None) if self.config is not None else None
        if config_path:
            return Path(str(config_path)).expanduser()
        return self.paths.oobe_matrix_path

    def _resolve_sdk_root(self) -> Path | None:
        root: Any = getattr(self.paths, "sdk_root", None)
        if not root and self.host is not None:
            sdk_paths = getattr(self.host, "sdk_paths", None)
            if callable(sdk_paths):
                try:
                    resolved = sdk_paths()
                except Exception as exc:  # pragma: no cover - defensive
                    self._warn(f"host backend failed to report SDK paths: {exc}")
                    resolved = None
                root = getattr(resolved, "sdk_root", None)
        if not root:
            return None
        return Path(str(root)).expanduser()

    def _host_abi(self) -> str:
        arch: Any = None
        if self.host is not None:
            detect = getattr(self.host, "detect", None)
            if callable(detect):
                try:
                    arch = getattr(detect(), "arch", None)
                except Exception as exc:  # pragma: no cover - defensive
                    self._warn(f"host backend failed to detect the host architecture: {exc}")
        value = getattr(arch, "value", arch)
        if value is None:
            return "x86_64"
        return _normalize_abi(str(value)) or "x86_64"

    def _attach_oobe(self, images: list[SystemImage]) -> list[SystemImage]:
        matrix = self._matrix_store.load()
        by_id = {record.image_id: record for record in matrix.records if record.image_id}
        by_coordinate = {
            (record.api_level, record.tag, record.abi): record for record in matrix.records
        }
        attached: list[SystemImage] = []
        for image in images:
            record = by_id.get(image.id)
            if record is None:
                record = by_coordinate.get((image.api_level, image.tag, image.abi))
            status = record.status if record is not None else OobeStatus.UNVERIFIED
            attached.append(image.model_copy(update={"oobe_status": status}))
        return attached


class _ServiceWarningSink:
    """Adapter so module helpers can emit warnings through :class:`ImageService`."""

    def __init__(self, emit: Any) -> None:
        self._emit = emit

    def add(self, message: str) -> None:
        self._emit(message)


__all__ = ["ImageService"]
