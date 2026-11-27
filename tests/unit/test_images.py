"""Unit tests for the system-image catalogue, import and OOBE matrix.

Every test is offline and hermetic: a fake SDK tree is created under ``tmp_path`` and
downloads go through a recording fake ``SdkBackend``.  No real Android tools, no
network and no credentials are involved (contracts/fakes-spec.md §7).
"""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path
from typing import Any

import pytest

from avdfleet.core.errors import NotFoundError, ToolError
from avdfleet.core.interfaces import ImageCatalog
from avdfleet.core.models import (
    CpuArch,
    HostInfo,
    ImageKind,
    OobeRecord,
    OobeStatus,
    OperationResult,
    SdkPaths,
    SystemImage,
    utcnow,
)
from avdfleet.core.paths import Paths
from avdfleet.platform.images import (
    DEFAULT_CATALOGUE_PATH,
    ImageCatalogue,
    ImageError,
    ImageNotFoundError,
    ImageService,
)
from avdfleet.platform.images.catalogue import OobeMatrixStore

STOCK_PLAYSTORE_36 = "system-images;android-36;google_apis_playstore;x86_64"
STOCK_GOOGLE_APIS_35 = "system-images;android-35;google_apis;x86_64"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_paths(tmp_path: Path, sdk_root: Path | None = None) -> Paths:
    return Paths(
        root=tmp_path,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
        sdk_root=sdk_root or tmp_path / "sdk",
        avd_home=tmp_path / "avd",
    )


def write_installed_image(
    sdk_root: Path,
    *,
    api: int = 35,
    tag: str = "google_apis",
    abi: str = "x86_64",
    payload: bytes = b"installed-system-image",
    properties: dict[str, str] | None = None,
) -> Path:
    directory = sdk_root / "system-images" / f"android-{api}" / tag / abi
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "system.img").write_bytes(payload)
    default_properties = {
        "Pkg.Desc": "Installed fake image",
        "AndroidVersion.ApiLevel": str(api),
        "SystemImage.Abi": abi,
        "SystemImage.TagId": tag,
        "SystemImage.TagDisplay": "Google APIs",
    }
    if properties is not None:
        default_properties.update(properties)
    (directory / "source.properties").write_text(
        "".join(f"{key}={value}\n" for key, value in default_properties.items()),
        encoding="utf-8",
    )
    return directory


def write_source_properties(
    source: Path,
    *,
    api: int,
    tag: str,
    abi: str,
    tag_display: str | None = None,
) -> None:
    lines = [
        f"AndroidVersion.ApiLevel={api}",
        f"SystemImage.Abi={abi}",
        f"SystemImage.TagId={tag}",
    ]
    if tag_display:
        lines.append(f"SystemImage.TagDisplay={tag_display}")
    (source / "source.properties").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_custom_source(
    root: Path,
    *,
    api: int = 34,
    tag: str = "my_custom",
    abi: str = "x86_64",
    payload: bytes = b"custom-system-image",
    with_properties: bool = True,
    with_build_prop: bool = False,
    qcow2: bool = False,
    name: str = "custom-src",
) -> Path:
    source = root / name
    source.mkdir(parents=True)
    filename = "system.img.qcow2" if qcow2 else "system.img"
    (source / filename).write_bytes(payload)
    (source / "vendor.img").write_bytes(b"vendor-image")
    if with_properties:
        write_source_properties(source, api=api, tag=tag, abi=abi)
    if with_build_prop:
        (source / "build.prop").write_text(
            f"ro.build.version.sdk={api}\nro.product.cpu.abi={abi}\nro.build.version.release=13\n",
            encoding="utf-8",
        )
    return source


def find_image(images: list[SystemImage], image_id: str) -> SystemImage:
    for image in images:
        if image.id == image_id:
            return image
    raise AssertionError(f"image {image_id!r} not in {[image.id for image in images]}")


class FakeSdk:
    """Minimal duck-typed ``SdkBackend`` recording calls (never touches the network)."""

    name = "fake-sdk"

    def __init__(
        self,
        images: list[SystemImage] | None = None,
        *,
        fail_install: bool = False,
        raise_install: Exception | None = None,
        fail_refresh: Exception | None = None,
        on_install: Any = None,
    ) -> None:
        self.images = list(images or [])
        self.fail_install = fail_install
        self.raise_install = raise_install
        self.fail_refresh = fail_refresh
        self.on_install = on_install
        self.install_calls: list[dict[str, Any]] = []
        self.refresh_calls: list[int] = []

    def list_system_images(self) -> list[SystemImage]:
        return [image.model_copy(deep=True) for image in self.images]

    def install_packages(
        self,
        package_ids: list[str],
        *,
        accept_licenses: bool = False,
        dry_run: bool = False,
        timeout_s: int = 1800,
    ) -> OperationResult:
        self.install_calls.append(
            {
                "packages": list(package_ids),
                "accept_licenses": accept_licenses,
                "dry_run": dry_run,
            }
        )
        if self.raise_install is not None:
            raise self.raise_install
        if self.fail_install:
            return OperationResult(ok=False, message="fake sdkmanager failure")
        if self.on_install is not None:
            self.on_install(list(package_ids))
        return OperationResult(ok=True, message="installed")

    def refresh(self, *, timeout_s: int = 60) -> list[SystemImage]:
        self.refresh_calls.append(timeout_s)
        if self.fail_refresh is not None:
            raise self.fail_refresh
        return self.list_system_images()


class FakeHost:
    """Duck-typed host backend used to test ``sdk_paths``/``detect`` fallbacks."""

    def __init__(self, sdk_root: Path, arch: CpuArch) -> None:
        self._sdk_root = sdk_root
        self._arch = arch

    def sdk_paths(self) -> SdkPaths:
        return SdkPaths(sdk_root=str(self._sdk_root))

    def detect(self) -> HostInfo:
        return HostInfo(arch=self._arch)


# ---------------------------------------------------------------------------
# Stock catalogue / listing
# ---------------------------------------------------------------------------


def test_stock_catalogue_lists_remote_images(tmp_path: Path) -> None:
    service = ImageService(make_paths(tmp_path))

    assert isinstance(service, ImageCatalog)
    images = service.list_images()
    assert images
    assert DEFAULT_CATALOGUE_PATH.is_file()

    playstore = find_image(images, STOCK_PLAYSTORE_36)
    assert playstore.api_level == 36
    assert playstore.tag == "google_apis_playstore"
    assert playstore.abi == "x86_64"
    assert playstore.kind is ImageKind.GOOGLE_APIS_PLAYSTORE
    assert playstore.installed is False
    assert playstore.url is None  # unverified catalogue never invents URLs
    assert playstore.sha256 is None
    assert playstore.oobe_status is OobeStatus.UNVERIFIED

    assert service.list_images(installed_only=True) == []
    assert service.list_images(include_remote=False) == []

    # get_image resolves ids, package ids and short coordinates.
    assert service.get_image(STOCK_PLAYSTORE_36).id == STOCK_PLAYSTORE_36
    assert service.get_image("android-35/google_apis/x86_64").id == STOCK_GOOGLE_APIS_35
    with pytest.raises(NotFoundError):
        service.get_image("system-images;android-99;nope;x86_64")
    with pytest.raises(ImageError):
        # ImageNotFoundError is both a contract 404 and an image error.
        service.get_image("system-images;android-99;nope;x86_64")
    assert issubclass(ImageNotFoundError, NotFoundError)
    assert issubclass(ImageNotFoundError, ImageError)


def test_catalogue_contract_file_matches_service(tmp_path: Path) -> None:
    catalogue = ImageCatalogue(make_paths(tmp_path))
    stock = catalogue.stock_images()
    assert len(stock) >= 20
    identifiers = {image.id for image in stock}
    assert STOCK_PLAYSTORE_36 in identifiers
    assert all(image.installed is False for image in stock)
    # Every stock image has a coherent coordinate triple.
    for image in stock:
        assert image.api_level > 0
        assert image.tag
        assert image.abi in {"x86_64", "arm64-v8a"}
        assert image.package == image.id


def test_installed_sdk_scan_marks_installed(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    installed_dir = write_installed_image(paths.sdk_root)
    service = ImageService(paths)

    images = service.list_images()
    installed = find_image(images, STOCK_GOOGLE_APIS_35)
    assert installed.installed is True
    assert installed.local_path == str(installed_dir)
    assert installed.kind is ImageKind.GOOGLE_APIS

    installed_only = service.list_images(installed_only=True)
    assert [image.id for image in installed_only] == [STOCK_GOOGLE_APIS_35]
    assert service.list_images(include_remote=False) == installed_only

    catalogue = service.catalogue
    assert [image.id for image in catalogue.scan_installed()] == [STOCK_GOOGLE_APIS_35]


def test_installed_scan_prefers_source_properties(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    # Directory names lie; source.properties is authoritative.
    directory = paths.sdk_root / "system-images" / "android-35" / "google_apis" / "x86_64"
    directory.mkdir(parents=True)
    (directory / "system.img").write_bytes(b"img")
    write_source_properties(directory, api=34, tag="aosp_atd", abi="x86_64")

    service = ImageService(paths)
    images = service.list_images(installed_only=True)
    assert [image.id for image in images] == ["system-images;android-34;aosp_atd;x86_64"]
    assert images[0].kind is ImageKind.AOSP


def test_scan_warns_about_incomplete_system_image(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    broken = paths.sdk_root / "system-images" / "android-35" / "broken" / "x86_64"
    broken.mkdir(parents=True)
    (broken / "vendor.img").write_bytes(b"vendor-only")

    service = ImageService(paths)
    assert service.list_images()  # stock catalogue still renders
    assert any("without system.img" in message for message in service.warnings)


def test_sdk_backend_images_are_preferred(tmp_path: Path) -> None:
    backend_package = "system-images;android-31;google_apis;x86_64"
    available_package = "system-images;android-31;google_apis_playstore;x86_64"
    backend = FakeSdk(
        [
            SystemImage(
                id=backend_package,
                api_level=31,
                tag="google_apis",
                abi="x86_64",
                kind=ImageKind.GOOGLE_APIS,
                display_name="Backend Google APIs API 31",
                package=backend_package,
            ),
            SystemImage(
                id=available_package,
                api_level=31,
                tag="google_apis_playstore",
                abi="x86_64",
                kind=ImageKind.GOOGLE_APIS_PLAYSTORE,
                package=available_package,
                installed=False,
            ),
        ]
    )
    service = ImageService(make_paths(tmp_path), sdk=backend)

    images = service.list_images()
    installed = find_image(images, backend_package)
    assert installed.installed is True
    available = find_image(images, available_package)
    assert available.installed is False

    # Backend entries are not stock entries; there is no stock metadata to merge.
    assert installed.display_name == "Backend Google APIs API 31"


def test_corrupt_custom_registry_is_tolerated(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.data_dir.mkdir(parents=True)
    (paths.data_dir / "custom_images.json").write_text("{not valid json", encoding="utf-8")
    service = ImageService(paths)

    assert service.list_images()  # stock images still list
    assert any("custom image registry is unreadable" in message for message in service.warnings)


def test_custom_registry_records_are_listed(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    destination = paths.sdk_root / "system-images" / "android-34" / "demo" / "x86_64"
    destination.mkdir(parents=True)
    (destination / "system.img").write_bytes(b"demo")
    record = {
        "id": "custom-demo",
        "api_level": 34,
        "tag": "demo",
        "abi": "x86_64",
        "kind": "custom",
        "display_name": "Demo custom image",
        "package": "system-images;android-34;demo;x86_64",
        "local_path": str(destination),
        "custom": True,
    }
    paths.data_dir.mkdir(parents=True)
    (paths.data_dir / "custom_images.json").write_text(
        json.dumps({"version": 1, "images": [record]}), encoding="utf-8"
    )

    service = ImageService(paths)
    image = service.get_image("custom-demo")
    assert image.custom is True
    assert image.installed is True
    assert image.local_path == str(destination)
    assert image.kind is ImageKind.CUSTOM

    # Remove the files: the record remains, but is not "installed" any more.
    (destination / "system.img").unlink()
    (destination / "vendor.img").unlink(missing_ok=True)
    stale = service.get_image("custom-demo")
    assert stale.installed is False
    assert "missing" in stale.notes


# ---------------------------------------------------------------------------
# Custom image import
# ---------------------------------------------------------------------------


def test_import_directory_valid(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    source = make_custom_source(tmp_path, api=34, tag="my_custom", abi="x86_64")
    original_bytes = (source / "system.img").read_bytes()
    service = ImageService(paths)

    image = service.import_directory(source)
    expected_package = "system-images;android-34;my_custom;x86_64"
    assert image.id == expected_package
    assert image.package == expected_package
    assert image.api_level == 34
    assert image.tag == "my_custom"
    assert image.abi == "x86_64"
    assert image.kind is ImageKind.CUSTOM
    assert image.custom is True
    assert image.installed is True
    assert image.sha256 == hashlib.sha256(original_bytes).hexdigest()
    assert image.size_bytes and image.size_bytes > 0

    destination = paths.sdk_root / "system-images" / "android-34" / "my_custom" / "x86_64"
    assert Path(image.local_path) == destination
    assert (destination / "system.img").read_bytes() == original_bytes
    assert (destination / "vendor.img").is_file()
    assert (destination / "source.properties").is_file()
    # The import copies; the operator's source directory is untouched.
    assert source.is_dir()
    assert (source / "system.img").read_bytes() == original_bytes

    registry = json.loads((paths.data_dir / "custom_images.json").read_text(encoding="utf-8"))
    assert registry["version"] == 1
    assert [record["id"] for record in registry["images"]] == [expected_package]

    listed = service.get_image(expected_package)
    assert listed.installed is True
    assert listed.notes.startswith("Imported from")


def test_import_infers_metadata_from_build_prop_and_path(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    source_root = tmp_path / "source" / "system-images" / "android-33" / "google_apis_playstore"
    source = source_root / "x86_64"
    source.mkdir(parents=True)
    (source / "system.img").write_bytes(b"playstore-image")
    (source / "build.prop").write_text(
        "ro.build.version.sdk=33\nro.product.cpu.abi=x86_64\n", encoding="utf-8"
    )
    service = ImageService(paths)

    image = service.import_directory(source)
    assert image.api_level == 33
    assert image.tag == "google_apis_playstore"
    assert image.abi == "x86_64"
    assert image.kind is ImageKind.GOOGLE_APIS_PLAYSTORE
    assert image.custom is True

    destination = paths.sdk_root / "system-images" / "android-33" / "google_apis_playstore" / "x86_64"
    assert Path(image.local_path) == destination
    generated = (destination / "source.properties").read_text(encoding="utf-8")
    assert "AndroidVersion.ApiLevel=33" in generated
    assert "SystemImage.TagId=google_apis_playstore" in generated
    assert "SystemImage.Abi=x86_64" in generated


def test_import_accepts_qcow2_disk_image(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    payload = b"qcow2-content"
    source = make_custom_source(tmp_path, api=33, tag="qcow_custom", qcow2=True, payload=payload)
    service = ImageService(paths)

    image = service.import_directory(source)
    destination = Path(image.local_path)
    assert destination.name == "x86_64"
    assert (destination / "system.img.qcow2").read_bytes() == payload
    assert not (destination / "system.img").exists()
    assert image.sha256 == hashlib.sha256(payload).hexdigest()
    assert image.metadata["primary_file"] == "system.img.qcow2"


def test_import_prefers_raw_system_img_when_both_exist(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    source = make_custom_source(tmp_path, api=32, tag="both_custom", payload=b"raw-image")
    (source / "system.img.qcow2").write_bytes(b"qcow2-overlay")
    service = ImageService(paths)

    image = service.import_directory(source)
    assert image.metadata["primary_file"] == "system.img"
    assert image.sha256 == hashlib.sha256(b"raw-image").hexdigest()
    destination = Path(image.local_path)
    assert (destination / "system.img").read_bytes() == b"raw-image"
    assert (destination / "system.img.qcow2").is_file()


def test_import_requires_system_img(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    source = tmp_path / "no-system"
    source.mkdir()
    (source / "vendor.img").write_bytes(b"vendor")
    service = ImageService(paths)

    with pytest.raises(ImageError, match="no system.img"):
        service.import_directory(source)
    with pytest.raises(ImageError):
        service.import_directory(tmp_path / "does-not-exist")
    a_file = tmp_path / "just-a-file.txt"
    a_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ImageError, match="not a directory"):
        service.import_directory(a_file)

    # Nothing was written into the SDK tree.
    assert not (paths.sdk_root / "system-images").exists()


def test_import_requires_api_level_or_metadata(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    source = tmp_path / "bare-image"
    source.mkdir()
    (source / "system.img").write_bytes(b"bare")
    service = ImageService(paths)

    with pytest.raises(ImageError, match="API level"):
        service.import_directory(source)


def test_import_rejects_invalid_coordinates(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    service = ImageService(paths)

    with pytest.raises(ImageError, match="invalid API level"):
        service.import_directory(
            make_custom_source(tmp_path, api=34, name="zero-api"), api_level=0
        )
    with pytest.raises(ImageError, match="unsupported image ABI"):
        service.import_directory(
            make_custom_source(tmp_path, api=34, name="bad-abi"), api_level=34, abi="../../evil"
        )
    with pytest.raises(ImageError, match="invalid image id"):
        service.import_directory(
            make_custom_source(tmp_path, api=34, name="bad-id"),
            api_level=34,
            image_id="Bad/ID",
        )


def test_import_rejects_oversized_image(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    source = make_custom_source(tmp_path, api=34, payload=b"x" * 4096)
    service = ImageService(paths, max_import_bytes=1024)

    with pytest.raises(ImageError, match="too large"):
        service.import_directory(source)
    assert not (paths.sdk_root / "system-images").exists()


def test_import_sanitizes_hostile_tag(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    source = make_custom_source(tmp_path, api=34, tag="../../evil")
    service = ImageService(paths)

    image = service.import_directory(source, api_level=34)
    assert image.tag == "evil"
    destination = Path(image.local_path)
    system_images_root = paths.sdk_root / "system-images"
    assert system_images_root in destination.parents
    assert destination == system_images_root / "android-34" / "evil" / "x86_64"


def test_import_never_overwrites_without_force(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    destination = paths.sdk_root / "system-images" / "android-34" / "my_custom" / "x86_64"
    destination.mkdir(parents=True)
    (destination / "system.img").write_bytes(b"old-image")
    source = make_custom_source(tmp_path, api=34, tag="my_custom", payload=b"new-image")
    service = ImageService(paths)

    with pytest.raises(ImageError, match="already exists"):
        service.import_directory(source)
    assert (destination / "system.img").read_bytes() == b"old-image"

    image = service.import_directory(source, force=True)
    assert (destination / "system.img").read_bytes() == b"new-image"
    assert image.sha256 == hashlib.sha256(b"new-image").hexdigest()
    registry = json.loads((paths.data_dir / "custom_images.json").read_text(encoding="utf-8"))
    assert len(registry["images"]) == 1


def test_import_registry_conflict_requires_force(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    source = make_custom_source(tmp_path, api=34, tag="repeat_custom", payload=b"first")
    service = ImageService(paths)
    first = service.import_directory(source)

    (source / "system.img").write_bytes(b"second")
    with pytest.raises(ImageError, match="already registered"):
        service.import_directory(source)

    second = service.import_directory(source, force=True)
    assert second.sha256 == hashlib.sha256(b"second").hexdigest()
    assert (Path(first.local_path) / "system.img").read_bytes() == b"second"
    registry = json.loads((paths.data_dir / "custom_images.json").read_text(encoding="utf-8"))
    assert [record["id"] for record in registry["images"]] == [first.id]


def test_import_writes_source_properties_when_missing(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    source = make_custom_source(tmp_path, api=34, tag="no_props", with_properties=False)
    service = ImageService(paths)

    # The tag is unknown without metadata, so the stable custom tag is used.
    image = service.import_directory(source, api_level=34, abi="x86_64")
    assert image.tag == "avdfleet_custom"
    generated = (Path(image.local_path) / "source.properties").read_text(encoding="utf-8")
    assert "AndroidVersion.ApiLevel=34" in generated
    assert "SystemImage.TagId=avdfleet_custom" in generated
    assert "SystemImage.Abi=x86_64" in generated


def test_import_ignores_unknown_files_with_warning(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    source = make_custom_source(tmp_path, api=34, tag="extra_files")
    (source / "README.txt").write_text("not part of an image", encoding="utf-8")
    (source / "notes.bin").write_bytes(b"junk")
    service = ImageService(paths)

    image = service.import_directory(source)
    assert any("ignoring unrecognised file" in message for message in service.warnings)
    destination = Path(image.local_path)
    assert not (destination / "README.txt").exists()
    assert (destination / "system.img").is_file()


def test_import_uses_duck_typed_host_backend(tmp_path: Path) -> None:
    host_sdk_root = tmp_path / "host-sdk"
    paths = make_paths(tmp_path)
    paths.sdk_root = None  # force the service to ask the host backend
    host = FakeHost(host_sdk_root, CpuArch.ARM64)
    source = make_custom_source(
        tmp_path, api=33, tag="host_custom", with_properties=False, name="host-src"
    )
    service = ImageService(paths, host=host)

    image = service.import_directory(source, api_level=33)
    assert image.abi == "arm64-v8a"  # inferred from the duck-typed host ABI
    assert any("defaulting to the host ABI" in message for message in service.warnings)
    assert Path(image.local_path).is_relative_to(host_sdk_root / "system-images")


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------


def test_download_dry_run_does_not_install(tmp_path: Path) -> None:
    backend = FakeSdk()
    service = ImageService(make_paths(tmp_path), sdk=backend)

    planned = service.download(STOCK_PLAYSTORE_36, dry_run=True)
    assert planned.installed is False
    assert planned.metadata["dry_run"] is True
    assert planned.metadata["package"] == STOCK_PLAYSTORE_36
    assert backend.install_calls == []


def test_download_delegates_to_sdk_backend(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    backend = FakeSdk()

    def materialize(packages: list[str]) -> None:
        assert packages == [STOCK_PLAYSTORE_36]
        write_installed_image(paths.sdk_root, api=36, tag="google_apis_playstore")

    backend.on_install = materialize
    service = ImageService(paths, sdk=backend, accept_licenses=True)

    image = service.download(STOCK_PLAYSTORE_36)
    assert image.installed is True
    assert image.id == STOCK_PLAYSTORE_36
    assert Path(image.local_path) == (
        paths.sdk_root / "system-images" / "android-36" / "google_apis_playstore" / "x86_64"
    )
    assert backend.install_calls == [
        {
            "packages": [STOCK_PLAYSTORE_36],
            "accept_licenses": True,
            "dry_run": False,
        }
    ]


def test_download_trusts_backend_success_without_local_files(tmp_path: Path) -> None:
    backend = FakeSdk()
    service = ImageService(make_paths(tmp_path), sdk=backend)

    image = service.download(STOCK_PLAYSTORE_36)
    assert image.installed is True
    assert backend.install_calls[0]["packages"] == [STOCK_PLAYSTORE_36]
    assert any("no local files were found" in message for message in service.warnings)


def test_download_verifies_catalogue_checksum(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    payload = b"checksummed-image"
    ok_package = "system-images;android-35;sha_tag;x86_64"
    corrupt_package = "system-images;android-35;sha_tag_corrupt;x86_64"
    catalogue_path = tmp_path / "catalogue.json"

    def write_catalogue(package: str, checksum: str) -> None:
        catalogue_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "images": [
                        {
                            "id": package,
                            "package": package,
                            "api_level": 35,
                            "tag": package.split(";")[2],
                            "abi": "x86_64",
                            "kind": "custom",
                            "display_name": "Checksum test image",
                            "sha256": checksum,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def materialize(tag: str) -> Any:
        def _install(packages: list[str]) -> None:
            assert len(packages) == 1
            directory = paths.sdk_root / "system-images" / "android-35" / tag / "x86_64"
            directory.mkdir(parents=True)
            (directory / "system.img").write_bytes(payload)
            write_source_properties(directory, api=35, tag=tag, abi="x86_64")

        return _install

    write_catalogue(ok_package, hashlib.sha256(payload).hexdigest())
    service = ImageService(
        paths,
        sdk=FakeSdk(on_install=materialize("sha_tag")),
        catalogue_path=catalogue_path,
    )
    assert service.download(ok_package).installed is True

    write_catalogue(corrupt_package, "0" * 64)
    corrupted = ImageService(
        paths,
        sdk=FakeSdk(on_install=materialize("sha_tag_corrupt")),
        catalogue_path=catalogue_path,
    )
    with pytest.raises(ImageError, match="SHA-256 mismatch"):
        corrupted.download(corrupt_package)


def test_download_failures_are_typed(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    with pytest.raises(ToolError):
        ImageService(paths).download(STOCK_PLAYSTORE_36)  # no backend at all

    failing = ImageService(paths, sdk=FakeSdk(fail_install=True))
    with pytest.raises(ToolError):
        failing.download(STOCK_PLAYSTORE_36)

    exploding = ImageService(paths, sdk=FakeSdk(raise_install=RuntimeError("boom")))
    with pytest.raises(ToolError, match="failed"):
        exploding.download(STOCK_PLAYSTORE_36)

    with pytest.raises(NotFoundError):
        ImageService(paths).download("system-images;android-99;missing;x86_64")
    with pytest.raises(ImageError):
        ImageService(paths).download("system-images;android-99;missing;x86_64")


def test_download_marks_custom_missing_directory_as_error(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    registry = {
        "version": 1,
        "images": [
            {
                "id": "custom-gone",
                "api_level": 34,
                "tag": "gone",
                "abi": "x86_64",
                "package": "system-images;android-34;gone;x86_64",
                "display_name": "Gone",
                "local_path": str(tmp_path / "does-not-exist"),
                "custom": True,
            }
        ],
    }
    paths.data_dir.mkdir(parents=True)
    (paths.data_dir / "custom_images.json").write_text(json.dumps(registry), encoding="utf-8")
    service = ImageService(paths)

    with pytest.raises(ImageError, match="not installed"):
        service.download("custom-gone", dry_run=False)


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def test_refresh_uses_backend_refresh_when_supported(tmp_path: Path) -> None:
    backend = FakeSdk()
    service = ImageService(make_paths(tmp_path), sdk=backend)

    images = service.refresh(timeout_s=45)
    assert images
    assert backend.refresh_calls == [45]


def test_refresh_without_backend_reloads(tmp_path: Path) -> None:
    service = ImageService(make_paths(tmp_path))
    assert service.refresh()
    assert service.list_images(installed_only=True) == []


def test_refresh_backend_failure_is_tool_error(tmp_path: Path) -> None:
    backend = FakeSdk(fail_refresh=RuntimeError("sdkmanager exploded"))
    service = ImageService(make_paths(tmp_path), sdk=backend)

    with pytest.raises(ToolError, match="refresh failed"):
        service.refresh()


# ---------------------------------------------------------------------------
# OOBE matrix
# ---------------------------------------------------------------------------


def test_oobe_matrix_merges_seed_and_persisted_records(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    service = ImageService(paths)

    matrix = service.oobe_matrix()
    assert matrix.version == "1"
    assert matrix.records
    seeded = matrix.get(STOCK_PLAYSTORE_36)
    assert seeded is not None
    assert seeded.status is OobeStatus.UNVERIFIED
    assert matrix.get(STOCK_GOOGLE_APIS_35) is not None

    record = OobeRecord(
        image_id=STOCK_PLAYSTORE_36,
        api_level=36,
        tag="google_apis_playstore",
        abi="x86_64",
        status=OobeStatus.VERIFIED,
        activity="com.google.android.setupwizard/.SetupWizardActivity",
        boot_seconds=42.5,
        checked_by="test",
        checked_at=utcnow(),
        notes="fakes-backed verification only",
        evidence=["research/fake/playstore36.txt"],
    )
    service.update_oobe_record(record)

    matrix_file = paths.oobe_matrix_path
    assert matrix_file.is_file()
    persisted = json.loads(matrix_file.read_text(encoding="utf-8"))
    saved = {record["image_id"]: record for record in persisted["records"]}
    assert saved[STOCK_PLAYSTORE_36]["status"] == "verified"
    assert saved[STOCK_GOOGLE_APIS_35]["status"] == "unverified"

    # A fresh service instance sees the persisted record (JSON survives restarts).
    restarted = ImageService(paths)
    merged = restarted.oobe_matrix()
    assert merged.get(STOCK_PLAYSTORE_36).status is OobeStatus.VERIFIED
    assert merged.get(STOCK_GOOGLE_APIS_35).status is OobeStatus.UNVERIFIED
    listed = find_image(restarted.list_images(), STOCK_PLAYSTORE_36)
    assert listed.oobe_status is OobeStatus.VERIFIED


def test_oobe_failed_record_emits_warning(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    service = ImageService(paths)
    failed = OobeRecord(
        image_id=STOCK_GOOGLE_APIS_35,
        api_level=35,
        tag="google_apis",
        abi="x86_64",
        status=OobeStatus.FAILED,
        notes="setup wizard never appeared",
    )
    service.update_oobe_record(failed)

    assert any("OOBE verification failed" in message for message in service.warnings)
    assert service.oobe_matrix().get(STOCK_GOOGLE_APIS_35).status is OobeStatus.FAILED
    assert find_image(service.list_images(), STOCK_GOOGLE_APIS_35).oobe_status is OobeStatus.FAILED


def test_oobe_matrix_marks_unknown_images_unverified(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    service = ImageService(paths)
    source = make_custom_source(tmp_path, api=34, tag="matrix_custom")
    imported = service.import_directory(source)

    matrix = service.oobe_matrix()
    record = matrix.get(imported.id)
    assert record is not None
    assert record.status is OobeStatus.UNVERIFIED
    assert find_image(service.list_images(), imported.id).oobe_status is OobeStatus.UNVERIFIED


def test_oobe_matrix_store_merges_seed_and_ignores_unreadable_persisted(tmp_path: Path) -> None:
    matrix_path = tmp_path / "data" / "oobe_matrix.json"
    matrix_path.parent.mkdir(parents=True)
    matrix_path.write_text("{corrupt", encoding="utf-8")
    store = OobeMatrixStore(matrix_path)

    matrix = store.load()
    assert matrix.records  # falls back to the shipped seed
    assert any("unreadable" in message for message in store.warnings)
    assert matrix.get(STOCK_PLAYSTORE_36).status is OobeStatus.UNVERIFIED


# ---------------------------------------------------------------------------
# Determinism / safety
# ---------------------------------------------------------------------------


def test_operations_open_no_network_sockets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = make_paths(tmp_path)
    backend = FakeSdk()
    service = ImageService(paths, sdk=backend)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a network socket was opened during an offline operation")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)

    service.list_images()
    service.download(STOCK_PLAYSTORE_36, dry_run=True)
    service.oobe_matrix()
    source = make_custom_source(tmp_path, api=35, tag="offline_custom")
    service.import_directory(source)
    service.update_oobe_record(
        OobeRecord(image_id="offline", status=OobeStatus.UNVERIFIED)
    )


def test_registry_and_matrix_contain_no_credentials(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    service = ImageService(paths)
    source = make_custom_source(tmp_path, api=35, tag="plain_custom")
    service.import_directory(source)
    service.update_oobe_record(OobeRecord(image_id="plain_custom", status=OobeStatus.UNVERIFIED))

    for artifact in (paths.data_dir / "custom_images.json", paths.oobe_matrix_path):
        text = artifact.read_text(encoding="utf-8").lower()
        for needle in ("password", "token", "authorization"):
            assert needle not in text
