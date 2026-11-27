"""Unit tests for typed AVD ``config.ini`` parsing, validation and rendering.

Hermetic: every test uses ``tmp_path`` and an in-memory :class:`Template`; no SDK
or emulator is required (contracts/fakes-spec.md §7).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from avdfleet.core.errors import InstanceError, ValidationError
from avdfleet.core.models import GpuMode, ImageKind, SystemImage, Template
from avdfleet.platform.avd.configini import (
    ConfigFileError,
    ConfigIni,
    ConfigValidationError,
    apply_config_overrides,
    detect_config_issues,
    image_sysdir,
    load_values,
    read_config,
    render_template_config,
    validate_config_values,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CANONICAL = """\
# AVD configuration generated for testing
avd.ini.encoding=UTF-8
hw.device.name=pixel_7
hw.ramSize=2048
vm.heapSize=256
hw.cpu.ncore=2
hw.gpu.mode=swiftshader_indirect
image.sysdir.1=system-images/android-35/google_apis_playstore/x86_64/
tag.id=google_apis_playstore
abi.type=x86_64
PlayStore.enabled=true
# a trailing comment
disk.dataPartition.size=6G
unknown.custom.key=keep-me
"""


def write_config(tmp_path: Path, text: str = CANONICAL) -> Path:
    path = tmp_path / "avd-01.avd" / "config.ini"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_image(**overrides: object) -> SystemImage:
    data: dict[str, object] = {
        "id": "system-images;android-35;google_apis_playstore;x86_64",
        "api_level": 35,
        "tag": "google_apis_playstore",
        "abi": "x86_64",
        "kind": ImageKind.GOOGLE_APIS_PLAYSTORE,
        "display_name": "Android API 35 Google Play (x86_64)",
        "installed": True,
    }
    data.update(overrides)
    return SystemImage(**data)


def make_template(**overrides: object) -> Template:
    data: dict[str, object] = {
        "id": "pixel-play-35",
        "name": "Pixel Play 35",
        "image_id": "system-images;android-35;google_apis_playstore;x86_64",
        "device_id": "pixel_7",
        "ram_mb": 2048,
        "heap_mb": 256,
        "cpu_cores": 2,
        "gpu_mode": GpuMode.SWIFTSHADER_INDIRECT,
        "resolution": "1080x2400",
        "density": 420,
        "config": {"custom.flag": "1"},
    }
    data.update(overrides)
    return Template(**data)


# ---------------------------------------------------------------------------
# Parsing / serialisation
# ---------------------------------------------------------------------------


def test_parse_preserves_comments_and_unknown_keys(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    document = read_config(path)
    assert document.get("hw.ramSize") == "2048"
    assert document.get("unknown.custom.key") == "keep-me"
    assert document.comments()[0].startswith("# AVD configuration")
    assert "# a trailing comment" in document.comments()
    assert document.keys().index("unknown.custom.key") > document.keys().index("PlayStore.enabled")


def test_canonical_file_roundtrips_byte_for_byte(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    document = read_config(path)
    assert document.render() == CANONICAL


def test_quoted_values_roundtrip() -> None:
    document = ConfigIni()
    document.set("tag.display", "  Google Play  ")
    document.set("avd.ini.displayname", 'Pixel "7" \\ test')
    text = document.render()
    assert '"' in text  # values that need quoting are quoted
    parsed = ConfigIni.parse(text)
    assert parsed.get("tag.display") == "  Google Play  "
    assert parsed.get("avd.ini.displayname") == 'Pixel "7" \\ test'


def test_newline_in_value_is_rejected() -> None:
    with pytest.raises(ConfigValidationError):
        validate_config_values({"tag.display": "line1\nline2"})
    with pytest.raises(ConfigValidationError):
        validate_config_values({"tag.display": "line1\rline2"})


def test_missing_file_strict_vs_tolerant(tmp_path: Path) -> None:
    path = tmp_path / "missing.avd" / "config.ini"
    assert load_values(path) == {}
    with pytest.raises(ConfigFileError):
        read_config(path)


def test_detect_missing_file(tmp_path: Path) -> None:
    issues = detect_config_issues(tmp_path / "missing.avd" / "config.ini")
    assert issues and issues[0].is_error
    assert "missing" in issues[0].message


def test_detect_malformed_and_duplicate_lines(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        "\n".join(
            [
                "hw.ramSize=2048",
                "this is not a config line",
                "hw.ramSize=4096",
                "hw.cpu.ncore=99",
            ]
        )
        + "\n",
    )
    issues = detect_config_issues(path)
    messages = [issue.as_text() for issue in issues]
    assert any("malformed line" in message for message in messages)
    assert any("duplicate key" in message for message in messages)
    assert any("hw.cpu.ncore" in message for message in messages)


def test_detect_missing_required_keys(tmp_path: Path) -> None:
    path = write_config(tmp_path, "hw.ramSize=2048\n")
    issues = detect_config_issues(path, required_keys=("image.sysdir.1",))
    assert any(issue.key == "image.sysdir.1" for issue in issues)


# ---------------------------------------------------------------------------
# Typed validation
# ---------------------------------------------------------------------------


def test_ram_bounds() -> None:
    assert validate_config_values({"hw.ramSize": "2048"}) == {"hw.ramSize": "2048"}
    with pytest.raises(ConfigValidationError):
        validate_config_values({"hw.ramSize": "128"})
    with pytest.raises(ConfigValidationError):
        validate_config_values({"hw.ramSize": "999999"})
    with pytest.raises(ConfigValidationError):
        validate_config_values({"hw.ramSize": "2G"})


def test_cpu_and_heap_bounds() -> None:
    assert validate_config_values({"hw.cpu.ncore": "8", "vm.heapSize": "512"}) == {
        "hw.cpu.ncore": "8",
        "vm.heapSize": "512",
    }
    with pytest.raises(ConfigValidationError):
        validate_config_values({"hw.cpu.ncore": "0"})
    with pytest.raises(ConfigValidationError):
        validate_config_values({"vm.heapSize": "99999"})


def test_gpu_mode_enum() -> None:
    assert validate_config_values({"hw.gpu.mode": "HOST"}) == {"hw.gpu.mode": "host"}
    with pytest.raises(ConfigValidationError):
        validate_config_values({"hw.gpu.mode": "vulkan"})


def test_abi_normalisation() -> None:
    assert validate_config_values({"abi.type": "ARM64"}) == {"abi.type": "arm64-v8a"}
    assert validate_config_values({"abi.type": "x86_64"}) == {"abi.type": "x86_64"}
    with pytest.raises(ConfigValidationError):
        validate_config_values({"abi.type": "mips"})


def test_bool_normalisation() -> None:
    assert validate_config_values({"PlayStore.enabled": "YES"}) == {"PlayStore.enabled": "yes"}
    assert validate_config_values({"PlayStore.enabled": "1"}) == {"PlayStore.enabled": "true"}
    assert validate_config_values({"PlayStore.enabled": "off"}) == {"PlayStore.enabled": "false"}
    with pytest.raises(ConfigValidationError):
        validate_config_values({"PlayStore.enabled": "maybe"})


def test_data_partition_size() -> None:
    assert validate_config_values({"disk.dataPartition.size": "6g"}) == {
        "disk.dataPartition.size": "6G"
    }
    assert validate_config_values({"disk.dataPartition.size": "2048M"}) == {
        "disk.dataPartition.size": "2048M"
    }
    with pytest.raises(ConfigValidationError):
        validate_config_values({"disk.dataPartition.size": "huge"})


def test_secret_key_rejected() -> None:
    with pytest.raises(ConfigValidationError):
        validate_config_values({"proxy.password": "hunter2"})
    with pytest.raises(ConfigValidationError):
        validate_config_values({"api_token": "abc"})


def test_bad_keys_rejected() -> None:
    with pytest.raises(ConfigValidationError):
        validate_config_values({"": "value"})
    with pytest.raises(ConfigValidationError):
        validate_config_values({"bad key": "value"})


def test_collects_all_errors() -> None:
    with pytest.raises(ConfigValidationError) as info:
        validate_config_values({"hw.ramSize": "1", "hw.cpu.ncore": "x"})
    assert len(info.value.errors) == 2


# ---------------------------------------------------------------------------
# Editing / atomic writes
# ---------------------------------------------------------------------------


def test_apply_overrides_preserves_comments_and_backs_up(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    document = apply_config_overrides(
        path,
        {"hw.ramSize": "4096", "hw.cpu.ncore": "4", "new.key": "value"},
    )
    assert document.get("hw.ramSize") == "4096"
    backup = path.with_name("config.ini.bak")
    assert backup.exists()
    assert "hw.ramSize=2048" in backup.read_text(encoding="utf-8")
    updated = path.read_text(encoding="utf-8")
    assert "# AVD configuration generated for testing" in updated
    assert "unknown.custom.key=keep-me" in updated
    assert "new.key=value" in updated


def test_apply_overrides_dry_run_does_not_write(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    before = path.read_text(encoding="utf-8")
    document = apply_config_overrides(path, {"hw.ramSize": "4096"}, dry_run=True)
    assert document.get("hw.ramSize") == "4096"
    assert path.read_text(encoding="utf-8") == before
    assert not path.with_name("config.ini.bak").exists()


def test_apply_overrides_missing_file_requires_opt_in(tmp_path: Path) -> None:
    path = tmp_path / "missing.avd" / "config.ini"
    with pytest.raises(ConfigFileError):
        apply_config_overrides(path, {"hw.ramSize": "2048"})
    document = apply_config_overrides(
        path, {"hw.ramSize": "2048"}, create_if_missing=True, backup=False
    )
    assert document.get("hw.ramSize") == "2048"
    assert path.exists()


def test_apply_overrides_rejects_invalid_before_writing(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        apply_config_overrides(path, {"hw.ramSize": "12"})
    assert path.read_text(encoding="utf-8") == before
    assert not path.with_name("config.ini.bak").exists()


def test_deduplicate_keeps_first_position() -> None:
    document = ConfigIni.parse("# head\nhw.ramSize=1\nother=2\nhw.ramSize=3\n")
    document.set("hw.ramSize", "2048")
    document.deduplicate()
    assert document.render() == "# head\nhw.ramSize=2048\nother=2\n"


def test_remove_and_contains() -> None:
    document = ConfigIni.parse("a=1\nb=2\n")
    assert "a" in document
    assert document.remove("a")
    assert "a" not in document
    assert not document.remove("a")


# ---------------------------------------------------------------------------
# Image / template helpers
# ---------------------------------------------------------------------------


def test_image_sysdir_from_package_and_custom_path(tmp_path: Path) -> None:
    assert (
        image_sysdir("system-images;android-35;google_apis_playstore;x86_64")
        == "system-images/android-35/google_apis_playstore/x86_64/"
    )
    custom = tmp_path / "custom-image"
    assert image_sysdir("custom-image-1", local_path=str(custom)) == str(custom).replace(
        "\\", "/"
    )


def test_render_template_config_typed_values() -> None:
    values = render_template_config(make_template(), image=make_image())
    assert values["hw.ramSize"] == "2048"
    assert values["vm.heapSize"] == "256"
    assert values["hw.cpu.ncore"] == "2"
    assert values["hw.device.name"] == "pixel_7"
    assert values["tag.id"] == "google_apis_playstore"
    assert values["abi.type"] == "x86_64"
    assert values["PlayStore.enabled"] == "true"
    assert values["hw.gpu.mode"] == "swiftshader_indirect"
    assert values["hw.lcd.width"] == "1080"
    assert values["hw.lcd.height"] == "2400"
    assert values["hw.lcd.density"] == "420"
    assert values["custom.flag"] == "1"
    assert values["image.sysdir.1"].startswith("system-images/android-35/")


def test_render_template_config_rejects_out_of_bounds_ram() -> None:
    template = make_template(ram_mb=128)
    # Template itself is a plain model; rendering applies the typed validation.
    with pytest.raises(InstanceError.__mro__[1]):  # ValidationError subclass
        render_template_config(template, image=make_image())


def test_render_template_config_extra_overrides() -> None:
    values = render_template_config(
        make_template(), image=make_image(), extra={"hw.ramSize": "4096"}
    )
    assert values["hw.ramSize"] == "4096"


def test_validation_error_has_details() -> None:
    with pytest.raises(ValidationError) as info:
        validate_config_values({"hw.ramSize": "-1"})
    assert info.value.details["errors"]
