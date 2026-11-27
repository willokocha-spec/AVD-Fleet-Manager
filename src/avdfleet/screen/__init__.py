"""Screen streaming and input for AVD Fleet.

Public surface
--------------
:class:`ScreenService`
    Concrete ``avdfleet.core.interfaces.ScreenClient``: gRPC capture/streaming
    with ``adb exec-out screencap`` fallback, frame cache, and safe input
    injection.
:class:`GrpcEmulatorTransport` / :func:`discover_endpoints`
    Optional ``grpcio`` transport and the emulator gRPC discovery-file parser.
:class:`AdbScreencap` / :class:`FrameCache`
    The always-available adb capture path and its on-disk frame cache.
:class:`InputController`
    ``tap``/``swipe``/``text``/``key`` command builders with device-shell quoting.
:class:`ScrcpyLauncher`
    Optional local-display fallback (command builder + optional spawn).
"""

from .grpc_client import (
    DEFAULT_GRPC_PORT,
    GrpcEmulatorTransport,
    GrpcEndpoint,
    ScreenTransport,
    default_discovery_dirs,
    discover_endpoints,
    find_endpoint,
    grpc,
    parse_discovery_file,
)
from .input import (
    InputController,
    build_key_argv,
    build_swipe_argv,
    build_tap_argv,
    build_text_argv,
    normalize_keycode,
    quote_device_argument,
)
from .scrcpy import ScrcpyConfig, ScrcpyLauncher, build_scrcpy_command, find_scrcpy
from .screencap import (
    AdbScreencap,
    FrameCache,
    SubprocessRunner,
    adb_capture_argv,
    has_png_ihdr,
    is_png,
    repair_shell_png,
    validate_png,
)
from .service import DEFAULT_FPS, ScreenService

__all__ = [
    "DEFAULT_FPS",
    "DEFAULT_GRPC_PORT",
    "AdbScreencap",
    "FrameCache",
    "GrpcEmulatorTransport",
    "GrpcEndpoint",
    "InputController",
    "ScreenService",
    "ScreenTransport",
    "ScrcpyConfig",
    "ScrcpyLauncher",
    "SubprocessRunner",
    "adb_capture_argv",
    "build_key_argv",
    "build_scrcpy_command",
    "build_swipe_argv",
    "build_tap_argv",
    "build_text_argv",
    "default_discovery_dirs",
    "discover_endpoints",
    "find_endpoint",
    "find_scrcpy",
    "grpc",
    "has_png_ihdr",
    "is_png",
    "normalize_keycode",
    "parse_discovery_file",
    "quote_device_argument",
    "repair_shell_png",
    "validate_png",
]
