"""Measure what a device can actually do, instead of assuming it.

Two distinctions drive this module and both were established by live measurement
on this project (see ``docs/rooted-images.md`` and ``docs/oobe-api25.md``):

``adb root`` != Magisk root
    A userdebug emulator image lets ``adb root`` restart adbd as uid 0. That is a
    *host* capability: it does not give applications root, it does not install a
    ``su`` manager, and it disappears on the next boot. Magisk root is a separate
    thing, detected by ``su -c id`` reporting ``context=u:r:magisk:s0``.

installed != enabled != active
    A Magisk module present under ``/data/adb/modules`` is installed. It is
    enabled unless the ``disable`` marker exists. It is only *active* after the
    framework has run it, which requires a reboot. The probe reports all three so
    the GUI cannot claim success for a file transfer.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any

from ..core.logging import get_logger
from ..core.models import utcnow
from .models import (
    CapabilityFlags,
    DeviceInstrumentation,
    InstalledModule,
    ModuleKind,
    RootImplementation,
)

logger = get_logger("avdfleet.instrumentation.probe")

#: Marker Magisk writes when a module is disabled.
MODULE_DISABLE_MARKER = "disable"
#: Marker Magisk writes when a module is queued for removal.
MODULE_REMOVE_MARKER = "remove"

_LSPOSED_MODULE_IDS = ("zygisk_lsposed", "lsposed", "zygisk-lsposed", "riru_lsposed")

#: LSPosed's own configuration database (module recognition, enable flag, scope).
LSPOSED_DB = "/data/adb/lspd/config/modules_config.db"


@dataclass
class CommandResult:
    """One adb invocation."""

    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def text(self) -> str:
        return (self.stdout or "").strip()


@dataclass
class MagiskModuleInfo:
    """Parsed ``/data/adb/modules/<id>`` entry."""

    identifier: str
    name: str = ""
    version: str = ""
    description: str = ""
    author: str = ""
    enabled: bool = True
    pending_removal: bool = False
    fields: dict[str, str] = field(default_factory=dict)


def parse_module_prop(text: str) -> dict[str, str]:
    """Parse a Magisk ``module.prop`` file into a mapping.

    Magisk's format is ``key=value`` per line with ``#`` comments. Malformed lines
    are skipped rather than raising: a partially written module.prop should show
    up as a partially known module, not as a crashed scan.
    """
    values: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        values[key] = value.strip()
    return values


def classify_magisk_module(
    module_id: str, fields: dict[str, str], *, has_zygisk: bool = False
) -> ModuleKind:
    """Classify a Magisk module.

    LSPosed ships as a Magisk module, so it is detected from its id/name before
    the generic Zygisk/standard split — the formats are not interchangeable.
    """
    haystack = f"{module_id} {fields.get('name', '')}".lower()
    if any(marker in haystack for marker in _LSPOSED_MODULE_IDS):
        return ModuleKind.LSPOSED_ZIP
    if has_zygisk or "zygisk" in haystack:
        return ModuleKind.ZYGISK_ZIP
    return ModuleKind.MAGISK_ZIP


class DeviceProber:
    """Runs the probes for one device serial.

    ``adb`` is injected so tests can drive a scripted command boundary; the
    production container passes the SDK ``adb`` path.
    """

    def __init__(
        self,
        adb_path: str | None = None,
        *,
        timeout_s: float = 30.0,
        allow_adb_root: bool = True,
        runner: Any | None = None,
    ) -> None:
        self._adb = adb_path or "adb"
        self._timeout_s = float(timeout_s)
        self._allow_adb_root = bool(allow_adb_root)
        self._runner = runner
        #: Serials whose adbd has already been restarted as uid 0.
        self._adbd_root: set[str] = set()

    # -- process boundary ------------------------------------------------
    def run(self, serial: str, *args: str, timeout_s: float | None = None) -> CommandResult:
        argv = [self._adb, "-s", serial, *args]
        if self._runner is not None:
            return self._runner(argv)
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv,
                capture_output=True,
                text=True,
                timeout=timeout_s or self._timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(argv=argv, returncode=1, stderr=str(exc))
        return CommandResult(
            argv=argv,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )

    def shell(self, serial: str, command: str, *, timeout_s: float | None = None) -> CommandResult:
        return self.run(serial, "shell", command, timeout_s=timeout_s)

    def forget_root(self, serial: str) -> None:
        """Drop the cached adbd-root state; a guest reboot resets it."""
        self._adbd_root.discard(serial)

    def root_shell(self, serial: str, command: str, *, magisk: bool) -> CommandResult:
        """Run *command* with whatever root path this device has.

        Magisk images use ``su -c``; plain userdebug images have no usable ``su``
        (the bundled one rejects ``-c``), so ``adb root`` is used and the command
        runs directly through the rooted adbd.
        """
        if magisk:
            result = self.shell(serial, f'su -c "{_escape(command)}"')
            # Magisk asks the user to grant shell su on a fresh device; until that
            # happens the request is rejected and a su-only implementation would
            # misreport a healthy environment as missing.
            lowered = (result.stdout or "") + (result.stderr or "")
            if result.ok and "rejected" not in lowered.lower():
                return result
        if serial not in self._adbd_root:
            self.run(serial, "root")
            self._adbd_root.add(serial)
        return self.shell(serial, command)

    # -- individual probes ----------------------------------------------
    def _magisk_payload_present(self, serial: str) -> bool:
        """Whether magiskd answers through adbd root, even if shell su is denied."""
        if not self._allow_adb_root:
            return False
        version = self.root_shell(serial, "magisk -V", magisk=True)
        if version.ok and version.text() and "not found" not in version.text().lower():
            return True
        pid = self.root_shell(serial, "pidof magiskd", magisk=True)
        return bool(pid.ok and pid.text())

    def props(self, serial: str) -> dict[str, str]:
        result = self.shell(serial, "getprop")
        props: dict[str, str] = {}
        if not result.ok:
            return props
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line.startswith("[") or "]" not in line:
                continue
            key, _, rest = line[1:].partition("]: [")
            value = rest[:-1] if rest.endswith("]") else rest
            props[key] = value
        return props

    def adb_root_available(self, props: dict[str, str]) -> bool:
        """Whether the image is a userdebug build that permits ``adb root``."""
        build_type = (props.get("ro.build.type", "") or "").lower()
        debuggable = (props.get("ro.debuggable", "") or "").strip()
        return build_type == "userdebug" or debuggable == "1"

    def magisk_state(self, serial: str) -> tuple[bool, str, bool, list[str]]:
        """Return ``(installed, version, environment_ready, notes)``."""
        notes: list[str] = []
        version_result = self.shell(serial, 'su -c "magisk -V"')
        version = version_result.text().splitlines()[-1].strip() if version_result.ok else ""
        if not version:
            verify = self.shell(serial, 'su -c "magisk -v"')
            if verify.ok and verify.text():
                version = verify.text().splitlines()[-1].strip()
        installed = bool(version)
        ready = False
        if installed:
            listing = self.shell(serial, 'su -c "ls /data/adb/magisk"')
            entries = [line for line in listing.stdout.splitlines() if line.strip()]
            ready = listing.ok and bool(entries)
            if not ready:
                notes.append(
                    "Magisk runs but /data/adb/magisk is empty: the Magisk app's 'Additional"
                    " Setup' has not completed, so magiskd logs '/data/adb is not present,"
                    " abort' and no module script runs. /data/adb/magisk existing means only"
                    " that setup is done -- not that Magisk is present."
                )
        else:
            notes.append("no Magisk su available to the shell user")
        return installed, version, ready, notes

    def zygisk_state(self, serial: str, *, magisk: bool) -> tuple[bool, bool, bool]:
        """Return ``(available, enabled, active)`` for Zygisk.

        ``enabled`` is the persisted Magisk setting; ``active`` additionally
        requires the module set to have been mounted, which only happens after a
        reboot. Without that distinction the GUI would report success one reboot
        too early.
        """
        if not magisk:
            return False, False, False
        result = self.root_shell(
            serial,
            "magisk --sqlite \"select value from settings where key='zygisk'\"",
            magisk=True,
        )
        enabled = False
        if result.ok:
            text = result.text().lower()
            enabled = "value=1" in text or text.endswith("=1") or "=1" in text
        if not enabled:
            return True, False, False
        # Some builds expose an explicit marker; when it is set, trust it. Neither
        # API 25 nor API 34 sets it, so the measured signals below are the norm.
        marker = self.root_shell(serial, "getprop ro.zygisk.active", magisk=True)
        if marker.ok and marker.text().strip() == "1":
            return True, True, True
        processes = self.root_shell(serial, "ps -A", magisk=True)
        listing = processes.stdout if processes.ok and processes.stdout else ""
        if not listing:
            fallback = self.root_shell(serial, "ps", magisk=True)
            listing = fallback.stdout
        active = "zygiskd" in listing
        if not active:
            base = self.root_shell(serial, "magisk --path", magisk=True)
            path = base.text().splitlines()[-1].strip() if base.ok and base.text() else ""
            if path:
                staged = self.root_shell(serial, f"ls {path}/.magisk/zygisk", magisk=True)
                active = bool(staged.ok and "loader" in staged.stdout)
        return True, enabled, active

    def lsposed_state(
        self,
        serial: str,
        *,
        magisk: bool,
        modules: list[InstalledModule] | None = None,
    ) -> tuple[bool, str, bool, list[InstalledModule]]:
        """Return ``(installed, version, active, inventory)`` for LSPosed.

        Passing *modules* lets the caller reuse a single ``/data/adb/modules``
        enumeration for both this check and the module inventory, which halves the
        adb round-trips (each module costs four commands).
        """
        if not magisk:
            return False, "", False, list(modules or [])
        inventory = modules if modules is not None else self.magisk_modules(serial)
        lsposed = next((m for m in inventory if m.kind is ModuleKind.LSPOSED_ZIP), None)
        if lsposed is None:
            return False, "", False, inventory
        running = self.root_shell(serial, "pidof lspd", magisk=True)
        active = bool(running.ok and running.text())
        if not active:
            evidence = self.root_shell(serial, "ls /data/adb/lspd", magisk=True)
            active = bool(evidence.ok and evidence.text())
        return True, lsposed.version, active, inventory

    def xposed_modules(
        self, serial: str, *, framework_active: bool = True
    ) -> tuple[list[InstalledModule], list[str]]:
        """Read LSPosed's module and scope tables.

        LSPosed's database is the authority for which Xposed APKs the framework
        recognises, whether each is enabled, and which applications it targets.
        There is no supported shell-only reader other than ``sqlite3``, so when it
        is missing the inventory is reported as unavailable instead of being guessed
        from ``pm list packages`` (an installed APK is not an active module).
        """
        notes: list[str] = []
        probe = self.root_shell(serial, "command -v sqlite3", magisk=True)
        if not probe.ok or not probe.text():
            notes.append(
                "Xposed module inventory unavailable: no sqlite3 on the device;"
                " inspect modules and scope in the LSPosed manager"
            )
            return [], notes

        listing = self.root_shell(
            serial,
            f"sqlite3 {LSPOSED_DB} 'select module_pkg_name, enabled from modules'",
            magisk=True,
        )
        if not listing.ok:
            notes.append(
                "could not read the LSPosed database: "
                + (listing.text() or listing.stderr[:200])
            )
            return [], notes

        inventory: list[InstalledModule] = []
        for line in listing.stdout.splitlines():
            package, _, enabled = line.strip().partition("|")
            if not package:
                continue
            inventory.append(
                InstalledModule(
                    identifier=package,
                    kind=ModuleKind.XPOSED_APK,
                    enabled=enabled.strip() == "1",
                    active=framework_active and enabled.strip() == "1",
                    source="lsposed",
                )
            )

        scopes: dict[str, list[str]] = {}
        scope_rows = self.root_shell(
            serial,
            f"sqlite3 {LSPOSED_DB} 'select m.module_pkg_name, s.app_pkg_name from modules m"
            " join scope s on s.mid = m.mid'",
            magisk=True,
        )
        if scope_rows.ok:
            for line in scope_rows.stdout.splitlines():
                package, _, app = line.strip().partition("|")
                if package and app:
                    scopes.setdefault(package, []).append(app)
        for module in inventory:
            module.scope = sorted(set(scopes.get(module.identifier, [])))
        return inventory, notes

    def magisk_modules(self, serial: str) -> list[InstalledModule]:
        """Enumerate ``/data/adb/modules`` with enable/disable state."""
        listing = self.root_shell(serial, "ls /data/adb/modules", magisk=True)
        if not listing.ok:
            return []
        modules: list[InstalledModule] = []
        for entry in listing.stdout.splitlines():
            name = entry.strip()
            if not name or name.startswith("/"):
                continue
            base = f"/data/adb/modules/{name}"
            prop = self.root_shell(serial, f"cat {base}/module.prop", magisk=True)
            fields = parse_module_prop(prop.stdout if prop.ok else "")
            disabled = self.root_shell(serial, f"[ -e {base}/{MODULE_DISABLE_MARKER} ] && echo yes", magisk=True)
            pending = self.root_shell(serial, f"[ -e {base}/{MODULE_REMOVE_MARKER} ] && echo yes", magisk=True)
            enabled = "yes" not in disabled.stdout.lower()
            has_zygisk = False
            contents = self.root_shell(serial, f"ls {base}/zygisk 2>/dev/null", magisk=True)
            if contents.ok and contents.text():
                has_zygisk = True
            modules.append(
                InstalledModule(
                    identifier=fields.get("id", name),
                    kind=classify_magisk_module(name, fields, has_zygisk=has_zygisk),
                    version=fields.get("version", ""),
                    enabled=enabled and "yes" not in pending.stdout.lower(),
                    active=enabled and "yes" not in pending.stdout.lower(),
                    source=f"{base}/module.prop",
                    notes=[] if enabled else [f"disabled via {MODULE_DISABLE_MARKER} marker"],
                )
            )
        return modules

    def writable_system(self, serial: str, *, magisk: bool) -> bool:
        """True when ``/system`` is already read-write (overlayfs or remounted)."""
        mounts = self.root_shell(serial, "mount", magisk=magisk)
        for line in mounts.stdout.splitlines():
            if " /system " not in line:
                continue
            if " rw" in f" {line.split(' type ')[0]}" or ",rw," in line or " rw," in line:
                return True
        return False

    # -- aggregate -------------------------------------------------------
    def probe(
        self,
        *,
        instance: str,
        serial: str,
        image_id: str = "",
        image_tag: str = "",
        running: bool = True,
        boot_completed: bool = True,
        api_level: int = 0,
        abi: str = "",
        declared_root: bool = False,
    ) -> DeviceInstrumentation:
        """Build a full :class:`DeviceInstrumentation` snapshot."""
        flags = CapabilityFlags()
        blockers: list[str] = []
        props: dict[str, str] = {}
        if running:
            props = self.props(serial)
            api_level = api_level or _int(props.get("ro.build.version.sdk", "0"))
            abi = abi or props.get("ro.product.cpu.abi", "")
        else:
            blockers.append("device is not running; start the instance to probe it")

        root = RootImplementation.NONE
        if running:
            root_probe = self.shell(serial, 'su -c id')
            probe_text = root_probe.text() if root_probe.ok else ""
            # Measured discriminator on both API 25 and API 34: Magisk's su returns
            # context=u:r:magisk:s0, while a plain userdebug su is u:r:su:s0. uid 0
            # alone would misreport a userdebug image as Magisk-rooted.
            if "uid=0" in probe_text and "u:r:magisk:s0" in probe_text:
                root = RootImplementation.MAGISK
                flags.magisk = True
            elif self.adb_root_available(props) or not props:
                if self._allow_adb_root:
                    root_result = self.run(serial, "root")
                    follow = self.shell(serial, "id") if root_result.ok else root_result
                    if follow.ok and "uid=0" in follow.text():
                        root = RootImplementation.ADB_ROOT
                else:
                    flags.notes.append("adb root is available but was not attempted (opt-in)")

        xposed_inventory: list[InstalledModule] = []
        if running and root is not RootImplementation.NONE:
            installed, version, ready, magisk_notes = self.magisk_state(serial) if root is RootImplementation.MAGISK else (False, "", False, [])
            if root is RootImplementation.MAGISK:
                flags.magisk = installed
                flags.magisk_version = version
                flags.magisk_environment_ready = ready
                flags.notes.extend(magisk_notes)
                available, enabled, active = self.zygisk_state(serial, magisk=installed)
                flags.zygisk_available = available
                flags.zygisk_enabled = enabled
                flags.zygisk_active = active
                inventory = self.magisk_modules(serial)
                lsposed_installed, lsposed_version, lsposed_active, inventory = (
                    self.lsposed_state(serial, magisk=installed, modules=inventory)
                )
                flags.lsposed_installed = lsposed_installed
                flags.lsposed_version = lsposed_version
                flags.lsposed_active = lsposed_active
                flags.reboot_required = installed and (enabled != active)
                if lsposed_installed:
                    xposed_inventory, xposed_notes = self.xposed_modules(
                        serial, framework_active=lsposed_active
                    )
                    flags.notes.extend(xposed_notes)
                magisk_modules = [
                    InstalledModule(
                        identifier=module.identifier,
                        kind=module.kind,
                        version=module.version,
                        enabled=module.enabled,
                        active=module.active,
                        source=module.source,
                        notes=module.notes,
                    )
                    for module in inventory
                ]
            else:
                magisk_modules = []
                flags.notes.append(
                    "adb root only: no application-facing su manager, no Zygisk, no LSPosed"
                )
            # ``adb_root`` means adbd itself is running as uid 0 -- not "some
            # root exists". A Magisk-rooted device does not imply this, and
            # conflating them is exactly the inaccurate status this feature
            # exists to prevent.
            bare_id = self.shell(serial, "id")
            flags.adb_root = bare_id.ok and "uid=0" in bare_id.text()
            flags.writable_system = self.writable_system(serial, magisk=flags.magisk)
        else:
            magisk_modules = []

        if running and root is RootImplementation.NONE:
            blockers.append("no root: install Magisk or use a userdebug image with adb root")
        if root is RootImplementation.ADB_ROOT:
            blockers.append("Magisk/Zygisk/LSPosed need Magisk root, not adb root")

        return DeviceInstrumentation(
            instance=instance,
            api_level=api_level,
            abi=abi,
            image_id=image_id,
            image_tag=image_tag,
            serial=serial,
            running=running,
            boot_completed=boot_completed,
            root_implementation=root,
            capabilities=flags,
            xposed_modules=xposed_inventory,
            magisk_modules=magisk_modules,
            compatible=root is not RootImplementation.NONE,
            blockers=blockers,
            probed_at=utcnow(),
        )


def _escape(value: str) -> str:
    """Escape a command for embedding inside ``su -c "..."``."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")


def _int(value: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


__all__ = [
    "CommandResult",
    "DeviceProber",
    "MagiskModuleInfo",
    "MODULE_DISABLE_MARKER",
    "MODULE_REMOVE_MARKER",
    "classify_magisk_module",
    "parse_module_prop",
]
