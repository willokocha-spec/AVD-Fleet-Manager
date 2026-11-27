"""Real-emulator acceptance entry point (skipped on unaccelerated hosts).

This is the only test that exercises real AVDs. It is intentionally the same
path a user runs manually: ``avdfleet selftest --e2e``. It is skipped unless a
usable WHPX/KVM/HVF accelerator and the Android SDK are present.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.emulator


def _acceleration_available() -> bool:
    try:
        from avdfleet.platform import PlatformService

        host = PlatformService().detect(refresh=True)
        return bool(host.acceleration_available)
    except Exception:  # noqa: BLE001 - probe failures mean skip, not failure
        return False


@pytest.mark.skipif(
    shutil.which("adb") is None or not _acceleration_available(),
    reason="requires real hardware acceleration (WHPX/KVM/HVF) and an installed Android SDK",
)
def test_selftest_e2e_on_real_emulator(tmp_path) -> None:
    env = dict(os.environ)
    env.setdefault("AVDFLEET_DATA_DIR", str(tmp_path / "data"))
    env.setdefault("AVDFLEET_CONFIG_DIR", str(tmp_path / "config"))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "avdfleet",
            "selftest",
            "--e2e",
            "--fast",
            "--count",
            "1",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=1800,
        env=env,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert '"ok": true' in result.stdout
    assert '"evidence": "real"' in result.stdout or '"evidence": "real-emulator"' in result.stdout
