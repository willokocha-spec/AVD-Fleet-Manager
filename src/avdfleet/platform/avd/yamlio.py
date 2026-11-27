"""YAML export/import of AVD templates and instance definitions.

The YAML document is the portable, human-editable form of the fleet definition
used by ``avdfleet export`` / ``import`` and by the fleet-spec reconciler.  It is
schema-checked with the core models (:class:`AvdTemplateExport`), and it never
contains secrets: proxy passwords, vault references, tokens and API keys are
removed on export and rejected on import.

Format (``version: "1"``)::

    version: "1"
    templates:
      - id: pixel-7-play-35
        name: Pixel 7 Play 35
        image_id: system-images;android-35;google_apis_playstore;x86_64
        device_id: pixel_7
        ram_mb: 2048
        ...
    instances:
      - name: avd-01
        template_id: pixel-7-play-35
        proxy_id: proxy-01
        config: {}

Everything is validated before anything is persisted; the importer returns the
parsed model and the AVD manager decides how to apply it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

import yaml

from avdfleet.core.errors import ValidationError
from avdfleet.core.logging import redact
from avdfleet.core.models import AvdInfo, AvdTemplateExport, Template, utcnow

from .configini import ConfigValidationError, is_secret_key, validate_config_values

FORMAT_VERSION = "1"
SUPPORTED_VERSIONS = {"1", 1}
EXPORT_HEADER = (
    "# AVD Fleet definition export (version {version}) generated {generated_at}.\n"
    "# Contains no credentials: proxy passwords live in the encrypted vault.\n"
)

_INSTANCE_KEYS = frozenset(
    {
        "name",
        "template_id",
        "image_id",
        "device_id",
        "proxy_id",
        "config",
        "overrides",
        "tags",
        "description",
    }
)
_EXPORTED_INSTANCE_FIELDS = (
    "name",
    "template_id",
    "image_id",
    "device_id",
    "proxy_id",
    "config",
)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:password|passwd|token|secret|api[_-]?key|credential)\s*[=:]"
)


# ---------------------------------------------------------------------------
# Sanitising
# ---------------------------------------------------------------------------


def _sanitise_value(value: Any) -> Any:
    """Recursively strip credential-looking keys and redact credential strings."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if is_secret_key(text_key):
                continue
            result[text_key] = _sanitise_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitise_value(item) for item in value]
    if isinstance(value, str):
        cleaned = str(redact(value))
        if _SECRET_VALUE_RE.search(cleaned):
            return "***"
        return cleaned
    return value


def sanitise_config(config: Mapping[str, Any] | None) -> dict[str, str]:
    """Return a credential-free ``config.ini`` mapping suitable for YAML."""
    sanitised = _sanitise_value(dict(config or {}))
    return {str(key): str(value) for key, value in sanitised.items()}


def sanitise_instance(instance: AvdInfo | Mapping[str, Any]) -> dict[str, Any]:
    """Return the importable definition fields of an instance, without secrets."""
    if isinstance(instance, AvdInfo):
        data: dict[str, Any] = {
            "name": instance.name,
            "template_id": instance.template_id,
            "image_id": instance.image_id,
            "device_id": instance.device_id,
            "proxy_id": instance.proxy_id,
            "config": sanitise_config(instance.config),
        }
    elif isinstance(instance, Mapping):
        data = {
            key: _sanitise_value(instance[key])
            for key in _EXPORTED_INSTANCE_FIELDS
            if instance.get(key) not in (None, "", {})
        }
        if "config" in data and not isinstance(data["config"], dict):
            data["config"] = sanitise_config({})
        elif "config" in data:
            data["config"] = sanitise_config(data["config"])
    else:  # pragma: no cover - defensive
        raise ValidationError("instance export expects AvdInfo or a mapping")
    return {
        key: value
        for key, value in data.items()
        if value not in (None, "", {}) or key == "name"
    }


def sanitise_template(template: Template) -> Template:
    """Return a copy of *template* with credential-free config/scripts/tags."""
    return template.model_copy(
        update={
            "config": sanitise_config(template.config),
            "scripts": [
                str(redact(str(item)))
                for item in template.scripts
                if not _SECRET_VALUE_RE.search(str(item))
            ],
            "tags": [str(item) for item in template.tags],
        }
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def build_export(
    templates: Iterable[Template],
    instances: Iterable[AvdInfo | Mapping[str, Any]] = (),
) -> AvdTemplateExport:
    """Assemble a clean :class:`AvdTemplateExport` from templates and instances."""
    clean_templates = sorted(
        (sanitise_template(template) for template in templates),
        key=lambda item: (item.name.lower(), item.id),
    )
    clean_instances = [
        sanitise_instance(instance)
        for instance in sorted(instances, key=lambda item: str(
            getattr(item, "name", None) or (item.get("name") if isinstance(item, Mapping) else "")
        ))
    ]
    return AvdTemplateExport(
        version=FORMAT_VERSION,
        templates=clean_templates,
        instances=clean_instances,
    )


def export_avd_yaml(
    templates: Iterable[Template],
    instances: Iterable[AvdInfo | Mapping[str, Any]] = (),
    *,
    generated_at: datetime | None = None,
) -> str:
    """Serialise templates + instances as deterministic, secret-free YAML."""
    export = build_export(templates, instances)
    payload = export.model_dump(mode="json")
    body = yaml.safe_dump(
        payload,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=120,
    )
    header = EXPORT_HEADER.format(
        version=FORMAT_VERSION,
        generated_at=(generated_at or utcnow()).isoformat(timespec="seconds"),
    )
    return header + body


def dump_export(export: AvdTemplateExport, *, generated_at: datetime | None = None) -> str:
    """Serialise an already-built export model."""
    return export_avd_yaml(export.templates, export.instances, generated_at=generated_at)


# ---------------------------------------------------------------------------
# Import / validation
# ---------------------------------------------------------------------------


def _collect_secrets(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            if is_secret_key(str(key)):
                found.append(child)
            else:
                found.extend(_collect_secrets(item, child))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_collect_secrets(item, f"{path}[{index}]"))
    return found


def _require_mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{what} must be a mapping")
    return value


def _parse_template_entry(entry: Any, index: int) -> Template:
    data = _require_mapping(entry, f"templates[{index}]")
    try:
        return Template.model_validate(dict(data))
    except Exception as exc:  # noqa: BLE001 - pydantic error converted
        raise ValidationError(f"invalid template at templates[{index}]: {exc}") from exc


def _parse_instance_entry(entry: Any, index: int) -> dict[str, Any]:
    data = dict(_require_mapping(entry, f"instances[{index}]"))
    unknown = sorted(set(data) - _INSTANCE_KEYS)
    if unknown:
        raise ValidationError(
            f"instances[{index}] has unsupported keys: {', '.join(unknown)}",
            hint=f"supported instance keys: {', '.join(sorted(_INSTANCE_KEYS))}",
        )
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValidationError(f"instances[{index}].name must not be empty")
    if not _NAME_RE.match(name):
        raise ValidationError(
            f"instances[{index}].name {name!r} is invalid: use letters, digits, '.', '_' and '-'"
        )
    data["name"] = name
    for field in ("template_id", "image_id", "device_id", "proxy_id"):
        if data.get(field) is not None:
            text = str(data[field]).strip()
            if not text:
                data.pop(field, None)
            else:
                data[field] = text
    config: dict[str, str] = {}
    for field in ("config", "overrides"):
        raw = data.get(field)
        if raw is None:
            continue
        raw_mapping = _require_mapping(raw, f"instances[{index}].{field}")
        try:
            config.update(validate_config_values(raw_mapping))
        except ConfigValidationError as exc:
            raise ValidationError(
                f"invalid config for instances[{index}]: {exc}",
                details={"errors": exc.errors},
            ) from exc
    data["config"] = config
    data.pop("overrides", None)
    for field in ("tags", "description"):
        if field in data and not isinstance(data[field], (list, str)):
            raise ValidationError(f"instances[{index}].{field} has an invalid type")
    return data


def parse_avd_yaml(content: str) -> AvdTemplateExport:
    """Parse + validate a YAML fleet definition; raises ``ValidationError``."""
    if not isinstance(content, str) or not content.strip():
        raise ValidationError("YAML definition is empty")
    try:
        loaded = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ValidationError(f"invalid YAML: {exc}") from exc
    if loaded is None:
        raise ValidationError("YAML definition is empty")
    data = _require_mapping(loaded, "YAML definition")
    version = data.get("version", FORMAT_VERSION)
    if version not in SUPPORTED_VERSIONS:
        raise ValidationError(
            f"unsupported YAML definition version {version!r}",
            hint=f"supported versions: {', '.join(str(item) for item in sorted(SUPPORTED_VERSIONS, key=str))}",
        )
    secrets = _collect_secrets(data)
    if secrets:
        raise ValidationError(
            "YAML definitions must not contain credentials",
            hint=f"remove secret fields: {', '.join(secrets[:10])}",
        )
    raw_templates = data.get("templates") or []
    raw_instances = data.get("instances") or []
    if not isinstance(raw_templates, list):
        raise ValidationError("'templates' must be a list")
    if not isinstance(raw_instances, list):
        raise ValidationError("'instances' must be a list")
    templates = [_parse_template_entry(entry, index) for index, entry in enumerate(raw_templates)]
    instances = [_parse_instance_entry(entry, index) for index, entry in enumerate(raw_instances)]
    seen: set[str] = set()
    for template in templates:
        if template.id in seen:
            raise ValidationError(f"duplicate template id in YAML: {template.id!r}")
        seen.add(template.id)
    seen_names: set[str] = set()
    for instance in instances:
        name = instance["name"]
        if name in seen_names:
            raise ValidationError(f"duplicate instance name in YAML: {name!r}")
        seen_names.add(name)
    return AvdTemplateExport(version=FORMAT_VERSION, templates=templates, instances=instances)


def load_avd_yaml(path: str) -> AvdTemplateExport:
    """Read and validate a YAML definition from disk."""
    try:
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
    except OSError as exc:
        raise ValidationError(f"could not read YAML definition {path}: {exc}") from exc
    return parse_avd_yaml(content)


__all__ = [
    "EXPORT_HEADER",
    "FORMAT_VERSION",
    "SUPPORTED_VERSIONS",
    "build_export",
    "dump_export",
    "export_avd_yaml",
    "load_avd_yaml",
    "parse_avd_yaml",
    "sanitise_config",
    "sanitise_instance",
    "sanitise_template",
]
