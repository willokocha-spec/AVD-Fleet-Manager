"""Bundle builder, manifest, cache and verify tests (offline, synthetic trees)."""

from __future__ import annotations

import json
import socket
import tomllib
from pathlib import Path

import pytest

from avdfleet.core.errors import PortableError
from avdfleet.core.interfaces import BundleBuilder
from avdfleet.portable.bundle import (
    BundleVerificationError,
    PortableBundleBuilder,
    SourcePlan,
    read_manifest,
    resolve_sources,
    verify_bundle,
)
from avdfleet.portable.checks import FileSystemInfo, FileSystemKind
from avdfleet.portable.relocate import relocate_bundle

try:  # tests are a package in the repo layout
    from .conftest import HOST_ARCH, HOST_OS, make_file, populate_runtime_dirs
except ImportError:  # pragma: no cover - collected without package __init__
    from conftest import HOST_ARCH, HOST_OS, make_file, populate_runtime_dirs  # type: ignore[no-redef]

pytestmark = pytest.mark.portable

LAUNCHER_NAME = {
    "windows": "AVD Fleet.cmd",
    "macos": "AVD Fleet.command",
    "linux": "avdfleet.sh",
}[HOST_OS]


def make_builder(
    *,
    source_root: Path | None = None,
    cache_dir: Path | None = None,
    filesystem_kind: FileSystemKind | None = None,
    **kwargs: object,
) -> PortableBundleBuilder:
    probe = (lambda path: FileSystemInfo(kind=filesystem_kind, raw=filesystem_kind.value)) if filesystem_kind else None
    return PortableBundleBuilder(
        source_root=source_root,
        cache_dir=cache_dir,
        python_version="3.12.14",
        filesystem_probe=probe,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def test_build_minimal_bundle(tmp_path: Path, source_tree: Path) -> None:
    destination = tmp_path / "bundle"
    manifest = make_builder(source_root=source_tree).build(destination)

    assert (destination / "avdfleet-portable.json").is_file()
    loaded = read_manifest(destination)
    assert loaded.format_version == "1"
    assert loaded.name == "avdfleet-portable"
    assert loaded.host_os.value == HOST_OS
    assert loaded.host_arch.value == HOST_ARCH
    assert loaded.root == str(destination.resolve())
    assert loaded.offline is False
    assert loaded.entries, "manifest must list bundle files"
    assert loaded.total_bytes == sum(entry.size for entry in loaded.entries)
    assert manifest.model_dump() == loaded.model_dump()

    entries = {entry.path for entry in loaded.entries}
    for expected in (
        "python/bin/python3",
        "sdk/platform-tools/adb",
        "sdk/emulator/emulator",
        "ui/index.html",
        "config/config.toml",
        "studio/idea.properties",
        "studio/studio.vmoptions",
        LAUNCHER_NAME,
        f"launchers/{LAUNCHER_NAME}",
    ):
        assert expected in entries, expected
    assert "avdfleet-portable.json" not in entries
    assert not any(entry.path.startswith(("data/", "logs/", "tmp/", "avd/")) for entry in loaded.entries)

    for runtime_dir in ("data", "config", "cache", "logs", "tmp", "avd", "android", "bin"):
        assert (destination / runtime_dir).is_dir()

    config = (destination / "config" / "config.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(config)
    assert parsed["edition"] == "portable"
    assert parsed["portable_root"] == destination.as_posix()
    assert parsed["data_dir"] == (destination / "data").as_posix()
    assert parsed["api_port"] == 8765
    assert parsed["telemetry"] is False

    payload = json.loads((destination / "avdfleet-portable.json").read_text(encoding="utf-8"))
    required = {"format_version", "name", "version", "created_at", "host_os", "host_arch", "root", "entries"}
    assert required <= set(payload)
    assert all(set(entry) == {"path", "sha256", "size", "executable"} for entry in payload["entries"])


def test_builder_implements_protocol(source_tree: Path) -> None:
    builder = make_builder(source_root=source_tree)
    assert isinstance(builder, BundleBuilder)


def test_build_is_atomic_and_leaves_no_temp(tmp_path: Path, source_tree: Path) -> None:
    destination = tmp_path / "bundle"
    make_builder(source_root=source_tree).build(destination)
    assert list(tmp_path.glob(".bundle.build-*")) == []


def test_refuses_non_empty_destination(tmp_path: Path, source_tree: Path) -> None:
    destination = tmp_path / "bundle"
    make_file(destination / "keep.txt", "important")
    with pytest.raises(PortableError, match="not empty"):
        make_builder(source_root=source_tree).build(destination)
    assert (destination / "keep.txt").is_file()


def test_force_backs_up_existing_destination(tmp_path: Path, source_tree: Path) -> None:
    destination = tmp_path / "bundle"
    make_file(destination / "keep.txt", "important")
    manifest = make_builder(source_root=source_tree).build(destination, force=True)
    backups = list(tmp_path.glob("bundle.backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "keep.txt").is_file()
    assert any("backup" in warning for warning in manifest.warnings)


def test_refuses_fat32_destination(tmp_path: Path, source_tree: Path) -> None:
    builder = make_builder(source_root=source_tree, filesystem_kind=FileSystemKind.FAT32)
    with pytest.raises(PortableError, match="FAT32"):
        builder.build(tmp_path / "bundle")


def test_exfat_destination_warns(tmp_path: Path, source_tree: Path) -> None:
    builder = make_builder(source_root=source_tree, filesystem_kind=FileSystemKind.EXFAT)
    manifest = builder.build(tmp_path / "bundle")
    assert any("exFAT" in warning for warning in manifest.warnings)


def test_dry_run_writes_nothing(tmp_path: Path, source_tree: Path) -> None:
    destination = tmp_path / "bundle"
    manifest = make_builder(source_root=source_tree).build(destination, dry_run=True)
    assert not destination.exists()
    assert manifest.entries == []
    assert any("dry run" in warning for warning in manifest.warnings)
    assert manifest.root == str(destination.resolve())


def test_missing_sources_raise(tmp_path: Path) -> None:
    builder = PortableBundleBuilder()
    with pytest.raises(PortableError, match="missing required"):
        builder.build(tmp_path / "bundle")


def test_resolve_sources_reports_missing(tmp_path: Path) -> None:
    plan = resolve_sources(source_root=tmp_path / "absent", cache_dir=None)
    assert plan.missing_required == ["python", "sdk", "ui"]
    assert plan.network_used is False
    with pytest.raises(PortableError, match="missing required"):
        plan.require()
    assert isinstance(plan, SourcePlan)


def test_offline_requires_populated_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    (cache / "downloads").mkdir(parents=True)
    builder = PortableBundleBuilder(cache_dir=cache)
    with pytest.raises(PortableError, match="offline"):
        builder.build(tmp_path / "bundle", offline=True)


@pytest.mark.parametrize("offline", [False, True])
def test_build_from_cache(tmp_path: Path, offline: bool) -> None:
    cache = tmp_path / "cache"
    populate_runtime_dirs(cache)
    make_file(cache / "downloads" / "platform-tools.zip", "zip-bytes")
    builder = PortableBundleBuilder(cache_dir=cache, python_version="3.12.14")
    destination = tmp_path / "bundle"
    manifest = builder.build(destination, offline=offline)
    assert manifest.offline is offline
    assert (destination / "cache" / "downloads" / "platform-tools.zip").is_file()
    if offline:
        assert any("Offline build" in warning for warning in manifest.warnings)
    assert manifest.tool_versions["avdfleet"] == "0.1.0"
    assert manifest.tool_versions["python"] == "3.12.14"


def test_offline_build_uses_no_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    populate_runtime_dirs(cache)
    make_file(cache / "downloads" / "sdk.zip", "zip")

    def deny(*args: object, **kwargs: object) -> None:
        raise AssertionError("the bundle builder must not open sockets in offline mode")

    monkeypatch.setattr(socket, "socket", deny)
    manifest = PortableBundleBuilder(cache_dir=cache, python_version="3.12.14").build(
        tmp_path / "bundle", offline=True
    )
    assert manifest.offline is True


# ---------------------------------------------------------------------------
# Verify / inspect
# ---------------------------------------------------------------------------


def test_verify_clean_bundle(built_bundle: Path) -> None:
    report = verify_bundle(built_bundle)
    assert report.ok is True
    assert report.errors == []
    assert report.checked == len(report.manifest.entries)  # type: ignore[union-attr]
    assert report.relocated is False
    assert report.missing == [] and report.modified == [] and report.extra == []


def test_verify_detects_modified_file(built_bundle: Path) -> None:
    target = built_bundle / "lib" / "avdfleet" / "__init__.py"
    target.write_text("__version__ = 'tampered'\n", encoding="utf-8")
    report = verify_bundle(built_bundle)
    assert report.ok is False
    kinds = {issue.kind for issue in report.errors}
    assert "checksum_mismatch" in kinds or "size_mismatch" in kinds
    assert "lib/avdfleet/__init__.py" in report.modified


def test_verify_detects_missing_and_extra(built_bundle: Path) -> None:
    (built_bundle / "ui" / "index.html").unlink()
    make_file(built_bundle / "sneaky.bin", "not in manifest")
    report = verify_bundle(built_bundle)
    assert report.ok is False
    assert report.missing == ["ui/index.html"]
    assert "sneaky.bin" in report.extra


def test_verify_ignores_runtime_writes(built_bundle: Path) -> None:
    make_file(built_bundle / "data" / "avdfleet.db", "sqlite")
    make_file(built_bundle / "logs" / "avdfleet.log", "log line")
    make_file(built_bundle / "tmp" / "scratch", "tmp")
    make_file(built_bundle / "config" / "portable-state.json", "{}")
    make_file(built_bundle / "lib" / "avdfleet" / "__pycache__" / "x.pyc", "pyc")
    report = verify_bundle(built_bundle)
    assert report.ok is True, [issue.to_dict() for issue in report.errors]


def test_verify_detects_path_traversal_entry(built_bundle: Path) -> None:
    marker = built_bundle / "avdfleet-portable.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["entries"][0]["path"] = "../../outside.txt"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    report = verify_bundle(built_bundle)
    assert report.ok is False
    assert any(issue.kind == "path_traversal" for issue in report.errors)


def test_verify_detects_symlink(built_bundle: Path) -> None:
    link = built_bundle / "lib" / "escape"
    try:
        link.symlink_to(built_bundle / "python", target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without symlink support
        pytest.skip("host does not allow creating symlinks")
    report = verify_bundle(built_bundle)
    assert report.ok is False
    assert any(issue.kind in {"symlink", "path_traversal"} for issue in report.errors)


def test_verify_detects_manifest_issue(built_bundle: Path) -> None:
    (built_bundle / "avdfleet-portable.json").write_text("{not json", encoding="utf-8")
    report = verify_bundle(built_bundle)
    assert report.ok is False
    assert report.errors[0].kind == "manifest"


def test_builder_verify_raises_on_tamper(built_bundle: Path) -> None:
    (built_bundle / "ui" / "index.html").write_text("tampered", encoding="utf-8")
    builder = PortableBundleBuilder()
    with pytest.raises(BundleVerificationError) as excinfo:
        builder.verify(built_bundle)
    assert excinfo.value.report.ok is False


def test_builder_inspect_api(built_bundle: Path) -> None:
    builder = PortableBundleBuilder()
    manifest = builder.inspect(built_bundle)
    assert manifest.name == "avdfleet-portable"
    summary = builder.inspect_summary(built_bundle)
    assert summary["entry_count"] == len(manifest.entries)
    assert summary["files_on_disk"] >= summary["entry_count"]
    assert summary["components"]["python"] >= 2
    assert summary["missing"] == []
    inspected = builder.inspect_bundle(built_bundle)
    assert inspected.total_bytes > 0
    assert inspected.largest_files


def test_verify_reports_moved_bundle(built_bundle: Path) -> None:
    moved = built_bundle.with_name("moved-bundle")
    built_bundle.rename(moved)
    report = verify_bundle(moved)
    assert report.relocated is True
    assert report.ok is True, [issue.to_dict() for issue in report.errors]
    assert any(issue.kind == "relocated" for issue in report.warnings)

    result = relocate_bundle(moved)
    assert result.changed_files > 0
    after = verify_bundle(moved)
    assert after.relocated is False
    assert after.ok is True, [issue.to_dict() for issue in after.errors]


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------


def test_manifest_matches_contract_schema(built_bundle: Path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = Path(__file__).resolve().parents[2] / "contracts" / "bundle.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    payload = json.loads((built_bundle / "avdfleet-portable.json").read_text(encoding="utf-8"))
    jsonschema.validate(instance=payload, schema=schema)


def test_dry_run_does_not_create_destination_parent(tmp_path: Path, source_tree: Path) -> None:
    destination = tmp_path / "nested" / "deeper" / "bundle"
    manifest = make_builder(source_root=source_tree).build(destination, dry_run=True)
    assert not (tmp_path / "nested").exists()
    assert manifest.entries == []


def test_manifest_from_source_cache_resolution(tmp_path: Path) -> None:
    source = tmp_path / "source"
    populate_runtime_dirs(source)
    plan = resolve_sources(source_root=source)
    assert "python" in plan.components and "sdk" in plan.components and "ui" in plan.components
    assert plan.missing_required == []
    assert plan.missing_optional  # lib/venv/node/jdk/studio are optional


def test_resolve_sources_accepts_r7_runtime_layout(tmp_path: Path) -> None:
    source = tmp_path / "prepared"
    make_file(source / "runtime" / "python" / "bin" / "python3", "#!/bin/sh\n")
    make_file(source / "runtime" / "jre" / "bin" / "java", "binary")
    make_file(source / "runtime" / "app-site-packages" / "avdfleet" / "__init__.py", "")
    make_file(source / "sdk" / "platform-tools" / "adb", "adb")
    make_file(source / "ui" / "index.html", "<html></html>")
    plan = resolve_sources(source_root=source)
    assert plan.missing_required == []
    assert plan.components["python"] == source / "runtime" / "python"
    assert plan.components["jdk"] == source / "runtime" / "jre"
    assert plan.components["lib"] == source / "runtime" / "app-site-packages"

    destination = tmp_path / "bundle"
    manifest = PortableBundleBuilder(source_root=source, python_version="3.12.14").build(destination)
    entry_paths = {entry.path for entry in manifest.entries}
    assert "python/bin/python3" in entry_paths  # normalised into the bundle layout
    assert "lib/avdfleet/__init__.py" in entry_paths
    assert "jdk/bin/java" in entry_paths


def test_entry_executable_flag(built_bundle: Path) -> None:
    from avdfleet.portable.bundle.manifest import entry_executable

    manifest = read_manifest(built_bundle)
    entries = {entry.path: entry for entry in manifest.entries}
    entry = entries["python/bin/python3"]
    assert entry.executable == entry_executable(built_bundle / "python" / "bin" / "python3")


def test_manifest_summary(built_bundle: Path) -> None:
    from avdfleet.portable.bundle import manifest_summary

    summary = manifest_summary(read_manifest(built_bundle))
    assert summary["entry_count"] > 0
    assert summary["total_bytes"] > 0
    assert summary["host_os"] == HOST_OS
    assert summary["components"]
    assert len(summary["largest_files"]) <= 5
