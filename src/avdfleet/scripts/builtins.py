"""Built-in script library.

The library is intentionally small and stable: operators can rely on these ids
across daemon restarts, upgrades and the fleet spec.  Every entry is a regular
:class:`~avdfleet.core.models.ScriptDefinition` with ``builtin=True`` so the GUI
can show it as read-only-ish template material and the installer/daemon can
re-seed a store at any time through :func:`install_builtins`.
"""

from __future__ import annotations

from textwrap import dedent

from ..core.errors import NotFoundError
from ..core.models import ScriptDefinition, ScriptType
from .types import ScriptRepositoryLike

__all__ = ["BUILTIN_IDS", "BUILTIN_SCRIPTS", "builtin_scripts", "install_builtins"]

BUILTIN_GETPROP_ID = "builtin.getprop-summary"
BUILTIN_LIST_PACKAGES_ID = "builtin.list-packages"
BUILTIN_OOBE_ACTIVITY_ID = "builtin.oobe-activity"
BUILTIN_SCREENSHOT_ID = "builtin.screenshot-pull"
BUILTIN_PING_TOKEN_ID = "builtin.ping-proxy-token"

BUILTIN_IDS: tuple[str, ...] = (
    BUILTIN_GETPROP_ID,
    BUILTIN_LIST_PACKAGES_ID,
    BUILTIN_OOBE_ACTIVITY_ID,
    BUILTIN_SCREENSHOT_ID,
    BUILTIN_PING_TOKEN_ID,
)

_GETPROP_CONTENT = "getprop"

_SCREENSHOT_CONTENT = dedent(
    """
    import json
    import os
    import subprocess

    serial = os.environ["AVDFLEET_SERIAL"]
    raw_command = os.environ.get("AVDFLEET_ADB_COMMAND")
    adb = json.loads(raw_command) if raw_command else [os.environ.get("AVDFLEET_ADB", "adb")]
    output_dir = os.environ["AVDFLEET_OUTPUT_DIR"]
    target = os.path.join(output_dir, "screenshot-%s.png" % os.environ.get("AVDFLEET_INSTANCE", serial))
    data = subprocess.check_output(adb + ["-s", serial, "exec-out", "screencap", "-p"])
    with open(target, "wb") as handle:
        handle.write(data)
    print("screenshot: %s (%d bytes)" % (target, len(data)))
    """
).strip()

_PING_TOKEN_CONTENT = dedent(
    """
    import os
    import sys
    import urllib.error
    import urllib.request

    url = os.environ.get("AVDFLEET_TOKEN_URL") or os.environ.get("TOKEN_URL")
    if not url:
        print("AVDFLEET_TOKEN_URL is not set", file=sys.stderr)
        sys.exit(2)
    timeout = float(os.environ.get("AVDFLEET_TOKEN_TIMEOUT", "10"))
    request = urllib.request.Request(url, headers={"User-Agent": "avdfleet-script"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", "replace").strip()
            print("token-url %s -> %s %s" % (url, response.status, body))
    except (urllib.error.URLError, OSError) as exc:
        print("token-url %s failed: %s" % (url, exc), file=sys.stderr)
        sys.exit(1)
    """
).strip()

BUILTIN_SCRIPTS: tuple[ScriptDefinition, ...] = (
    ScriptDefinition(
        id=BUILTIN_GETPROP_ID,
        name="Getprop summary",
        type=ScriptType.ADB_SHELL,
        content=_GETPROP_CONTENT,
        description="Dump Android system properties (API level, release, model, serial, boot state).",
        timeout_s=30,
        builtin=True,
        tags=["device", "inventory"],
    ),
    ScriptDefinition(
        id=BUILTIN_LIST_PACKAGES_ID,
        name="List packages",
        type=ScriptType.ADB_SHELL,
        content="pm list packages",
        description="List installed Android packages on the instance.",
        timeout_s=60,
        builtin=True,
        tags=["device", "packages"],
    ),
    ScriptDefinition(
        id=BUILTIN_OOBE_ACTIVITY_ID,
        name="OOBE activity check",
        type=ScriptType.ADB_SHELL,
        content="dumpsys activity activities",
        description="Dump the activity stack so the operator (or OOBE checker) can find the setup wizard.",
        timeout_s=60,
        builtin=True,
        tags=["oobe", "diagnostics"],
    ),
    ScriptDefinition(
        id=BUILTIN_SCREENSHOT_ID,
        name="Pull screenshot",
        type=ScriptType.HOST_PYTHON,
        content=_SCREENSHOT_CONTENT,
        description=(
            "Capture a PNG with `adb exec-out screencap -p` and store it under the run artifacts "
            "directory; prints the file path."
        ),
        timeout_s=90,
        builtin=True,
        tags=["screen", "artifact"],
    ),
    ScriptDefinition(
        id=BUILTIN_PING_TOKEN_ID,
        name="Ping proxy token URL",
        type=ScriptType.HOST_PYTHON,
        content=_PING_TOKEN_CONTENT,
        description=(
            "HTTP GET the URL in AVDFLEET_TOKEN_URL (or TOKEN_URL) and print status plus body; "
            "used to distinguish per-instance proxy listeners during egress verification."
        ),
        timeout_s=30,
        builtin=True,
        tags=["proxy", "verify"],
    ),
)


def builtin_scripts() -> list[ScriptDefinition]:
    """Return fresh copies of every built-in definition."""
    return [script.model_copy(deep=True) for script in BUILTIN_SCRIPTS]


def install_builtins(
    script_store: ScriptRepositoryLike,
    *,
    overwrite: bool = False,
) -> list[ScriptDefinition]:
    """Seed *script_store* with the built-in library.

    Existing scripts with the same id are left untouched unless they are marked
    ``builtin`` and ``overwrite`` is true, so user edits to a built-in (for
    example a longer timeout) survive daemon restarts.
    """
    installed: list[ScriptDefinition] = []
    for script in builtin_scripts():
        existing: ScriptDefinition | None = None
        try:
            existing = script_store.get(script.id)
        except (NotFoundError, KeyError):
            existing = None
        if existing is None or overwrite and existing.builtin:
            installed.append(script_store.upsert(script))
        else:
            installed.append(existing)
    return installed
