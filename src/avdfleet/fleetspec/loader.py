"""Parse, validate and resolve ``fleet.yaml`` documents.

``FleetLoader`` is the single entry point used by the CLI, the API and the
reconciler:

* ``load`` / ``loads`` / ``from_dict`` parse YAML text, a file or a mapping;
* ``load_document`` validates against ``contracts/fleet.schema.json`` and then the
  frozen :class:`~avdfleet.core.models.FleetSpec` pydantic model, raising a
  :class:`~avdfleet.core.errors.FleetSpecError` with JSON paths and hints;
* ``validate`` returns the same problems as a list of strings (protocol shape);
* ``resolve`` expands instance defaults (RAM/proxy inheritance), checks template
  and proxy references, capacity and name collisions, and -- when an image
  catalogue is injected -- verifies that every required image is known/installed;
* ``resolve_proxy_passwords`` reads ``password_env`` variables from the process
  environment **at apply time**.  Passwords never appear in fleet.yaml, in the
  resolved fleet, in plans or in error messages.
"""

from __future__ import annotations

import inspect
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr
from pydantic import ValidationError as PydanticValidationError

from ..core.errors import FleetSpecError, NotFoundError
from ..core.models import (
    FleetInstanceSpec,
    FleetProxySpec,
    FleetSpec,
    FleetTemplateSpec,
    SystemImage,
)
from .schema import (
    FLEET_API_VERSION,
    FLEET_KIND,
    SchemaIssue,
    fleet_document_error,
    validate_document,
)

#: How a known-but-not-installed system image is treated by validation.
IMAGE_POLICIES: tuple[str, ...] = ("warn", "fail", "ignore")

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_IMAGE_PREFIX = "system-images;"


# ---------------------------------------------------------------------------
# Resolved fleet
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedFleet:
    """A fleet spec with instance defaults expanded and references resolved.

    ``instances`` keeps the declaration order of the spec but every instance has
    its effective ``ram_mb`` and ``proxy`` filled in (inherited from the template
    when the instance did not override them).
    """

    spec: FleetSpec
    templates: tuple[FleetTemplateSpec, ...]
    proxies: tuple[FleetProxySpec, ...]
    instances: tuple[FleetInstanceSpec, ...]
    images: Mapping[str, SystemImage | None] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def capacity(self) -> int:
        return int(self.spec.capacity)

    @property
    def template_names(self) -> tuple[str, ...]:
        return tuple(template.name for template in self.templates)

    @property
    def proxy_ids(self) -> tuple[str, ...]:
        return tuple(proxy.id for proxy in self.proxies)

    @property
    def instance_names(self) -> tuple[str, ...]:
        return tuple(instance.name for instance in self.instances)

    def template(self, name: str) -> FleetTemplateSpec | None:
        for template in self.templates:
            if template.name == name:
                return template
        return None

    def proxy(self, proxy_id: str) -> FleetProxySpec | None:
        for proxy in self.proxies:
            if proxy.id == proxy_id:
                return proxy
        return None

    def image_for(self, template_name: str) -> SystemImage | None:
        return self.images.get(template_name)

    def image_ids(self) -> tuple[str, ...]:
        return tuple(sorted({template.image for template in self.templates}))


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class FleetLoader:
    """Load, validate and resolve declarative fleet documents.

    Parameters
    ----------
    image_catalog:
        Optional duck-typed :class:`~avdfleet.core.interfaces.ImageCatalog`.  When
        supplied, ``validate``/``resolve`` verify that every template image exists
        in the catalogue and (optionally, per ``image_policy``) is installed.
    schema_path:
        Override for ``contracts/fleet.schema.json``.
    environ:
        Optional mapping used instead of ``os.environ`` when resolving proxy
        passwords.  Production code leaves this unset so secrets are read at
        apply time; tests inject a mapping.
    image_policy:
        ``"warn"`` (default), ``"fail"`` or ``"ignore"`` for known-but-not-installed
        images.  Applied by ``resolve(require_images=True)``; the planner performs
        the same check with the identical policy for plans.
    """

    def __init__(
        self,
        *,
        image_catalog: Any | None = None,
        schema_path: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
        image_policy: str = "warn",
    ) -> None:
        if image_policy not in IMAGE_POLICIES:
            raise ValueError(
                f"image_policy must be one of {', '.join(IMAGE_POLICIES)}; got {image_policy!r}"
            )
        self._image_catalog = image_catalog
        self._schema_path = schema_path
        self._environ = (
            None if environ is None else {str(key): str(value) for key, value in environ.items()}
        )
        self._image_policy = image_policy

    # -- properties ---------------------------------------------------------
    @property
    def image_catalog(self) -> Any | None:
        return self._image_catalog

    @property
    def schema_path(self) -> str | Path | None:
        return self._schema_path

    @property
    def image_policy(self) -> str:
        return self._image_policy

    # -- environment --------------------------------------------------------
    def environment(self) -> dict[str, str]:
        """Return the environment used for ``password_env`` resolution.

        When no mapping was injected this reads ``os.environ`` *now*, which is what
        "resolved at apply time" means: changing the variable between planning and
        applying takes effect.
        """
        if self._environ is not None:
            return dict(self._environ)
        return dict(os.environ)

    # -- public loading API -------------------------------------------------
    def load(self, path: str | Path, *, check_images: bool = False) -> FleetSpec:
        """Load and validate a fleet document from *path*.

        A missing file raises :class:`FileNotFoundError` (the API maps it to a 404);
        every other problem raises :class:`FleetSpecError`.
        """
        candidate = Path(path).expanduser()
        try:
            text = candidate.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"fleet spec file not found: {candidate}") from exc
        except IsADirectoryError as exc:
            raise FleetSpecError(
                f"fleet spec path is a directory, not a file: {candidate}",
                hint="pass the path to a fleet.yaml file",
            ) from exc
        except OSError as exc:
            raise FleetSpecError(
                f"cannot read the fleet spec file {candidate}: {exc}",
                hint="check the file permissions",
            ) from exc
        spec = self.loads(text, source=str(candidate))
        if check_images:
            self.resolve(spec, require_images=True)
        return spec

    def loads(self, text: str, *, source: str = "<string>") -> FleetSpec:
        """Parse and validate a YAML string."""
        if not isinstance(text, str) or not text.strip():
            raise FleetSpecError(
                f"fleet spec from {source} is empty",
                hint="provide a document with apiVersion, kind, name and templates",
            )
        document = self._parse_yaml(text, source=source)
        return self.load_document(document, source=source)

    def from_dict(self, data: Mapping[str, Any], *, source: str = "<mapping>") -> FleetSpec:
        """Validate an already parsed mapping (the API ``spec`` request field)."""
        if not isinstance(data, Mapping):
            raise FleetSpecError(
                f"fleet spec from {source} must be a mapping, got {type(data).__name__}",
                hint="the top level of a fleet document is a JSON/YAML object",
            )
        return self.load_document(dict(data), source=source)

    def load_document(self, document: Any, *, source: str = "<document>") -> FleetSpec:
        """Validate a parsed document against the schema and the core model."""
        if not isinstance(document, Mapping):
            raise FleetSpecError(
                f"fleet spec from {source} must be a mapping, got {type(document).__name__}",
                hint="the top level of fleet.yaml is a mapping with apiVersion, kind, name and templates",
            )
        issues = validate_document(document, schema_path=self._schema_path, source=source)
        if issues:
            raise fleet_document_error(issues, source=source)
        try:
            spec = FleetSpec.model_validate(dict(document))
        except PydanticValidationError as exc:
            raise _pydantic_error(exc, source=source) from exc
        semantic, _warnings = self._semantic_issues(spec, require_images=False)
        if semantic:
            raise _issues_error(semantic, source=source)
        return spec

    # -- validation ---------------------------------------------------------
    def validate(self, spec: FleetSpec) -> list[str]:
        """Return human-readable problems for *spec* (empty list means valid).

        This is the shape required by :class:`avdfleet.core.interfaces.FleetReconciler`
        and includes image-requirement checks when a catalogue was injected.
        """
        if isinstance(spec, Mapping):
            try:
                spec = self.load_document(spec, source="<mapping>")
            except FleetSpecError as exc:
                return [exc.message]
        if not isinstance(spec, FleetSpec):
            return [f"not a fleet spec: {type(spec).__name__}"]
        issues, warnings = self._semantic_issues(spec, require_images=True)
        return [str(issue) for issue in issues] + list(warnings)

    def resolve(self, spec: FleetSpec | Mapping[str, Any], *, require_images: bool = False) -> ResolvedFleet:
        """Return a :class:`ResolvedFleet` with defaults expanded and refs checked.

        ``require_images`` additionally verifies every template image against the
        injected catalogue (unknown images are errors; known-but-not-installed ones
        follow ``image_policy``).
        """
        if isinstance(spec, Mapping):
            spec = self.load_document(spec, source="<mapping>")
        if not isinstance(spec, FleetSpec):
            raise FleetSpecError(
                f"resolve() expects a FleetSpec, got {type(spec).__name__}",
                hint="call load()/loads()/from_dict() first",
            )
        issues, warnings = self._semantic_issues(spec, require_images=require_images)
        if issues:
            raise _issues_error(issues, source="fleet spec")
        template_by_name = {template.name: template for template in spec.templates}
        instances: list[FleetInstanceSpec] = []
        for instance in spec.instances:
            template = template_by_name[instance.template]
            effective_proxy = instance.proxy if instance.proxy is not None else template.proxy
            effective_ram = instance.ram_mb if instance.ram_mb is not None else template.ram_mb
            instances.append(
                instance.model_copy(update={"proxy": effective_proxy, "ram_mb": effective_ram})
            )
        images: dict[str, SystemImage | None] = {}
        if require_images and self._image_catalog is not None:
            for template in spec.templates:
                images[template.name] = self._lookup_image(template.image)
        return ResolvedFleet(
            spec=spec,
            templates=tuple(spec.templates),
            proxies=tuple(sorted(spec.proxies, key=lambda proxy: proxy.id)),
            instances=tuple(instances),
            images=images,
            warnings=tuple(warnings),
        )

    # -- secrets ------------------------------------------------------------
    def resolve_proxy_passwords(
        self,
        fleet: ResolvedFleet | FleetSpec,
        *,
        required: bool = True,
    ) -> dict[str, SecretStr]:
        """Resolve ``password_env`` values from the environment at apply time.

        Only environment *names* live in the fleet document.  Returns a mapping of
        proxy id to :class:`~pydantic.SecretStr`; the plaintext never leaves this
        method (no logging, no error details).  With ``required=False`` missing
        variables are skipped instead of raising.
        """
        spec = fleet.spec if isinstance(fleet, ResolvedFleet) else fleet
        env = self.environment()
        resolved: dict[str, SecretStr] = {}
        missing: list[dict[str, str]] = []
        for proxy in spec.proxies:
            if not proxy.password_env:
                continue
            value = env.get(proxy.password_env)
            if value is None or value == "":
                missing.append({"proxy": proxy.id, "variable": proxy.password_env})
                continue
            resolved[proxy.id] = SecretStr(value)
        if missing and required:
            variables = ", ".join(sorted({item["variable"] for item in missing}))
            raise FleetSpecError(
                f"proxy password environment variable(s) not set: {variables}",
                hint=(
                    f"export {variables} before applying the fleet; "
                    "passwords are never read from fleet.yaml"
                ),
                details={"missing": missing},
            )
        return resolved

    # -- internals ----------------------------------------------------------
    def _parse_yaml(self, text: str, *, source: str) -> Any:
        try:
            documents = [document for document in yaml.safe_load_all(text) if document is not None]
        except yaml.YAMLError as exc:
            line = column = None
            mark = getattr(exc, "problem_mark", None)
            if mark is not None:
                line = mark.line + 1
                column = mark.column + 1
            problem = getattr(exc, "problem", None) or str(exc)
            location = f" at line {line}, column {column}" if line is not None else ""
            raise FleetSpecError(
                f"invalid YAML in {source}{location}: {problem}",
                hint="check the indentation and quoting of the fleet document",
                details={"source": source, "line": line, "column": column, "problem": problem},
            ) from exc
        if not documents:
            raise FleetSpecError(
                f"fleet spec from {source} is empty",
                hint="provide a document with apiVersion, kind, name and templates",
            )
        if len(documents) > 1:
            raise FleetSpecError(
                f"fleet spec from {source} contains {len(documents)} YAML documents",
                hint="a fleet file must contain exactly one document; split fleets into separate files",
                details={"source": source, "documents": len(documents)},
            )
        return documents[0]

    def _semantic_issues(
        self,
        spec: FleetSpec,
        *,
        require_images: bool,
    ) -> tuple[list[SchemaIssue], list[str]]:
        issues: list[SchemaIssue] = []
        warnings: list[str] = []

        # -- name collisions -------------------------------------------------
        template_index: dict[str, int] = {}
        for index, template in enumerate(spec.templates):
            path = f"$.templates[{index}].name"
            if template.name in template_index:
                issues.append(
                    SchemaIssue(
                        path,
                        f"duplicate template name {template.name!r} "
                        f"(already declared at index {template_index[template.name]})",
                        "unique",
                        "template names are unique and become stable template ids",
                    )
                )
            else:
                template_index[template.name] = index
            for script_index, script in enumerate(template.scripts):
                if not str(script).strip():
                    issues.append(
                        SchemaIssue(
                            f"$.templates[{index}].scripts[{script_index}]",
                            "script reference must not be empty",
                            "minLength",
                            "remove the empty entry or name an existing script",
                        )
                    )

        proxy_index: dict[str, int] = {}
        for index, proxy in enumerate(spec.proxies):
            if proxy.id in proxy_index:
                issues.append(
                    SchemaIssue(
                        f"$.proxies[{index}].id",
                        f"duplicate proxy id {proxy.id!r} "
                        f"(already declared at index {proxy_index[proxy.id]})",
                        "unique",
                        "proxy ids are unique and are used for instance assignments",
                    )
                )
            else:
                proxy_index[proxy.id] = index
            if proxy.password_env is not None:
                if not _ENV_NAME_RE.match(proxy.password_env):
                    issues.append(
                        SchemaIssue(
                            f"$.proxies[{index}].password_env",
                            f"invalid environment variable name {proxy.password_env!r}",
                            "pattern",
                            "use an environment variable name such as AVDFLEET_PROXY_1_PASSWORD",
                        )
                    )
                if not proxy.username:
                    issues.append(
                        SchemaIssue(
                            f"$.proxies[{index}].password_env",
                            "password_env requires a username",
                            "required",
                            "add username for the authenticated proxy, or remove password_env",
                        )
                    )

        instance_index: dict[str, int] = {}
        for index, instance in enumerate(spec.instances):
            if instance.name in instance_index:
                issues.append(
                    SchemaIssue(
                        f"$.instances[{index}].name",
                        f"duplicate instance name {instance.name!r} "
                        f"(already declared at index {instance_index[instance.name]})",
                        "unique",
                        "instance names become unique AVD names",
                    )
                )
            else:
                instance_index[instance.name] = index

        # -- capacity --------------------------------------------------------
        if not 1 <= int(spec.capacity) <= 64:
            issues.append(
                SchemaIssue(
                    "$.capacity",
                    f"capacity must be between 1 and 64, got {spec.capacity}",
                    "range",
                    "the emulator can run at most 64 concurrent instances",
                )
            )
        elif len(spec.instances) > int(spec.capacity):
            issues.append(
                SchemaIssue(
                    "$.instances",
                    f"fleet declares {len(spec.instances)} instances but capacity is {spec.capacity}",
                    "capacity",
                    "raise capacity (maximum 64) or remove instances",
                )
            )

        # -- references ------------------------------------------------------
        template_names = set(template_index)
        proxy_ids = set(proxy_index)
        known_templates = ", ".join(sorted(template_names)) or "<none>"
        known_proxies = ", ".join(sorted(proxy_ids)) or "<none>"
        for index, template in enumerate(spec.templates):
            if template.proxy is not None and template.proxy not in proxy_ids:
                issues.append(
                    SchemaIssue(
                        f"$.templates[{index}].proxy",
                        f"template {template.name!r} references unknown proxy {template.proxy!r}",
                        "reference",
                        f"declare the proxy under $.proxies (known ids: {known_proxies})",
                    )
                )
        for index, instance in enumerate(spec.instances):
            if instance.template not in template_names:
                issues.append(
                    SchemaIssue(
                        f"$.instances[{index}].template",
                        f"instance {instance.name!r} references unknown template "
                        f"{instance.template!r}",
                        "reference",
                        f"declare the template under $.templates (known names: {known_templates})",
                    )
                )
            if instance.proxy is not None and instance.proxy not in proxy_ids:
                issues.append(
                    SchemaIssue(
                        f"$.instances[{index}].proxy",
                        f"instance {instance.name!r} references unknown proxy {instance.proxy!r}",
                        "reference",
                        f"declare the proxy under $.proxies (known ids: {known_proxies})",
                    )
                )

        # -- image requirements ---------------------------------------------
        if require_images and self._image_catalog is not None:
            for index, template in enumerate(spec.templates):
                image = self._require_image(template.image, issues, index)
                if image is None:
                    continue
                if not image.installed:
                    message = (
                        f"system image {template.image!r} is known but not installed "
                        f"(template {template.name!r})"
                    )
                    if self._image_policy == "fail":
                        issues.append(
                            SchemaIssue(
                                f"$.templates[{index}].image",
                                message,
                                "image",
                                f"install it first: `avdfleet images download {template.image}`",
                            )
                        )
                    else:
                        warnings.append(message)

        return issues, warnings

    def _require_image(
        self,
        image_id: str,
        issues: list[SchemaIssue],
        index: int,
    ) -> SystemImage | None:
        try:
            image = self._lookup_image(image_id)
        except FleetSpecError as exc:
            issues.append(
                SchemaIssue(
                    f"$.templates[{index}].image",
                    exc.message,
                    "image",
                    exc.hint or "",
                )
            )
            return None
        if image is None:
            installed = self._installed_image_ids()
            hint = "install or import the system image and refresh the catalogue"
            if installed:
                hint = f"{hint}; installed images: {', '.join(installed[:5])}"
            issues.append(
                SchemaIssue(
                    f"$.templates[{index}].image",
                    f"system image {image_id!r} is not present in the image catalogue",
                    "image",
                    hint,
                )
            )
            return None
        return image

    def _lookup_image(self, image_id: str) -> SystemImage | None:
        catalog = self._image_catalog
        if catalog is None:
            return None
        getter = getattr(catalog, "get_image", None)
        if callable(getter):
            try:
                return getter(image_id)  # type: ignore[no-any-return]
            except NotFoundError:
                return None
            except Exception as exc:
                raise FleetSpecError(
                    f"image catalogue lookup failed for {image_id!r}: {exc}",
                    hint="check the Android SDK/system image catalogue and retry",
                ) from exc
        lister = getattr(catalog, "list_images", None)
        if callable(lister):
            try:
                images = _invoke(lister, [], {"include_remote": True})
            except Exception as exc:
                raise FleetSpecError(
                    f"image catalogue listing failed: {exc}",
                    hint="check the Android SDK/system image catalogue and retry",
                ) from exc
            for image in images or []:
                if _matches_image(image, image_id):
                    return image  # type: ignore[no-any-return]
        return None

    def _installed_image_ids(self) -> list[str]:
        catalog = self._image_catalog
        if catalog is None:
            return []
        lister = getattr(catalog, "list_images", None)
        if not callable(lister):
            return []
        try:
            images = _invoke(lister, [], {"include_remote": False, "installed_only": True})
        except Exception:
            return []
        identifiers: list[str] = []
        for image in images or []:
            if not getattr(image, "installed", False):
                continue
            candidate = str(getattr(image, "id", "") or getattr(image, "package", "") or "")
            if candidate:
                identifiers.append(candidate)
        return sorted(identifiers)


# ---------------------------------------------------------------------------
# Module-level convenience API
# ---------------------------------------------------------------------------


def load_fleet_file(
    path: str | Path,
    *,
    image_catalog: Any | None = None,
    schema_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    image_policy: str = "warn",
    check_images: bool = False,
) -> FleetSpec:
    """Load a fleet document from a file path."""
    loader = FleetLoader(
        image_catalog=image_catalog,
        schema_path=schema_path,
        environ=environ,
        image_policy=image_policy,
    )
    return loader.load(path, check_images=check_images)


def load_fleet_text(
    text: str,
    *,
    source: str = "<string>",
    image_catalog: Any | None = None,
    schema_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    image_policy: str = "warn",
) -> FleetSpec:
    """Load a fleet document from YAML text."""
    loader = FleetLoader(
        image_catalog=image_catalog,
        schema_path=schema_path,
        environ=environ,
        image_policy=image_policy,
    )
    return loader.loads(text, source=source)


def load_fleet_dict(
    data: Mapping[str, Any],
    *,
    source: str = "<mapping>",
    image_catalog: Any | None = None,
    schema_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    image_policy: str = "warn",
) -> FleetSpec:
    """Load a fleet document from an already parsed mapping."""
    loader = FleetLoader(
        image_catalog=image_catalog,
        schema_path=schema_path,
        environ=environ,
        image_policy=image_policy,
    )
    return loader.from_dict(data, source=source)


def load_fleet(
    source: str | Path | Mapping[str, Any],
    *,
    image_catalog: Any | None = None,
    schema_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    image_policy: str = "warn",
    check_images: bool = False,
) -> FleetSpec:
    """Load a fleet from a mapping, a file path or YAML text.

    Strings containing a newline (or an obvious YAML header) are parsed as
    documents; anything else is treated as a file path, matching CLI behaviour.
    """
    loader = FleetLoader(
        image_catalog=image_catalog,
        schema_path=schema_path,
        environ=environ,
        image_policy=image_policy,
    )
    if isinstance(source, Mapping):
        return loader.from_dict(source)
    if isinstance(source, Path):
        return loader.load(source, check_images=check_images)
    text = str(source)
    stripped = text.lstrip()
    if "\n" in text or stripped.startswith(("apiVersion", "---", "{")):
        return loader.loads(text)
    return loader.load(text, check_images=check_images)


def validate_fleet(
    spec: FleetSpec | Mapping[str, Any],
    *,
    image_catalog: Any | None = None,
    schema_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    image_policy: str = "warn",
) -> list[str]:
    """Validate *spec* and return human-readable problems (empty means valid)."""
    loader = FleetLoader(
        image_catalog=image_catalog,
        schema_path=schema_path,
        environ=environ,
        image_policy=image_policy,
    )
    if isinstance(spec, Mapping):
        try:
            spec = loader.from_dict(spec)
        except FleetSpecError as exc:
            return [exc.message]
    return loader.validate(spec)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invoke(function: Any, positional: list[Any], kwargs: dict[str, Any]) -> Any:
    """Call *function* passing only the keyword arguments its signature accepts."""
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins/odd callables
        return function(*positional, **kwargs)
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return function(*positional, **kwargs)
    accepted = {
        name: value
        for name, value in kwargs.items()
        if name in parameters
        and parameters[name].kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return function(*positional, **accepted)


def _matches_image(image: Any, query: str) -> bool:
    normalized = query.strip()
    if not normalized:
        return False
    candidates = {
        str(getattr(image, "id", "") or ""),
        str(getattr(image, "package", "") or ""),
        _coordinates(image),
    }
    for candidate in candidates:
        if not candidate:
            continue
        value = candidate.strip()
        if value == normalized:
            return True
        if value.removeprefix(_IMAGE_PREFIX) == normalized.removeprefix(_IMAGE_PREFIX):
            return True
    return False


def _coordinates(image: Any) -> str:
    api_level = getattr(image, "api_level", None)
    tag = getattr(image, "tag", None)
    abi = getattr(image, "abi", None)
    if api_level and tag and abi:
        return f"system-images;android-{api_level};{tag};{abi}"
    return ""


def _pydantic_error(exc: PydanticValidationError, *, source: str) -> FleetSpecError:
    issues: list[SchemaIssue] = []
    for error in exc.errors():
        location = [part for part in error.get("loc", ()) if part != "__root__"]
        path = _location_path(location)
        issues.append(
            SchemaIssue(
                path,
                str(error.get("msg", "invalid value")),
                str(error.get("type", "")),
                "fix the value reported at this JSON path",
            )
        )
    return _issues_error(issues, source=source)


def _location_path(location: Sequence[Any]) -> str:
    path = "$"
    for part in location:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


def _issues_error(issues: Sequence[SchemaIssue], *, source: str) -> FleetSpecError:
    hints = [issue.hint for issue in issues if issue.hint]
    shown = "; ".join(str(issue) for issue in issues[:5])
    if len(issues) > 5:
        shown += f"; … and {len(issues) - 5} more problem(s)"
    return FleetSpecError(
        f"fleet spec from {source} is invalid ({len(issues)} problem(s)): {shown}",
        hint="; ".join(dict.fromkeys(hints[:3])) or "fix the fleet document and retry",
        details={"source": source, "errors": [issue.as_dict() for issue in issues]},
    )


__all__ = [
    "FLEET_API_VERSION",
    "FLEET_KIND",
    "IMAGE_POLICIES",
    "FleetLoader",
    "ResolvedFleet",
    "load_fleet",
    "load_fleet_dict",
    "load_fleet_file",
    "load_fleet_text",
    "validate_fleet",
]
