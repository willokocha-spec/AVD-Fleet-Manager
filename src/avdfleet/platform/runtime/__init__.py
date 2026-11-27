"""Runtime domain: launch, boot detection, supervision, capacity and snapshots.

Public entry points:

* :class:`RuntimeManagerService` -- concrete ``core.interfaces.RuntimeManager``.
* :class:`~avdfleet.platform.runtime.boot.AdbRunner` /
  :class:`~avdfleet.platform.runtime.boot.SubprocessAdbRunner` -- the adb
  abstraction used for boot detection and snapshot/adb fallbacks.
* :class:`~avdfleet.platform.runtime.capacity.CapacityService` -- RAM/CPU/disk/
  port capacity accounting (never overcommits instance RAM).
* :class:`~avdfleet.platform.runtime.supervisor.ProcessSupervisor` -- background
  watcher for externally-exited emulator processes, with restart policy.
* :class:`~avdfleet.platform.runtime.snapshots.SnapshotService` -- emulator
  console/``adb emu`` snapshot save/load/delete/list with golden metadata.
* :func:`~avdfleet.platform.runtime.launch.build_emulator_argv` -- credential-free
  emulator command construction.
"""

from .boot import (
    AdbDevice,
    AdbError,
    AdbResult,
    AdbRunner,
    BackendAdbRunner,
    BootDetector,
    BootStatus,
    SubprocessAdbRunner,
    boot_serial,
    parse_devices,
)
from .capacity import CapacityDecision, CapacityService
from .launch import (
    LaunchSpec,
    ProcessFactory,
    ProcessHandle,
    SubprocessProcessFactory,
    build_emulator_argv,
    build_launch_spec,
    launch_log_path,
    resolve_gpu_mode,
    screen_endpoint,
    validate_proxy_upstream,
)
from .manager import (
    RunningInstance,
    RuntimeManagerService,
    RuntimeStateStore,
)
from .snapshots import (
    GOLDEN_SNAPSHOT,
    ConsoleFactory,
    EmulatorConsoleLike,
    SnapshotService,
    SnapshotStore,
    SnapshotTarget,
    default_console_factory,
    validate_snapshot_name,
)
from .supervisor import ExitRecord, ProcessSupervisor, RestartPolicy, SupervisedProcess

__all__ = [
    "AdbDevice",
    "AdbError",
    "AdbResult",
    "AdbRunner",
    "BackendAdbRunner",
    "BootDetector",
    "BootStatus",
    "CapacityDecision",
    "CapacityService",
    "ConsoleFactory",
    "EmulatorConsoleLike",
    "ExitRecord",
    "GOLDEN_SNAPSHOT",
    "LaunchSpec",
    "ProcessFactory",
    "ProcessHandle",
    "ProcessSupervisor",
    "RestartPolicy",
    "RunningInstance",
    "RuntimeManagerService",
    "RuntimeStateStore",
    "SnapshotService",
    "SnapshotStore",
    "SnapshotTarget",
    "SubprocessAdbRunner",
    "SubprocessProcessFactory",
    "SupervisedProcess",
    "boot_serial",
    "build_emulator_argv",
    "build_launch_spec",
    "default_console_factory",
    "launch_log_path",
    "parse_devices",
    "resolve_gpu_mode",
    "screen_endpoint",
    "validate_proxy_upstream",
    "validate_snapshot_name",
]
