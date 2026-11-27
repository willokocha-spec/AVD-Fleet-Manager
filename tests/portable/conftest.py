"""Shared fixtures and synthetic-tree helpers for portable-edition tests.

Everything here is offline and filesystem-synthetic: no real Android SDK, no
network, no emulator.  Markers are written for the *current* host by default so
that host validation passes on any CI OS; tests that simulate another OS pass the
matching ``host_os``/``host_arch`` parameters explicitly.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from avdfleet.portable.bundle.manifest import build_manifest, file_entry, iter_bundle_files, write_manifest
from avdfleet.portable.env.model import MARKER_NAME, host_arch_name, host_os_name

HOST_OS = host_os_name()
HOST_ARCH = host_arch_name()
CAN_RUN_SHELL_SCRIPTS = os.name != "nt"


# ---------------------------------------------------------------------------
# Low-level helpers (also imported by test modules)
# ---------------------------------------------------------------------------


def make_file(path: Path, content: str | bytes = "x", *, executable: bool = False) -> Path:
    """Create a file (and parents), optionally setting the POSIX execute bit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    if executable and os.name != "nt":
        path.chmod(path.stat().st_mode | 0o755)
    return path


def marker_payload(
    root: Path,
    *,
    host_os: str | None = None,
    host_arch: str | None = None,
    recorded_root: str | None = None,
    entries: list[dict] | None = None,
    **overrides: object,
) -> dict:
    payload: dict = {
        "format_version": "1",
        "name": "avdfleet-portable",
        "version": "0.1.0",
        "created_at": "2026-01-01T00:00:00Z",
        "host_os": host_os or HOST_OS,
        "host_arch": host_arch or HOST_ARCH,
        "root": recorded_root if recorded_root is not None else str(root),
        "offline": False,
        "python_version": "3.12.14",
        "tool_versions": {"avdfleet": "0.1.0", "python": "3.12.14"},
        "warnings": [],
        "entries": entries or [],
    }
    payload.update(overrides)
    return payload


def write_marker(
    root: Path,
    *,
    host_os: str | None = None,
    host_arch: str | None = None,
    recorded_root: str | None = None,
    entries: list[dict] | None = None,
    **overrides: object,
) -> Path:
    """Write a minimal valid ``avdfleet-portable.json`` marker."""
    root.mkdir(parents=True, exist_ok=True)
    payload = marker_payload(
        root,
        host_os=host_os,
        host_arch=host_arch,
        recorded_root=recorded_root,
        entries=entries,
        **overrides,
    )
    marker = root / MARKER_NAME
    marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return marker


def populate_runtime_dirs(
    root: Path,
    *,
    python: bool = True,
    sdk: bool = True,
    ui: bool = True,
    venv: bool = False,
    prep_root: str | None = None,
) -> Path:
    """Create a minimal *bundle layout* (not necessarily a valid manifest)."""
    if python:
        make_file(root / "python" / "bin" / "python3", "#!/bin/sh\nexit 0\n", executable=True)
        make_file(root / "python" / "lib" / "python3.12" / "os.py", "# stdlib placeholder")
        if HOST_OS == "windows":
            make_file(root / "python" / "python.exe", "MZ-placeholder")
    if sdk:
        make_file(root / "sdk" / "platform-tools" / "adb", "adb-placeholder", executable=True)
        make_file(root / "sdk" / "emulator" / "emulator", "emulator-placeholder", executable=True)
        make_file(
            root / "sdk" / "cmdline-tools" / "latest" / "bin" / "sdkmanager",
            "sdkmanager-placeholder",
            executable=True,
        )
        if HOST_OS == "windows":
            make_file(root / "sdk" / "platform-tools" / "adb.exe", "MZ-placeholder")
            make_file(root / "sdk" / "emulator" / "emulator.exe", "MZ-placeholder")
    if ui:
        make_file(root / "ui" / "index.html", "<html><body>AVD Fleet</body></html>")
    (root / "bin").mkdir(parents=True, exist_ok=True)
    if venv:
        base = prep_root if prep_root is not None else str(root / "prep")
        make_file(root / "venv" / "pyvenv.cfg", f"home = {base}/python\nversion_info = 3.12.14\n")
        bin_name = "Scripts" if HOST_OS == "windows" else "bin"
        script = root / "venv" / bin_name / "avdfleet"
        make_file(script, f"#!{base}/python/bin/python3\nprint('avdfleet')\n", executable=True)
        if HOST_OS == "windows":
            make_file(root / "venv" / "Scripts" / "python.exe", "MZ-placeholder")
    return root


def resign_manifest(root: Path, *, recorded_root: str | None = None) -> None:
    """Recompute manifest checksums/root for the current tree (test helper)."""
    from avdfleet.portable.bundle.manifest import read_manifest

    manifest = read_manifest(root)
    entries = [file_entry(root, relative) for relative, _path in iter_bundle_files(root)]
    refreshed = build_manifest(
        entries,
        root=recorded_root if recorded_root is not None else root,
        name=manifest.name,
        version=manifest.version,
        host_os=manifest.host_os.value,
        host_arch=manifest.host_arch.value,
        offline=manifest.offline,
        python_version=manifest.python_version,
        tool_versions=manifest.tool_versions,
        warnings=manifest.warnings,
    )
    write_manifest(root, refreshed)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def portable_root(tmp_path: Path) -> Path:
    """A minimal bundle root with a valid marker for the current host."""
    root = tmp_path / "bundle"
    populate_runtime_dirs(root)
    write_marker(root)
    return root


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    """Prepared component tree the bundle builder copies from (no venv)."""
    source = tmp_path / "source"
    populate_runtime_dirs(source)
    make_file(source / "lib" / "avdfleet" / "__init__.py", "__version__ = '0.1.0'\n")
    return source


@pytest.fixture
def source_tree_with_venv(tmp_path: Path) -> Path:
    """Prepared tree containing a venv built at a different (preparation) root."""
    source = tmp_path / "source"
    populate_runtime_dirs(source, venv=True, prep_root=str(tmp_path / "prepared-elsewhere"))
    make_file(source / "lib" / "avdfleet" / "__init__.py", "__version__ = '0.1.0'\n")
    return source


@pytest.fixture
def built_bundle(tmp_path: Path, source_tree: Path) -> Path:
    """A real bundle produced by ``PortableBundleBuilder`` (no offline cache)."""
    from avdfleet.portable.bundle import PortableBundleBuilder

    builder = PortableBundleBuilder(source_root=source_tree, python_version="3.12.14")
    destination = tmp_path / "bundle"
    builder.build(destination)
    return destination
