"""AVD domain: templates, instance definitions, typed ``config.ini`` and YAML I/O.

Public entry points:

* :class:`AvdManagerService` -- concrete ``core.interfaces.AvdManager``.
* :class:`TemplateService` / :class:`TemplateStore` -- template validation and
  persistence (store repository or JSON fallback).
* :mod:`avdfleet.platform.avd.configini` -- parse/serialise/validate
  ``config.ini`` while preserving comments and unknown keys.
* :mod:`avdfleet.platform.avd.yamlio` -- credential-free YAML export/import.
"""

from .configini import (
    ConfigFileError,
    ConfigIni,
    ConfigIniError,
    ConfigIniIssue,
    ConfigValidationError,
    apply_config_overrides,
    avd_dir_for,
    config_path_for,
    detect_config_issues,
    ini_path_for,
    load_values,
    read_config,
    render_template_config,
    validate_config_values,
)
from .manager import AvdManagerService
from .templates import DEFAULT_DEVICE_PROFILES, TemplateService, TemplateStore
from .yamlio import (
    build_export,
    dump_export,
    export_avd_yaml,
    load_avd_yaml,
    parse_avd_yaml,
    sanitise_config,
    sanitise_instance,
)

__all__ = [
    "DEFAULT_DEVICE_PROFILES",
    "AvdManagerService",
    "ConfigFileError",
    "ConfigIni",
    "ConfigIniError",
    "ConfigIniIssue",
    "ConfigValidationError",
    "TemplateService",
    "TemplateStore",
    "apply_config_overrides",
    "avd_dir_for",
    "build_export",
    "config_path_for",
    "detect_config_issues",
    "dump_export",
    "export_avd_yaml",
    "ini_path_for",
    "load_avd_yaml",
    "load_values",
    "parse_avd_yaml",
    "read_config",
    "render_template_config",
    "sanitise_config",
    "sanitise_instance",
    "validate_config_values",
]
