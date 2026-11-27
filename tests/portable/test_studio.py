"""Portable Android Studio config tests: STUDIO_PROPERTIES, vmoptions, leftovers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from avdfleet.portable.env import build_environment
from avdfleet.portable.studio import (
    STUDIO_LEFTOVERS,
    STUDIO_PROPERTIES_FILENAME,
    STUDIO_VMOPTIONS_FILENAME,
    StudioPaths,
    build_studio_properties,
    build_studio_vmoptions,
    leftover_documentation,
    studio_env,
    studio_leftovers,
    write_studio_config,
)

try:  # tests are a package in the repo layout
    from .conftest import HOST_ARCH, HOST_OS, populate_runtime_dirs, write_marker
except ImportError:  # pragma: no cover - collected without package __init__
    from conftest import HOST_ARCH, HOST_OS, populate_runtime_dirs, write_marker  # type: ignore[no-redef]

pytestmark = pytest.mark.portable


def test_studio_properties_redirect_into_bundle(tmp_path: Path) -> None:
    content = build_studio_properties(tmp_path)
    for key in (
        "idea.config.path=",
        "idea.system.path=",
        "idea.plugins.path=",
        "idea.log.path=",
        "idea.caches.path=",
    ):
        assert key in content, key
    state = tmp_path / "data" / "studio"
    assert (state / "config").as_posix() in content
    assert (state / "system").as_posix() in content
    assert (state / "plugins").as_posix() in content
    assert (state / "log").as_posix() in content
    assert "\\" not in content  # properties use forward slashes on every host
    assert content.endswith("\n")


def test_studio_vmoptions(tmp_path: Path) -> None:
    content = build_studio_vmoptions(tmp_path, heap_mb=3072)
    assert "-Xmx3072m" in content
    assert "-Didea.config.path=" in content
    assert "-Didea.system.path=" in content
    assert "-Djava.io.tmpdir=" in content
    assert str((tmp_path / "tmp").as_posix()) in content
    assert "\\" not in content


def test_studio_paths_layout(tmp_path: Path) -> None:
    paths = StudioPaths.for_root(tmp_path)
    assert paths.properties == tmp_path / "studio" / STUDIO_PROPERTIES_FILENAME
    assert paths.vmoptions == tmp_path / "studio" / STUDIO_VMOPTIONS_FILENAME
    assert paths.config.parent == tmp_path / "data" / "studio"
    dumped = paths.as_dict()
    assert set(dumped) == {"root", "properties", "vmoptions", "config", "system", "plugins", "log", "caches"}


def test_write_studio_config_idempotent(tmp_path: Path) -> None:
    first = write_studio_config(tmp_path)
    assert len(first.written) == 2
    assert first.warnings == []
    properties = tmp_path / "studio" / STUDIO_PROPERTIES_FILENAME
    vmoptions = tmp_path / "studio" / STUDIO_VMOPTIONS_FILENAME
    assert properties.is_file() and vmoptions.is_file()
    assert properties.read_text(encoding="utf-8") == build_studio_properties(tmp_path)
    second = write_studio_config(tmp_path)
    assert second.written == []
    assert len(second.unchanged) == 2
    assert second.changed is False


def test_write_studio_config_dry_run(tmp_path: Path) -> None:
    result = write_studio_config(tmp_path, dry_run=True)
    assert result.dry_run is True
    assert len(result.written) == 2
    assert not (tmp_path / "studio").exists()


def test_studio_env(tmp_path: Path) -> None:
    write_studio_config(tmp_path)
    env = studio_env(tmp_path)
    assert env["STUDIO_PROPERTIES"] == str(tmp_path / "studio" / STUDIO_PROPERTIES_FILENAME)
    assert env["STUDIO_VM_OPTIONS"] == str(tmp_path / "studio" / STUDIO_VMOPTIONS_FILENAME)
    assert studio_env(tmp_path, require_files=True)["STUDIO_PROPERTIES"] == env["STUDIO_PROPERTIES"]
    fresh = tmp_path / "fresh"
    assert studio_env(fresh, require_files=True) == {}


def test_environment_exports_studio_variables(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    populate_runtime_dirs(root)
    write_marker(root)
    write_studio_config(root)
    environment = build_environment(root, host_os=HOST_OS, host_arch=HOST_ARCH, base_env={})
    assert environment.env["STUDIO_PROPERTIES"] == str(root.resolve() / "studio" / STUDIO_PROPERTIES_FILENAME)
    assert environment.env["STUDIO_VM_OPTIONS"] == str(root.resolve() / "studio" / STUDIO_VMOPTIONS_FILENAME)


@pytest.mark.parametrize("host", ["windows", "macos", "linux"])
def test_studio_leftovers_per_host(host: str) -> None:
    leftovers = studio_leftovers(host)
    assert len(leftovers) >= 5
    for item in leftovers:
        assert item.host_os in {"all", host}
        assert item.path
        assert item.kind in {"config", "cache", "log", "registry", "database", "temp"}
        assert isinstance(item.unavoidable, bool)
    assert sum(1 for item in leftovers if item.host_os == "all") >= 1
    assert sum(1 for item in leftovers if item.host_os == host) >= 3


def test_studio_leftovers_specifics() -> None:
    windows_paths = " ".join(item.path for item in studio_leftovers("windows"))
    assert "HKCU\\Software\\Google" in windows_paths
    assert "%APPDATA%" in windows_paths
    macos_paths = " ".join(item.path for item in studio_leftovers("macos"))
    assert "com.google.android.studio" in macos_paths
    assert "LaunchServices" in macos_paths
    linux_paths = " ".join(item.path for item in studio_leftovers("linux"))
    assert ".config/Google/AndroidStudio" in linux_paths
    assert "XDG_RUNTIME_DIR" in linux_paths
    assert len(studio_leftovers()) == len(STUDIO_LEFTOVERS)


def test_leftover_documentation_json() -> None:
    documentation = leftover_documentation(HOST_OS)
    assert documentation
    assert all(set(item) == {"host_os", "path", "kind", "note", "unavoidable"} for item in documentation)
    json.dumps(documentation)  # must be API-serialisable


def test_studio_properties_are_deterministic(tmp_path: Path) -> None:
    assert build_studio_properties(tmp_path) == build_studio_properties(tmp_path)
    assert build_studio_vmoptions(tmp_path) == build_studio_vmoptions(tmp_path)
