"""JSON Schema support for the declarative fleet document.

The frozen contract lives at ``contracts/fleet.schema.json``.  This module loads it,
validates a parsed fleet document and converts every ``jsonschema`` error into a
:class:`SchemaIssue` that carries a JSON path, a human message and an actionable
hint.  Callers turn those issues into a :class:`~avdfleet.core.errors.FleetSpecError`
so the CLI, the API and the tests all report the same structured detail.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from ..core.errors import FleetSpecError

#: Values frozen by ``contracts/fleet.schema.json``.
FLEET_API_VERSION = "avdfleet/v1"
FLEET_KIND = "Fleet"

#: Checked-in contract location, relative to the repository root.
FLEET_SCHEMA_RELATIVE = Path("contracts") / "fleet.schema.json"
#: Environment override used by tests/portable bundles that ship the contract.
FLEET_SCHEMA_ENV = "AVDFLEET_FLEET_SCHEMA"

_REQUIRED_RE = re.compile(r"'([^']+)' is a required property")
_UNEXPECTED_RE = re.compile(
    r"Additional properties are not allowed \((?P<properties>.*?) (?:was|were) unexpected\)"
)
_QUOTED_RE = re.compile(r"'([^']+)'")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

_SECRET_FIELDS = frozenset(
    {"password", "passwd", "pwd", "secret", "token", "proxy_password", "authorization"}
)


@dataclass(frozen=True)
class SchemaIssue:
    """One validation problem with a machine-friendly JSON path."""

    path: str
    message: str
    keyword: str = ""
    hint: str = ""

    def as_dict(self) -> dict[str, str]:
        payload = {"path": self.path, "message": self.message}
        if self.keyword:
            payload["keyword"] = self.keyword
        if self.hint:
            payload["hint"] = self.hint
        return payload

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


# ---------------------------------------------------------------------------
# Locating and loading the contract
# ---------------------------------------------------------------------------


def _candidate_roots() -> list[Path]:
    """Roots searched for ``contracts/fleet.schema.json``, nearest first."""
    roots: list[Path] = []
    here = Path(__file__).resolve()
    roots.extend(here.parents[:8])
    cwd = Path.cwd().resolve()
    roots.append(cwd)
    roots.extend(cwd.parents[:8])
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def fleet_schema_path(explicit: str | Path | None = None) -> Path:
    """Return the resolved path of the frozen fleet JSON schema.

    Resolution order: the *explicit* argument, ``$AVDFLEET_FLEET_SCHEMA``, then a
    walk from this module and from the current working directory looking for
    ``contracts/fleet.schema.json``.
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FleetSpecError(
                f"fleet JSON schema not found at {path}",
                hint="point AVDFLEET_FLEET_SCHEMA at contracts/fleet.schema.json",
                details={"schema_path": str(path)},
            )
        return path
    override = os.environ.get(FLEET_SCHEMA_ENV)
    if override:
        return fleet_schema_path(override)
    # Installed/portable editions may ship a copy next to this module (packaging
    # force-includes contracts/fleet.schema.json there).
    packaged = Path(__file__).resolve().parent / "fleet.schema.json"
    if packaged.is_file():
        return packaged
    for root in _candidate_roots():
        candidate = root / FLEET_SCHEMA_RELATIVE
        if candidate.is_file():
            return candidate
    raise FleetSpecError(
        "the fleet JSON schema could not be located",
        hint=f"run from the repository checkout or set {FLEET_SCHEMA_ENV} "
        f"to the path of {FLEET_SCHEMA_RELATIVE.as_posix()}",
        details={"searched": [str(root / FLEET_SCHEMA_RELATIVE) for root in _candidate_roots()[:6]]},
    )


@lru_cache(maxsize=8)
def _load_schema_file(path_str: str) -> dict[str, Any]:
    path = Path(path_str)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FleetSpecError(
            f"cannot read the fleet JSON schema: {path}",
            hint="check the file permissions and the AVDFLEET_FLEET_SCHEMA setting",
        ) from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FleetSpecError(
            f"the fleet JSON schema at {path} is not valid JSON: line {exc.lineno}, column {exc.colno}",
            hint="restore the frozen contracts/fleet.schema.json from version control",
        ) from exc
    if not isinstance(document, dict):
        raise FleetSpecError(
            f"the fleet JSON schema at {path} must be a JSON object",
            hint="restore the frozen contracts/fleet.schema.json from version control",
        )
    return document


def load_fleet_schema(path: str | Path | None = None) -> dict[str, Any]:
    """Return the parsed JSON schema document (cached per resolved path)."""
    resolved = fleet_schema_path(path)
    return _load_schema_file(str(resolved.resolve()))


def fleet_schema_example(path: str | Path | None = None) -> dict[str, Any]:
    """Return the canonical ``examples[0]`` document from the contract."""
    schema = load_fleet_schema(path)
    examples = schema.get("examples")
    if not isinstance(examples, list) or not examples or not isinstance(examples[0], dict):
        raise FleetSpecError(
            "the fleet JSON schema does not contain an example document",
            hint="restore the frozen contracts/fleet.schema.json from version control",
        )
    # Deep copy: callers legitimately mutate the returned document in tests/CLI.
    return copy.deepcopy(examples[0])


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_document(
    document: Any,
    *,
    schema_path: str | Path | None = None,
    source: str = "fleet document",
) -> list[SchemaIssue]:
    """Validate *document* against the frozen schema and return every issue.

    The returned issues are sorted by JSON path and de-duplicated so the operators
    see deterministic output.
    """
    schema = load_fleet_schema(schema_path)
    validator = Draft202012Validator(schema)
    try:
        errors = list(validator.iter_errors(document))
    except FleetSpecError:
        raise
    except Exception as exc:  # pragma: no cover - a broken contract is a checkout bug
        raise FleetSpecError(
            f"cannot validate the fleet document from {source}: {exc}",
            hint="restore the frozen contracts/fleet.schema.json from version control",
        ) from exc
    issues: list[SchemaIssue] = []
    for error in errors:
        issues.extend(_issues_for_error(error))
    issues.sort(key=lambda issue: (issue.path, issue.message))
    return _dedupe(issues)


def raise_for_document(
    document: Any,
    *,
    schema_path: str | Path | None = None,
    source: str = "fleet document",
) -> None:
    """Validate *document* and raise :class:`FleetSpecError` when it is invalid."""
    issues = validate_document(document, schema_path=schema_path, source=source)
    if issues:
        raise fleet_document_error(issues, source=source)


def fleet_document_error(
    issues: Sequence[SchemaIssue], *, source: str = "fleet document"
) -> FleetSpecError:
    """Build the structured error raised for an invalid fleet document."""
    hints = [issue.hint for issue in issues if issue.hint]
    shown = "; ".join(str(issue) for issue in issues[:5])
    if len(issues) > 5:
        shown += f"; … and {len(issues) - 5} more problem(s)"
    return FleetSpecError(
        f"fleet spec from {source} does not match contracts/fleet.schema.json "
        f"({len(issues)} problem(s)): {shown}",
        hint="; ".join(dict.fromkeys(hints[:3]))
        or "compare the document with the example in contracts/fleet.schema.json",
        details={
            "source": source,
            "schema": "contracts/fleet.schema.json",
            "errors": [issue.as_dict() for issue in issues],
        },
    )


def format_issues(issues: Sequence[SchemaIssue]) -> list[str]:
    """Render issues as ``path: message (hint)`` lines for CLI output."""
    rendered: list[str] = []
    for issue in issues:
        line = str(issue)
        if issue.hint:
            line = f"{line} ({issue.hint})"
        rendered.append(line)
    return rendered


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _dedupe(issues: Sequence[SchemaIssue]) -> list[SchemaIssue]:
    unique: list[SchemaIssue] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in issues:
        key = (issue.path, issue.message, issue.keyword)
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    return unique


def _issues_for_error(error: Any) -> list[SchemaIssue]:
    parent = _json_path(error)
    message = str(error.message)
    validator = str(error.validator or "")
    if validator == "required":
        match = _REQUIRED_RE.search(message)
        if match is not None:
            prop = match.group(1)
            return [
                SchemaIssue(
                    _join(parent, prop),
                    f"missing required field {prop!r}",
                    validator,
                    _hint_for(validator, prop, parent, error),
                )
            ]
    if validator == "additionalProperties":
        match = _UNEXPECTED_RE.search(message)
        if match is not None:
            issues: list[SchemaIssue] = []
            for prop in _QUOTED_RE.findall(match.group("properties")):
                issues.append(
                    SchemaIssue(
                        _join(parent, prop),
                        f"unknown field {prop!r} is not allowed",
                        validator,
                        _hint_for(validator, prop, parent, error),
                    )
                )
            return issues
    return [SchemaIssue(parent, message, validator, _hint_for(validator, None, parent, error))]


def _hint_for(
    validator: str,
    prop: str | None,
    path: str,
    error: Any,
) -> str:
    value = getattr(error, "validator_value", None)
    if validator == "required":
        return f"add the required field {prop!r}" if prop else "add the missing required field"
    if validator == "additionalProperties":
        if prop in _SECRET_FIELDS:
            return (
                "never put proxy passwords in fleet.yaml: use "
                "password_env: ENV_VAR_NAME and export the secret before applying"
            )
        return "remove the unknown field or fix its spelling (unknown fields are rejected on purpose)"
    if validator == "const":
        if path.endswith("apiVersion"):
            return f"use apiVersion: {FLEET_API_VERSION!r}"
        if path.endswith("kind"):
            return f"use kind: {FLEET_KIND!r}"
        return f"expected the fixed value {value!r}"
    if validator == "enum":
        allowed = ", ".join(str(item) for item in (value or []))
        return f"allowed values: {allowed}"
    if validator == "pattern":
        return f"the value must match the pattern {value!r}"
    if validator in {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}:
        return f"the value must satisfy {validator} {value!r}"
    if validator in {"minItems", "maxItems", "minLength", "maxLength"}:
        return f"the value must satisfy {validator} {value!r}"
    if validator == "type":
        return f"expected a value of type {value!r}"
    if validator in {"anyOf", "oneOf"}:
        return "the value does not match any of the allowed shapes"
    return ""


def _json_path(error: Any) -> str:
    attribute = getattr(error, "json_path", None)
    if isinstance(attribute, str) and attribute:
        return attribute
    return _build_path(list(getattr(error, "absolute_path", []) or []))


def _build_path(parts: Sequence[Any]) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        elif isinstance(part, str) and _IDENTIFIER_RE.match(part):
            path += f".{part}"
        else:
            path += f"[{part!r}]"
    return path


def _join(path: str, prop: str) -> str:
    base = path or "$"
    if base == "$":
        return f"$.{prop}"
    return f"{base}.{prop}"


__all__ = [
    "FLEET_API_VERSION",
    "FLEET_KIND",
    "FLEET_SCHEMA_ENV",
    "FLEET_SCHEMA_RELATIVE",
    "SchemaIssue",
    "fleet_document_error",
    "fleet_schema_example",
    "fleet_schema_path",
    "format_issues",
    "load_fleet_schema",
    "raise_for_document",
    "validate_document",
]
