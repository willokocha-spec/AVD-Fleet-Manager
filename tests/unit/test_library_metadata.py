"""Xposed APKs marked only by manifest meta-data (audit item C3).

The library docstring and ``docs/instrumentation-compatibility.md`` say a
``<meta-data android:name="xposedmodule">`` declaration in the APK manifest is
enough to classify the APK as an Xposed module, with no ``assets/xposed_init``
and no ``META-INF/xposed/`` entries. ``docs/instrumentation-status.md`` recorded
the opposite as a defect: such an APK imported as ``unknown``.

The manifest inside a real APK is compiled binary XML, so the practical reader
is ``aapt2 dump xmltree``. A unit test needs no SDK: the ``aapt2`` process
boundary is stubbed with the stdout it would produce. Both directions are
asserted - the marker classifies, its absence stays ``unknown`` - so this file
cannot pass merely because every APK is called an Xposed module.
"""

from __future__ import annotations

import io
import subprocess
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from avdfleet.instrumentation import (
    DeviceProber,
    InstrumentationRepository,
    InstrumentationService,
    ModuleKind,
    ModuleLibrary,
    TargetDevice,
)
from avdfleet.instrumentation.capabilities import CommandResult
from avdfleet.store import Store

#: Inert stand-in for a compiled AndroidManifest.xml. The marker name lives in
#: the AXML string pool as UTF-16LE, exactly where the name would really appear.
MANIFEST_BYTES = (
    b"\x03\x00\x08\x00"
    + b"\x00" * 16
    + "xposedmodule".encode("utf-16-le")
    + b"\x00\x00"
    + "com.example.metamodule".encode("utf-16-le")
    + b"\x00\x00"
)

XMLTREE_WITH_META = """\
N: android=http://schemas.android.com/apk/res/android
  E: manifest (line=2)
    A: package="com.example.metamodule" (Raw: "com.example.metamodule")
    E: application (line=5)
      E: meta-data (line=6)
        A: android:name(0x01010003)="xposedmodule" (Raw: "xposedmodule")
        A: android:value(0x01010024)="true" (Raw: "true")
"""

XMLTREE_WITHOUT_META = """\
N: android=http://schemas.android.com/apk/res/android
  E: manifest (line=2)
    A: package="com.example.plain" (Raw: "com.example.plain")
    E: application (line=5)
      E: activity (line=6)
        A: android:name(0x01010003)="com.example.plain.Main" (Raw: "com.example.plain.Main")
"""

BADGING_WITH_META = """\
package: name='com.example.metamodule' versionCode='7' versionName='1.2.3'
sdkVersion:'24'
application-label:'Meta Module'
"""


def _apk_bytes(manifest: bytes = MANIFEST_BYTES) -> bytes:
    """A minimal APK-like ZIP: a manifest plus a dex payload."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AndroidManifest.xml", manifest)
        archive.writestr("classes.dex", b"dex\n035\x00")
    return buffer.getvalue()


class AaptStub:
    """Stands in for the ``aapt2`` executable at the ``subprocess`` boundary."""

    def __init__(self, xmltree: str, badging: str = BADGING_WITH_META) -> None:
        self._xmltree = xmltree
        self._badging = badging
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if "xmltree" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=self._xmltree, stderr="")
        if "badging" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=self._badging, stderr="")
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="unexpected aapt2 operation"
        )


@pytest.fixture()
def store() -> Iterator[Store]:
    store = Store(":memory:")
    store.initialize()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture()
def library(tmp_path: Path) -> ModuleLibrary:
    # A non-empty aapt path is what makes the manifest inspectable; the binary
    # itself is replaced by AaptStub below, so no SDK is needed.
    return ModuleLibrary(tmp_path / "modules", aapt_path="aapt2-stub")


@pytest.fixture()
def service(store: Store, library: ModuleLibrary) -> InstrumentationService:
    def runner(argv: list[str]) -> CommandResult:
        return CommandResult(argv=argv, returncode=1)

    return InstrumentationService(
        InstrumentationRepository(store.database),
        library,
        DeviceProber(runner=runner),
        lambda serial: TargetDevice(
            instance=serial, serial=serial, running=True, boot_completed=True
        ),
    )


def test_import_upload_classifies_a_meta_data_only_apk_as_xposed(
    service: InstrumentationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded defect: this imported as ``unknown`` despite the marker."""
    stub = AaptStub(XMLTREE_WITH_META)
    monkeypatch.setattr(subprocess, "run", stub)

    data = _apk_bytes()
    record = service.import_upload("meta-only.apk", data)

    assert record.kind is ModuleKind.XPOSED_APK
    assert record.metadata["xposed_meta_data"] is True
    # There is no archive-level marker; the manifest is the only evidence.
    assert record.metadata["xposed_markers"] == []
    assert record.package_name == "com.example.metamodule"
    assert record.version == "1.2.3"
    # The manifest was actually consulted, and the bytes were stored.
    assert any("xmltree" in call for call in stub.calls)
    assert Path(record.file_path).read_bytes() == data


def test_import_upload_leaves_an_apk_without_the_marker_unknown(
    service: InstrumentationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control: a manifest without the marker is not a module."""
    stub = AaptStub(XMLTREE_WITHOUT_META)
    monkeypatch.setattr(subprocess, "run", stub)

    record = service.import_upload("plain.apk", _apk_bytes(b"<manifest/>"))

    assert record.kind is ModuleKind.UNKNOWN
    assert record.metadata["xposed_markers"] == []
    assert "note" in record.metadata
    assert "xposed" in record.metadata["note"].lower()
