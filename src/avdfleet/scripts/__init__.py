"""Script subsystem: definitions, fan-out executor, built-in library, service.

Public surface::

    from avdfleet.scripts import ScriptService, CommandBuilder, builtin_scripts

``ScriptService`` implements :class:`avdfleet.core.interfaces.ScriptRunner`; it
takes its persistence, event bus, command builder and fan-out executor through
constructor injection so the API/CLI/daemon can wire real dependencies and the
unit tests can use in-memory stores and fake executables.
"""

from .builtins import (
    BUILTIN_GETPROP_ID,
    BUILTIN_IDS,
    BUILTIN_LIST_PACKAGES_ID,
    BUILTIN_OOBE_ACTIVITY_ID,
    BUILTIN_PING_TOKEN_ID,
    BUILTIN_SCREENSHOT_ID,
    BUILTIN_SCRIPTS,
    builtin_scripts,
    install_builtins,
)
from .executor import FanOutExecutor, SubprocessRunner, terminate_process
from .runners import CommandBuilder
from .service import ScriptService
from .types import (
    CommandResult,
    CommandRunner,
    InstancePlan,
    InstanceTarget,
    MemoryRunStore,
    MemoryScriptStore,
    OutputCallback,
    PlannedCommand,
    ScriptContext,
    ScriptPlanner,
)

__all__ = [
    "BUILTIN_GETPROP_ID",
    "BUILTIN_IDS",
    "BUILTIN_LIST_PACKAGES_ID",
    "BUILTIN_OOBE_ACTIVITY_ID",
    "BUILTIN_PING_TOKEN_ID",
    "BUILTIN_SCREENSHOT_ID",
    "BUILTIN_SCRIPTS",
    "CommandBuilder",
    "CommandResult",
    "CommandRunner",
    "FanOutExecutor",
    "InstancePlan",
    "InstanceTarget",
    "MemoryRunStore",
    "MemoryScriptStore",
    "OutputCallback",
    "PlannedCommand",
    "ScriptContext",
    "ScriptPlanner",
    "ScriptService",
    "SubprocessRunner",
    "builtin_scripts",
    "install_builtins",
    "terminate_process",
]
