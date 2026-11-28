"""Item 3: emulator logs must be owner-only (POSIX).

The emulator writes its per-instance http_proxy listener token into this file, so a
0644 log in a 0755 directory exposes the token to every local user.
"""

from __future__ import annotations

import os
import stat

import pytest

from avdfleet.platform.runtime.launch import open_emulator_log

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX file modes; Windows uses inherited ACLs"
)


def test_emulator_log_is_owner_only(tmp_path) -> None:
    log_dir = tmp_path / "Logs"
    log_path = log_dir / "avd-emulator.log"
    old_umask = os.umask(0o022)
    try:
        with open_emulator_log(log_path) as handle:
            handle.write(b"http_proxy=http://avd:token@127.0.0.1:7800\n")
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600, oct(log_path.stat().st_mode)
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700, oct(log_dir.stat().st_mode)


def test_reopening_keeps_the_restrictive_mode(tmp_path) -> None:
    log_dir = tmp_path / "Logs"
    log_path = log_dir / "avd-emulator.log"
    with open_emulator_log(log_path):
        pass
    # A pre-existing world-readable file must be tightened, not merely appended to.
    os.chmod(log_path, 0o644)
    with open_emulator_log(log_path):
        pass
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


def test_log_content_is_appended_not_truncated(tmp_path) -> None:
    log_path = tmp_path / "Logs" / "avd-emulator.log"
    with open_emulator_log(log_path) as handle:
        handle.write(b"first\n")
    with open_emulator_log(log_path) as handle:
        handle.write(b"second\n")
    assert log_path.read_bytes() == b"first\nsecond\n"
