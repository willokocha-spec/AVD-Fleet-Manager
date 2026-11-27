"""Static contract checks (Wave 0 freeze guard).

These run without a daemon and fail fast if the checked-in contracts drift or
contain broken local references.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"


def load_yaml(name: str) -> dict[str, Any]:
    return yaml.safe_load((CONTRACTS / name).read_text(encoding="utf-8"))


def load_json(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def resolve_pointer(document: dict[str, Any], pointer: str) -> Any:
    if not pointer.startswith("#/"):
        raise AssertionError(f"only local refs supported: {pointer}")
    node: Any = document
    for raw in pointer[2:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        node = node[int(token)] if isinstance(node, list) else node[token]
    return node


def walk_refs(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(walk_refs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(walk_refs(item))
    return found


def test_openapi_is_31_and_has_all_numbered_paths() -> None:
    spec = load_yaml("openapi.yaml")
    assert spec["openapi"].startswith("3.1")
    assert spec["info"]["version"]
    paths = spec["paths"]
    assert len(paths) >= 50
    for path in paths:
        assert path.startswith("/api/v1") or path == "/health", path


def test_every_operation_has_unique_id_and_tag() -> None:
    spec = load_yaml("openapi.yaml")
    seen: dict[str, str] = {}
    for path, item in spec["paths"].items():
        for method in ("get", "post", "put", "patch", "delete"):
            operation = item.get(method)
            if not operation:
                continue
            op_id = operation.get("operationId")
            assert op_id, f"{method.upper()} {path} is missing operationId"
            assert op_id not in seen, f"duplicate operationId {op_id} ({path}, {seen[op_id]})"
            seen[op_id] = path
            assert operation.get("tags"), f"{method.upper()} {path} is missing tags"
            assert "responses" in operation


def test_all_local_refs_resolve() -> None:
    spec = load_yaml("openapi.yaml")
    broken = [ref for ref in walk_refs(spec) if not ref.startswith("#/") or not _resolves(spec, ref)]
    assert not broken, f"broken refs: {broken}"


def _resolves(document: dict[str, Any], ref: str) -> bool:
    try:
        resolve_pointer(document, ref)
        return True
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def test_path_parameters_are_declared() -> None:
    spec = load_yaml("openapi.yaml")
    for path, item in spec["paths"].items():
        declared = {
            param.get("name")
            for param in item.get("parameters", [])
            if isinstance(param, dict)
        }
        for method, operation in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            declared_here = set(declared)
            for param in operation.get("parameters", []):
                if "$ref" in param:
                    resolved = resolve_pointer(spec, param["$ref"])
                    declared_here.add(resolved["name"])
                else:
                    declared_here.add(param.get("name"))
            for segment in path.split("/"):
                if segment.startswith("{") and segment.endswith("}"):
                    name = segment[1:-1]
                    assert name in declared_here, f"{method.upper()} {path} missing parameter {name}"


def test_fleet_schema_example_validates() -> None:
    schema = load_json("fleet.schema.json")
    example = schema["examples"][0]
    jsonschema.validate(example, schema)


def test_fleet_schema_rejects_bad_enum() -> None:
    schema = load_json("fleet.schema.json")
    example = copy.deepcopy(schema["examples"][0])
    example["templates"][0]["gpu_mode"] = "software"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(example, schema)


def test_events_schema_accepts_state_and_output() -> None:
    schema = load_json("events.schema.json")
    state = {
        "id": "e1",
        "type": "instance.state",
        "ts": "2026-11-26T18:00:00Z",
        "source": "avdfleet",
        "seq": 1,
        "payload": {"instance": "avd-01", "state": "running"},
    }
    jsonschema.validate(state, schema)
    output = {
        "id": "e2",
        "type": "script.run.output",
        "ts": "2026-11-26T18:00:01Z",
        "payload": {
            "run_id": "r1",
            "instance": "avd-01",
            "stream": "stdout",
            "line": "hello",
        },
    }
    jsonschema.validate(output, schema)


def test_events_schema_rejects_unknown_type() -> None:
    schema = load_json("events.schema.json")
    event = {"id": "e1", "type": "nope", "ts": "2026-11-26T18:00:00Z", "payload": {}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(event, schema)


def test_bundle_schema_accepts_manifest() -> None:
    schema = load_json("bundle.schema.json")
    manifest = {
        "format_version": "1",
        "name": "avdfleet-portable",
        "version": "0.1.0",
        "created_at": "2026-11-26T18:00:00Z",
        "host_os": "windows",
        "host_arch": "x86_64",
        "root": "D:/AVD-Fleet",
        "offline": True,
        "python_version": "3.12.14",
        "total_bytes": 3,
        "entries": [{"path": "avdfleet.exe", "sha256": "a" * 64, "size": 3}],
    }
    jsonschema.validate(manifest, schema)


def test_fakes_spec_mentions_every_required_tool() -> None:
    text = (CONTRACTS / "fakes-spec.md").read_text(encoding="utf-8")
    for tool in ("adb", "emulator", "sdkmanager", "avdmanager", "android", "appium"):
        assert tool in text
    assert "AVDFLEET_FAKE_HOME" in text
