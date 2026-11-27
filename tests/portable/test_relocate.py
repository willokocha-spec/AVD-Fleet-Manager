"""Relocation tests: AVD/config rewriting, venv repair, snapshot invalidation, moved bundles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from avdfleet.core.models import sha256_file
from avdfleet.portable.bundle import PortableBundleBuilder, read_manifest, verify_bundle
from avdfleet.portable.env import build_environment, detect_root_change, load_root_state
from avdfleet.portable.relocate import (
    RootReplacer,
    find_snapshots,
    find_stale_roots,
    inspect_venv,
    invalidate_stale_snapshots,
    relocate_bundle,
    repair_venv,
    rewrite_avd_config_ini,
    rewrite_avd_ini,
    rewrite_config_toml,
    rewrite_launcher_working_dir,
    rewrite_text_roots,
    snapshot_root_reference,
    venv,
)

try:  # tests are a package in the repo layout
    from .conftest import HOST_ARCH, HOST_OS, make_file, populate_runtime_dirs, write_marker
except ImportError:  # pragma: no cover - collected without package __init__
    from conftest import HOST_ARCH, HOST_OS, make_file, populate_runtime_dirs, write_marker  # type: ignore[no-redef]

pytestmark = pytest.mark.portable
NEW_ROOT = Path("/new/bundle") if HOST_OS != "windows" else Path("C:/new/bundle")


def new_env(root: Path):
    return build_environment(root, host_os=HOST_OS, host_arch=HOST_ARCH, base_env={})


# ---------------------------------------------------------------------------
# Text rewriting
# ---------------------------------------------------------------------------


def test_rewrite_avd_ini_windows_style(tmp_path: Path) -> None:
    old = r"C:\Old Bundle"
    content = f"[AVD]\npath={old}\\avd\\Pixel.avd\npath.rel=avd\\Pixel.avd\ntarget=android-34\n"
    result = rewrite_avd_ini(content, old_roots=[old], new_root=tmp_path, avd_name="Pixel")
    assert result.changed is True
    keys = {change.key for change in result.changes}
    assert keys == {"path", "path.rel"}
    expected_path = str(tmp_path).replace("/", "\\") + "\\avd\\Pixel.avd"
    assert f"path={expected_path}" in result.content
    assert "path.rel=avd/Pixel.avd" in result.content
    assert "target=android-34" in result.content


def test_rewrite_avd_ini_posix_style(tmp_path: Path) -> None:
    content = "[AVD]\npath=/mnt/old/avd/Pixel.avd\npath.rel=avd/Pixel.avd\n"
    result = rewrite_avd_ini(content, old_roots=["/mnt/old"], new_root=tmp_path, avd_name="Pixel")
    assert result.changed is True
    expected = str(tmp_path).replace("\\", "/") + "/avd/Pixel.avd"
    assert f"path={expected}" in result.content
    assert "path.rel=avd/Pixel.avd" in result.content


def test_rewrite_avd_ini_uses_new_avd_home(tmp_path: Path) -> None:
    content = "[AVD]\npath=C:\\old\\avd\\Pixel.avd\n"
    avd_home = tmp_path / "avd"
    result = rewrite_avd_ini(
        content, old_roots=["C:\\old"], new_root=tmp_path, avd_name="Pixel", avd_home=avd_home
    )
    expected = str(avd_home).replace("/", "\\") + "\\Pixel.avd"
    assert expected in result.content


def test_rewrite_avd_ini_is_idempotent(tmp_path: Path) -> None:
    content = "[AVD]\npath=C:\\old\\avd\\Pixel.avd\npath.rel=avd/Pixel.avd\n"
    first = rewrite_avd_ini(content, old_roots=["C:\\old"], new_root=tmp_path, avd_name="Pixel")
    second = rewrite_avd_ini(
        first.content, old_roots=["C:\\old"], new_root=tmp_path, avd_name="Pixel"
    )
    assert second.changed is False


def test_rewrite_avd_config_ini_keys(tmp_path: Path) -> None:
    old = str(tmp_path / "old-bundle").replace("/", "\\")
    content = (
        "[config]\n"
        f"image.sysdir.1={old}\\sdk\\system-images\\android-34\\google_apis\\x86_64/\n"
        "image.sysdir.2=system-images/android-33/google_apis/x86_64/\n"
        f"skin.path={old}\\skins\\pixel_7\n"
        f"snapshot.path={old}\\avd\\Pixel.avd\\snapshots\\default_boot\n"
        f"disk.dataPartition.path={old}\\avd\\Pixel.avd\\userdata-qemu.img\n"
        f"disk.systemPartition.path={old}\\avd\\Pixel.avd\\system.img\n"
        "hw.lcd.width=1080\n"
    )
    result = rewrite_avd_config_ini(content, old_roots=[old], new_root=tmp_path)
    assert result.changed is True
    keys = {change.key for change in result.changes}
    assert {"image.sysdir.1", "skin.path", "snapshot.path", "disk.dataPartition.path", "disk.systemPartition.path"} <= keys
    assert "image.sysdir.2=system-images/android-33/google_apis/x86_64/" in result.content
    assert "hw.lcd.width=1080" in result.content
    assert old not in result.content
    new_windows = str(tmp_path).replace("/", "\\")
    assert new_windows in result.content


def test_rewrite_config_toml_escaped_backslashes(tmp_path: Path) -> None:
    content = 'portable_root = "C:\\\\Old\\\\Bundle"\ndata_dir = "C:\\\\Old\\\\Bundle\\\\data"\n'
    result = rewrite_config_toml(content, old_roots=["C:\\Old\\Bundle"], new_root=tmp_path)
    assert result.changed is True
    assert "C:\\\\Old" not in result.content
    expected = str(tmp_path).replace("/", "\\").replace("\\", "\\\\")
    assert expected in result.content


def test_rewrite_text_roots_case_insensitive() -> None:
    content = 'path = "C:\\OLD\\bundle\\data"'
    result = rewrite_text_roots(content, ["C:\\Old\\Bundle"], "D:\\new")
    assert result.changed is True
    assert "D:\\new\\data" in result.content


def test_rewrite_launcher_working_dir(tmp_path: Path) -> None:
    windows = 'cd /d "C:\\Old Bundle"\n"%PY%" -m avdfleet.portable.launchers.generator start %*\n'
    rewritten = rewrite_launcher_working_dir(windows, old_roots=["C:\\Old Bundle"], new_root=tmp_path)
    assert "Old Bundle" not in rewritten.content
    posix = 'cd "/mnt/old bundle"\n'
    posix_rewritten = rewrite_launcher_working_dir(posix, old_roots=["/mnt/old bundle"], new_root=tmp_path)
    assert "old bundle" not in posix_rewritten.content


def test_root_replacer_matches_multiple_roots() -> None:
    replacer = RootReplacer(["/a/old", "/b/old"], "/c/new")
    result = replacer.rewrite("x=/a/old/data y=/b/old/avd")
    assert result.changed is True
    assert result.replaced == ("/a/old", "/b/old")
    assert "/a/old" not in result.content and "/b/old" not in result.content


# ---------------------------------------------------------------------------
# Venv repair
# ---------------------------------------------------------------------------


def _make_stale_venv(root: Path, *, prep_root: str) -> Path:
    venv_dir = root / "venv"
    make_file(venv_dir / "pyvenv.cfg", f"home = {prep_root}/python\nversion_info = 3.12.14\n")
    make_file(venv_dir / "bin" / "avdfleet", f"#!{prep_root}/python/bin/python3\nprint('x')\n", executable=True)
    make_file(venv_dir / "bin" / "activate", f'VIRTUAL_ENV="{prep_root}/venv"\n')
    return venv_dir


def test_inspect_and_repair_venv(tmp_path: Path) -> None:
    prep = str(tmp_path / "prepared-elsewhere")
    venv_dir = _make_stale_venv(tmp_path / "bundle", prep_root=prep)
    inspection = inspect_venv(
        venv_dir, old_roots=[prep], current_root=tmp_path / "bundle", host_os=HOST_OS
    )
    assert inspection.exists is True
    assert inspection.needs_repair is True
    assert inspection.stale_shebangs and inspection.stale_home

    dry = repair_venv(
        venv_dir, old_roots=[prep], current_root=tmp_path / "bundle", dry_run=True, host_os=HOST_OS
    )
    assert dry.status == "dry-run"
    assert prep in (venv_dir / "bin" / "avdfleet").read_text(encoding="utf-8")

    repaired = repair_venv(
        venv_dir, old_roots=[prep], current_root=tmp_path / "bundle", host_os=HOST_OS
    )
    assert repaired.status == "repaired"
    assert repaired.ok is True
    assert prep not in (venv_dir / "bin" / "avdfleet").read_text(encoding="utf-8")
    assert prep not in (venv_dir / "pyvenv.cfg").read_text(encoding="utf-8")
    assert inspect_venv(
        venv_dir, old_roots=[prep], current_root=tmp_path / "bundle", host_os=HOST_OS
    ).ok


def test_repair_missing_venv(tmp_path: Path) -> None:
    result = repair_venv(
        tmp_path / "absent", old_roots=["/old"], current_root=tmp_path, host_os=HOST_OS
    )
    assert result.status == "missing"


def test_discover_venv_old_roots_posix_layout(tmp_path: Path) -> None:
    prep = tmp_path / "prepared-elsewhere"
    venv_dir = tmp_path / "bundle" / "venv"
    make_file(venv_dir / "pyvenv.cfg", f"home = {prep.as_posix()}/python/bin\nversion_info = 3.12.14\n")
    make_file(venv_dir / "bin" / "avdfleet", f"#!{prep.as_posix()}/venv/bin/python3\n")
    make_file(venv_dir / "bin" / "activate", f'VIRTUAL_ENV="{prep.as_posix()}/venv"\n')
    roots = venv.discover_venv_old_roots(venv_dir, host_os="linux")
    assert [Path(root) for root in roots] == [prep]
    repaired = repair_venv(
        venv_dir,
        old_roots=roots,
        current_root=tmp_path / "bundle",
        host_os="linux",
    )
    assert repaired.status in {"ok", "repaired"}
    home_line = (venv_dir / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()[0]
    current_posix = str(tmp_path / "bundle").replace("\\", "/")
    assert str(prep) not in home_line
    assert current_posix in home_line
    assert str(prep) not in (venv_dir / "bin" / "avdfleet").read_text(encoding="utf-8")


def test_repair_venv_rebuild_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prep = str(tmp_path / "prepared-elsewhere")
    venv_dir = _make_stale_venv(tmp_path / "bundle", prep_root=prep)
    stale = inspect_venv(venv_dir, old_roots=[prep], current_root=tmp_path / "bundle", host_os=HOST_OS)

    monkeypatch.setattr(venv, "inspect_venv", lambda *args, **kwargs: stale)
    calls: list[list[str]] = []

    def fake_runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, "created"

    result = repair_venv(
        venv_dir,
        old_roots=[prep],
        current_root=tmp_path / "bundle",
        rebuild=True,
        python_executable="C:/bundle/python/bin/python3",
        runner=fake_runner,
        host_os=HOST_OS,
    )
    assert result.status == "rebuilt"
    assert result.rebuilt is True
    assert calls
    assert Path(calls[0][0]) == Path("C:/bundle/python/bin/python3")
    assert calls[0][1:3] == ["-m", "venv"]
    assert any("reinstall" in warning for warning in result.warnings)


# ---------------------------------------------------------------------------
# Snapshot invalidation
# ---------------------------------------------------------------------------


def test_snapshot_invalidation(tmp_path: Path) -> None:
    avd_home = tmp_path / "avd"
    old_root = str(tmp_path / "old-bundle")
    stale = avd_home / "Pixel.avd" / "snapshots" / "default_boot"
    make_file(stale / "snapshot.pb", f"header {old_root}/sdk header".encode())
    utf16 = avd_home / "Pixel.avd" / "snapshots" / "quick"
    make_file(utf16 / "snapshot.pb", f"x {old_root}/avd".encode("utf-16-le"))
    clean = avd_home / "Pixel.avd" / "snapshots" / "clean"
    make_file(clean / "snapshot.pb", b"nothing stale here")
    other = avd_home / "Other.avd" / "snapshots" / "other_boot"
    make_file(other / "snapshot.pb", b"clean too")

    refs = find_snapshots(avd_home)
    assert {ref.name for ref in refs} == {"default_boot", "quick", "clean", "other_boot"}
    assert snapshot_root_reference(stale, [old_root]) == old_root
    assert snapshot_root_reference(clean, [old_root]) is None

    dry = invalidate_stale_snapshots(avd_home, [old_root], dry_run=True)
    assert len(dry) == 2
    assert stale.is_dir()

    live = invalidate_stale_snapshots(avd_home, [old_root])
    assert {item.snapshot for item in live} == {"default_boot", "quick"}
    assert not stale.exists()
    quarantined = avd_home / "Pixel.avd" / "snapshots-invalidated"
    assert (quarantined / "default_boot").is_dir()
    assert (quarantined / "default_boot.invalidated.json").is_file()
    remaining = {ref.name for ref in find_snapshots(avd_home)}
    assert remaining == {"clean", "other_boot"}
    assert (avd_home / "Other.avd" / "snapshots" / "other_boot").is_dir()


# ---------------------------------------------------------------------------
# Full bundle relocation after a move
# ---------------------------------------------------------------------------


def _build_moved_bundle(tmp_path: Path, source_tree_with_venv: Path) -> tuple[Path, str]:
    dest = tmp_path / "bundle"
    PortableBundleBuilder(source_root=source_tree_with_venv, python_version="3.12.14").build(dest)
    old_root = str(dest.resolve())
    old_style = old_root.replace("/", "\\") if HOST_OS == "windows" else old_root
    sep = "\\" if HOST_OS == "windows" else "/"

    make_file(dest / "avd" / "Pixel.ini", f"[AVD]\npath={old_style}{sep}avd{sep}Pixel.avd\npath.rel=avd/Pixel.avd\n")
    make_file(
        dest / "avd" / "Pixel.avd" / "config.ini",
        f"image.sysdir.1={old_style}{sep}sdk{sep}system-images{sep}android-34{sep}google_apis{sep}x86_64/\n"
        f"skin.path={old_style}{sep}skins{sep}pixel_7\n"
        f"snapshot.path={old_style}{sep}avd{sep}Pixel.avd{sep}snapshots{sep}default_boot\n"
        f"disk.dataPartition.path={old_style}{sep}avd{sep}Pixel.avd{sep}userdata-qemu.img\n",
    )
    make_file(
        dest / "avd" / "Pixel.avd" / "snapshots" / "default_boot" / "snapshot.pb",
        f"snapshot referencing {old_root}/sdk".encode(),
    )
    make_file(
        dest / "avd" / "Pixel.avd" / "hardware-qemu.ini",
        f"avd.dir={old_style}\\avd\\Pixel.avd\n",
    )
    make_file(dest / "avd" / "Pixel.avd" / "multiinstance.lock", "")
    moved = tmp_path / "moved-bundle"
    dest.rename(moved)
    return moved, old_root


def test_detect_root_change_after_move(tmp_path: Path, source_tree_with_venv: Path) -> None:
    moved, old_root = _build_moved_bundle(tmp_path, source_tree_with_venv)
    environment = new_env(moved)
    change = detect_root_change(environment)
    assert change.relocated is True
    assert change.recorded_root and change.recorded_root.rstrip("/\\") == old_root.rstrip("/\\")
    assert any("relocation" in warning.lower() for warning in change.warnings)


def test_relocate_moved_bundle_end_to_end(tmp_path: Path, source_tree_with_venv: Path) -> None:
    moved, old_root = _build_moved_bundle(tmp_path, source_tree_with_venv)
    environment = new_env(moved)

    before = verify_bundle(moved)
    assert before.relocated is True
    assert before.ok is True, [issue.to_dict() for issue in before.errors]

    dry = relocate_bundle(moved, environment=environment, dry_run=True)
    assert dry.dry_run is True
    assert dry.changed_files > 0
    assert old_root in (moved / "avd" / "Pixel.ini").read_text(encoding="utf-8")
    assert not (moved / "config" / "relocation-backups").exists()
    assert json.loads((moved / "avdfleet-portable.json").read_text(encoding="utf-8"))["root"] == old_root

    report = relocate_bundle(moved, environment=environment)
    assert report.dry_run is False
    assert report.changed_files > 0
    assert report.manifest_updated is True
    assert report.stale == []
    assert report.backups
    assert (moved / "config" / "relocation-backups").is_dir()

    marker = json.loads((moved / "avdfleet-portable.json").read_text(encoding="utf-8"))
    assert marker["root"] == str(moved.resolve())
    assert any("relocated" in warning for warning in marker["warnings"])

    config = (moved / "config" / "config.toml").read_text(encoding="utf-8")
    assert str(moved.resolve()).replace("\\", "/") in config
    assert old_root not in config

    ini = (moved / "avd" / "Pixel.ini").read_text(encoding="utf-8")
    assert old_root not in ini
    assert str(moved.resolve()) in ini

    avd_config = (moved / "avd" / "Pixel.avd" / "config.ini").read_text(encoding="utf-8")
    assert old_root not in avd_config
    assert str(moved.resolve()) in avd_config

    venv_script = moved / "venv" / ("Scripts" if HOST_OS == "windows" else "bin") / "avdfleet"
    shebang = venv_script.read_text(encoding="utf-8").splitlines()[0]
    assert old_root not in shebang
    assert str(moved.resolve()) in shebang
    pyvenv = (moved / "venv" / "pyvenv.cfg").read_text(encoding="utf-8")
    assert old_root not in pyvenv

    snapshots = moved / "avd" / "Pixel.avd" / "snapshots-invalidated"
    assert (snapshots / "default_boot").is_dir()

    assert report.cleaned
    assert not (moved / "avd" / "Pixel.avd" / "hardware-qemu.ini").exists()
    assert not (moved / "avd" / "Pixel.avd" / "multiinstance.lock").exists()
    assert any("hardware-qemu.ini" in backup for backup in report.backups)

    state = load_root_state(moved)
    assert state is not None and state.root == str(moved.resolve())

    after = verify_bundle(moved)
    assert after.relocated is False
    assert after.ok is True, [issue.to_dict() for issue in after.errors]

    manifest = read_manifest(moved)
    entries = {entry.path: entry for entry in manifest.entries}
    config_entry = entries["config/config.toml"]
    assert config_entry.sha256 == sha256_file(str(moved / "config" / "config.toml"))

    second = relocate_bundle(moved, environment=environment)
    assert second.changed_files == 0


def test_relocate_noop_on_fresh_bundle(built_bundle: Path) -> None:
    environment = new_env(built_bundle)
    report = relocate_bundle(built_bundle, environment=environment)
    assert report.changed_files == 0
    assert report.old_roots == []
    assert load_root_state(built_bundle) is not None


def test_relocate_passes_rebuild_venv_flag(
    tmp_path: Path, source_tree_with_venv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from avdfleet.portable.relocate import rewriter

    destination = tmp_path / "bundle"
    PortableBundleBuilder(source_root=source_tree_with_venv, python_version="3.12.14").build(destination)
    environment = new_env(destination)
    captured: dict[str, object] = {}
    original = rewriter.repair_virtualenv

    def wrapper(*args: object, **kwargs: object):
        captured.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(rewriter, "repair_virtualenv", wrapper)
    relocate_bundle(
        destination,
        environment=environment,
        old_roots=["/stale/old/root"],
        rebuild_venv=True,
    )
    assert captured.get("rebuild") is True


def test_find_stale_roots(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    populate_runtime_dirs(root)
    write_marker(root)
    environment = new_env(root)
    old_root = str(tmp_path / "elsewhere")
    make_file(
        root / "config" / "config.toml",
        f'edition = "portable"\nportable_root = "{old_root}"\n',
    )
    stale = find_stale_roots(root, [old_root], environment=environment)
    assert stale == ["config/config.toml"]
    assert find_stale_roots(root, [str(tmp_path / "unrelated")], environment=environment) == []
