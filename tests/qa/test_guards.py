"""A documented guard that never fires in production: LSPosed's API-level check.

``_step_install_lsposed`` refuses targets below API 27, but it reads
``TargetDevice.api_level`` and the production resolver in ``container.py``
hard-codes ``api_level=0``. The guard is therefore dead: on an API 25 device the
step pushes LSPosed and reports it installed, even though LSPosed aborts with
"Unsupported Android version" on that image.
"""

from __future__ import annotations

from pathlib import Path

from avdfleet.instrumentation import ProfileStep, StepAction
from tests.qa.qa_support import (
    API25_PROPS,
    LSPOSED_PROP,
    magisk_script,
    open_service,
    target,
    zip_bytes,
)


def test_install_lsposed_does_not_refuse_an_api_25_device_when_the_resolver_omits_api_level(
    tmp_path: Path,
) -> None:
    """The device says API 25; the step installs LSPosed anyway.

    ``container._resolve`` always builds ``TargetDevice(api_level=0)``, so the
    "requires Android API 27" branch is unreachable from the API/CLI/GUI. The
    step should use the probed ``ro.build.version.sdk`` (or refuse when the level
    is unknown) instead of trusting a field the resolver never fills.
    """
    adb = magisk_script()
    adb.responses["getprop"] = (0, API25_PROPS)
    adb.responses["/data/local/tmp/lsposed.zip"] = (0, "")
    adb.responses["magisk --install-module /data/local/tmp/lsposed.zip"] = (0, "")
    adb.responses["[ -d /data/adb/modules/zygisk_lsposed ] && echo yes"] = (0, "yes\n")
    with open_service(tmp_path, adb, target_device=target(api_level=0)) as service:
        record = service.import_upload(
            "lsposed.zip", zip_bytes({"module.prop": LSPOSED_PROP.encode("utf-8")})
        )
        step = ProfileStep(
            id="lsposed-install",
            action=StepAction.INSTALL_LSPOSED,
            module_id=record.id,
        )

        result = service._step_install_lsposed(step, target(api_level=0))

    assert result.ok is False, (
        "install_lsposed ran on an API 25 device because the resolver supplied "
        f"api_level=0 and the step never probed the real level (message: {result.message!r})"
    )
    assert "API 27" in result.message, (
        f"expected the documented refusal, got {result.message!r}"
    )
