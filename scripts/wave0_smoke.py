"""Wave 0 smoke checks. Run with the bundled Python from the repo root."""

from __future__ import annotations

import json
import pathlib

import yaml

root = pathlib.Path(__file__).resolve().parents[1]
for rel in [
    "contracts/openapi.yaml",
    "contracts/events.schema.json",
    "contracts/fleet.schema.json",
    "contracts/bundle.schema.json",
]:
    path = root / rel
    try:
        if path.suffix in (".yaml", ".yml"):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
        extra = len(data.get("paths", {})) if isinstance(data, dict) else ""
        print(rel, "OK", type(data).__name__, extra)
    except Exception as exc:  # noqa: BLE001
        print(rel, "FAIL", type(exc).__name__, exc)

from avdfleet.core.config import Config
from avdfleet.core.logging import redact
from avdfleet.core.ports import PortAllocator

samples = [
    "connecting http://alice:s3cret@127.0.0.1:8081 to guest",
    '{"password":"hunter2","token":"abc123"}',
    "Authorization: Bearer eyJhbGciOi.eyJzdWIiOi.sig",
    "--password hunter2 --token abc",
    "socks5://user:pass@10.0.0.1:1080",
    "plain message",
]
for sample in samples:
    print(repr(sample), "=>", redact(sample))

alloc = PortAllocator(start=5600, end=5700, appium_start=4800, bridge_start=7900)
ports = alloc.lease("x")
print("ports", ports)
print("cap", alloc.capacity())
alloc.release("x")
print("cap2", alloc.capacity())

config = Config.load(
    overrides={"max_instances": 5, "log_level": "debug"},
    environ={"AVDFLEET_API_PORT": "9000"},
)
print("config", config.api_port, config.max_instances, config.log_level)
