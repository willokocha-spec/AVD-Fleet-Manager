"""REST API tests for the ``/api/v1/instrumentation/...`` routes.

The tests are end to end *within the process*: a real
:class:`InstrumentationService` (SQLite in memory + a temporary module library +
a temporary toolchain root) is injected through a duck-typed container, so the
routes, the service and the repository are all exercised together.  The only
fakes are the ``adb`` boundary of :class:`DeviceProber` and the ``http_get`` of
:class:`AcquisitionStore` — no emulator, no real ``adb``, no network.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import avdfleet.instrumentation.acquire as acquire_module
from avdfleet.api.app import create_app
from avdfleet.core.config import Config
from avdfleet.core.errors import NotFoundError
from avdfleet.core.paths import Paths
from avdfleet.instrumentation import (
    AcquisitionStore,
    DeviceProber,
    InstrumentationRepository,
    InstrumentationService,
    ModuleLibrary,
    PinnedArtifact,
    TargetDevice,
    ensure_builtin_profiles,
)
from avdfleet.instrumentation.capabilities import CommandResult
from avdfleet.store import Store

ORIGIN = "http://127.0.0.1:8765"
BASE_URL = "http://127.0.0.1:8765"
TOKEN = "instrumentation-api-token-0123456789"
BUILTIN_NAMES = {"Root", "Magisk + Zygisk", "LSPosed", "Xposed Instrumentation", "Custom"}
MAGISK_PROPS = (
    "[ro.build.type]: [user]\n"
    "[ro.build.version.sdk]: [34]\n"
    "[ro.product.cpu.abi]: [x86_64]\n"
    "[ro.debuggable]: [0]\n"
)
ALPHA_PROP = "id=alpha\nname=Alpha\nversion=1.0\n"
LSPOSED_PROP = "id=zygisk_lsposed\nname=Zygisk - LSPosed\nversion=v1.9.2 (7024)\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def auth_headers(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _config() -> Config:
    return Config(
        auth_required=True,
        allow_origins=[ORIGIN],
        trusted_hosts=["127.0.0.1", "localhost"],
        serve_ui=False,
    )


def _paths(tmp_path: Path) -> Paths:
    return Paths(root=tmp_path / "home", portable=True).ensure()


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _magisk_zip(**prop: str) -> bytes:
    fields = {"id": "alpha", "name": "Alpha", "version": "1.0", "versionCode": "1"}
    fields.update(prop)
    body = "".join(f"{key}={value}\n" for key, value in fields.items())
    return _zip_bytes({"module.prop": body.encode("utf-8")})


def _lsposed_zip() -> bytes:
    """A valid LSPosed module ZIP, used as a pinned release stand-in."""
    return _zip_bytes({"module.prop": LSPOSED_PROP.encode("utf-8")})


def _test_pin(data: bytes) -> PinnedArtifact:
    """A pinned ``lsposed`` artifact whose exact bytes this test can produce."""
    return PinnedArtifact(
        key="lsposed",
        name="LSPosed",
        version="1.9.2 (test)",
        url="https://downloads.example.invalid/lsposed/LSPosed-test.zip",
        filename="LSPosed-test.zip",
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        kind="module_zip",
        min_api=27,
        notes="test pin",
    )


def _install_pin(monkeypatch: pytest.MonkeyPatch, pin: PinnedArtifact) -> None:
    """Make the acquisition flow resolve *pin* for one test."""
    monkeypatch.setitem(acquire_module._BY_KEY, pin.key, pin)
    monkeypatch.setattr(acquire_module, "PINNED_ARTIFACTS", (pin,))


class ScriptedDownload:
    """Injected ``http_get``: returns fixed bytes and records every request."""

    def __init__(self, payload: bytes = b"") -> None:
        self.payload = payload
        self.calls: list[tuple[str, int]] = []

    def __call__(self, url: str, limit: int) -> bytes:
        self.calls.append((url, limit))
        return self.payload


class ScriptedAdb:
    """Deterministic ``adb`` boundary for :class:`DeviceProber` (no device)."""

    def __init__(self, responses: dict[str, tuple[int, str]]) -> None:
        self.responses = dict(responses)
        self.commands: list[str] = []

    @staticmethod
    def inner(argv: list[str]) -> str:
        command = argv[-1]
        prefix = 'su -c "'
        if command.startswith(prefix) and command.endswith('"'):
            return command[len(prefix) : -1].replace('\\"', '"')
        return command

    def __call__(self, argv: list[str]) -> CommandResult:
        command = self.inner(argv)
        self.commands.append(command)
        returncode, stdout = self.responses.get(command, (1, ""))
        return CommandResult(argv=argv, returncode=returncode, stdout=stdout)


def _magisk_device() -> ScriptedAdb:
    """A Magisk-rooted AVD with one plain module, answered entirely from memory."""
    responses: dict[str, tuple[int, str]] = {
        "getprop": (0, MAGISK_PROPS),
        "su -c id": (0, "uid=0(root) gid=0(root) context=u:r:magisk:s0\n"),
        "magisk -V": (0, "27000\n"),
        "ls /data/adb/magisk": (0, "magisk32\nmagisk64\n"),
        "magisk --sqlite \"select value from settings where key='zygisk'\"": (0, "value=1\n"),
        "ls /data/adb/modules": (0, "alpha\n"),
        "ls /data/adb/lspd": (0, "config\n"),
        "mount": (0, "/dev/block/by-name/system /system ext4 ro,seclabel 0 0\n"),
        # The Root profile now starts with enable_adb_root.
        "root": (0, "restarting adbd as root\n"),
        "id": (0, "uid=0(root) gid=0(root)\n"),
        "cat /data/adb/modules/alpha/module.prop": (0, ALPHA_PROP),
        "[ -e /data/adb/modules/alpha/disable ] && echo yes": (1, ""),
        "[ -e /data/adb/modules/alpha/remove ] && echo yes": (1, ""),
        "ls /data/adb/modules/alpha/zygisk 2>/dev/null": (1, ""),
    }
    return ScriptedAdb(responses)


class InstrumentationContainer:
    """Duck-typed container exposing only the instrumentation service."""

    def __init__(self, config: Config, paths: Paths, service: InstrumentationService) -> None:
        self.config = config
        self.paths = paths
        self.instrumentation = service

    def close(self) -> None:
        return None


class EmptyContainer:
    """Container without any services (used to prove the 503 fallback)."""

    def __init__(self, config: Config, paths: Paths) -> None:
        self.config = config
        self.paths = paths

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def store() -> Iterator[Store]:
    store = Store(":memory:")
    store.initialize()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture()
def repository(store: Store) -> InstrumentationRepository:
    return InstrumentationRepository(store.database)


@pytest.fixture()
def downloads() -> ScriptedDownload:
    return ScriptedDownload()


@pytest.fixture()
def service(
    repository: InstrumentationRepository, tmp_path: Path, downloads: ScriptedDownload
) -> InstrumentationService:
    running = {"avd-01": True, "avd-02": True, "avd-stopped": False}

    def resolve(name: str) -> TargetDevice:
        if name not in running:
            raise NotFoundError(f"instance {name!r} was not found")
        return TargetDevice(
            instance=name,
            serial=f"emulator-{name}",
            running=running[name],
            boot_completed=running[name],
            image_tag="google_apis",
        )

    ensure_builtin_profiles(repository)
    return InstrumentationService(
        repository,
        ModuleLibrary(tmp_path / "library"),
        DeviceProber(runner=_magisk_device()),
        resolve,
        acquisition=AcquisitionStore(tmp_path / "toolchain", http_get=downloads),
    )


@pytest.fixture()
def client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, service: InstrumentationService
) -> Iterator[TestClient]:
    monkeypatch.setenv("AVDFLEET_TOKEN", TOKEN)
    config = _config()
    paths = _paths(tmp_path)
    container = InstrumentationContainer(config, paths, service)
    app = create_app(config=config, container=container, paths=paths)
    with TestClient(app, base_url=BASE_URL) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def test_instrumentation_routes_require_the_bearer_token(client: TestClient) -> None:
    cases: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", "/api/v1/instrumentation/capabilities?targets=avd-01", None),
        ("POST", "/api/v1/instrumentation/scan", {"targets": ["avd-01"]}),
        ("GET", "/api/v1/instrumentation/modules", None),
        ("GET", "/api/v1/instrumentation/toolchain", None),
        ("POST", "/api/v1/instrumentation/toolchain/lsposed/acquire", {"confirm": False}),
        ("GET", "/api/v1/instrumentation/profiles", None),
        ("GET", "/api/v1/instrumentation/deployments", None),
    ]
    for method, path, body in cases:
        anonymous = client.request(method, path, json=body)
        assert anonymous.status_code == 401, (method, path, anonymous.text)
        assert anonymous.json()["error"]["code"] == "unauthorized"

        wrong_token = client.request(method, path, json=body, headers=auth_headers("not-the-token"))
        assert wrong_token.status_code == 401, (method, path, wrong_token.text)
        assert wrong_token.json()["error"]["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# Capability scanning
# ---------------------------------------------------------------------------
def test_capabilities_and_scan_return_the_probed_devices(client: TestClient) -> None:
    headers = auth_headers()

    response = client.get(
        "/api/v1/instrumentation/capabilities",
        params={"targets": "avd-01,avd-02"},
        headers=headers,
    )
    assert response.status_code == 200
    devices = response.json()["devices"]
    assert [device["instance"] for device in devices] == ["avd-01", "avd-02"]
    assert all(device["root_implementation"] == "magisk" for device in devices)
    assert all(device["capabilities"]["magisk"] is True for device in devices)
    assert all(device["compatible"] is True for device in devices)
    assert devices[0]["capabilities"]["magisk_version"] == "27000"
    assert devices[0]["capabilities"]["zygisk_enabled"] is True
    assert devices[0]["api_level"] == 34
    assert devices[0]["image_tag"] == "google_apis"

    scanned = client.post(
        "/api/v1/instrumentation/scan",
        json={"targets": ["avd-01", "avd-01", "avd-02"]},
        headers=headers,
    )
    assert scanned.status_code == 200
    assert [device["instance"] for device in scanned.json()["devices"]] == ["avd-01", "avd-02"]

    repeated = client.get(
        "/api/v1/instrumentation/capabilities",
        params=[("targets", "avd-01"), ("targets", "ghost")],
        headers=headers,
    )
    assert repeated.status_code == 200
    ghost = repeated.json()["devices"][1]
    assert ghost["instance"] == "ghost"
    assert ghost["compatible"] is False
    assert ghost["blockers"]


# ---------------------------------------------------------------------------
# Module library
# ---------------------------------------------------------------------------
def test_module_import_list_and_delete_round_trip(client: TestClient, tmp_path: Path) -> None:
    artifact = tmp_path / "alpha-module.zip"
    artifact.write_bytes(_magisk_zip())
    headers = auth_headers()

    created = client.post(
        "/api/v1/instrumentation/modules/import",
        json={"path": str(artifact)},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["kind"] == "magisk_zip"
    assert body["name"] == "Alpha"
    assert body["package_name"] == "alpha"
    assert body["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert body["source"] == "upload"
    module_id = body["id"]

    listed = client.get("/api/v1/instrumentation/modules", headers=headers)
    assert listed.status_code == 200
    assert [module["id"] for module in listed.json()["modules"]] == [module_id]

    filtered = client.get(
        "/api/v1/instrumentation/modules", params={"kind": "magisk_zip"}, headers=headers
    )
    assert [module["id"] for module in filtered.json()["modules"]] == [module_id]
    other_kind = client.get(
        "/api/v1/instrumentation/modules", params={"kind": "xposed_apk"}, headers=headers
    )
    assert other_kind.json()["modules"] == []

    deleted = client.delete(f"/api/v1/instrumentation/modules/{module_id}", headers=headers)
    assert deleted.status_code == 204
    assert not Path(body["file_path"]).exists()
    assert client.get("/api/v1/instrumentation/modules", headers=headers).json()["modules"] == []

    missing = client.delete(f"/api/v1/instrumentation/modules/{module_id}", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_module_upload_is_accepted_and_duplicates_conflict(client: TestClient) -> None:
    headers = auth_headers()
    payload = _magisk_zip(id="uploaded", name="Uploaded")

    uploaded = client.post(
        "/api/v1/instrumentation/modules/import",
        files={"file": ("uploaded.zip", payload, "application/zip")},
        headers=headers,
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["kind"] == "magisk_zip"
    assert uploaded.json()["name"] == "Uploaded"

    duplicate = client.post(
        "/api/v1/instrumentation/modules/import",
        files={"file": ("uploaded-again.zip", payload, "application/zip")},
        headers=headers,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "conflict"
    assert len(client.get("/api/v1/instrumentation/modules", headers=headers).json()["modules"]) == 1


# ---------------------------------------------------------------------------
# Toolchain acquisition
# ---------------------------------------------------------------------------
def test_toolchain_status_lists_the_pinned_releases(client: TestClient) -> None:
    response = client.get("/api/v1/instrumentation/toolchain", headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"artifacts", "manifest", "available"}
    assert body["available"] is True
    assert body["manifest"] == []

    artifacts = {artifact["key"]: artifact for artifact in body["artifacts"]}
    assert set(artifacts) == {"magisk", "lsposed"}
    assert (
        artifacts["magisk"]["pinned_sha256"]
        == "ae1a02b1ab608a51d5bc9b323e0588d06d30d9987ac8da01f4710d76f705dccb"
    )
    assert artifacts["magisk"]["pinned_size_bytes"] == 11_411_692
    assert (
        artifacts["lsposed"]["pinned_sha256"]
        == "0ebc6bcb465d1c4b44b7220ab5f0252e6b4eb7fe43da74650476d2798bb29622"
    )
    assert artifacts["lsposed"]["pinned_size_bytes"] == 2_462_055
    assert all(artifact["present"] is False for artifact in artifacts.values())
    assert all(artifact["verified"] is False for artifact in artifacts.values())


def test_toolchain_acquire_requires_confirmation(
    client: TestClient, downloads: ScriptedDownload
) -> None:
    headers = auth_headers()

    refused = client.post(
        "/api/v1/instrumentation/toolchain/lsposed/acquire", json={}, headers=headers
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "conflict"

    # An omitted body defaults to confirm=false as well.
    bare = client.post("/api/v1/instrumentation/toolchain/lsposed/acquire", headers=headers)
    assert bare.status_code == 409
    assert downloads.calls == []


def test_toolchain_acquire_downloads_and_imports_the_module(
    client: TestClient,
    downloads: ScriptedDownload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _lsposed_zip()
    pin = _test_pin(data)
    _install_pin(monkeypatch, pin)
    downloads.payload = data

    acquired = client.post(
        "/api/v1/instrumentation/toolchain/lsposed/acquire",
        json={"confirm": True},
        headers=auth_headers(),
    )
    assert acquired.status_code == 200, acquired.text
    body = acquired.json()
    assert set(body) == {"artifact", "module_id"}
    assert body["module_id"]
    assert body["artifact"]["verified"] is True
    assert body["artifact"]["present"] is True
    assert body["artifact"]["sha256"] == pin.sha256
    assert Path(body["artifact"]["path"]).is_file()

    modules = client.get(
        "/api/v1/instrumentation/modules",
        params={"kind": "lsposed_zip"},
        headers=auth_headers(),
    )
    assert [module["id"] for module in modules.json()["modules"]] == [body["module_id"]]
    assert modules.json()["modules"][0]["source"] == "official-release"
    assert modules.json()["modules"][0]["source_url"] == pin.url

    status = client.get("/api/v1/instrumentation/toolchain", headers=auth_headers()).json()
    assert [artifact["key"] for artifact in status["artifacts"]] == ["lsposed"]
    assert status["artifacts"][0]["present"] is True
    assert status["artifacts"][0]["verified"] is True
    assert len(status["manifest"]) == 1
    assert status["manifest"][0]["licence"] == "GPL-3.0"
    assert status["manifest"][0]["sha256"] == pin.sha256
    assert status["manifest"][0]["source_url"] == pin.url


def test_toolchain_acquire_unknown_key_is_a_404(client: TestClient) -> None:
    response = client.post(
        "/api/v1/instrumentation/toolchain/not-a-tool/acquire",
        json={"confirm": True},
        headers=auth_headers(),
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
def test_profiles_are_seeded_and_can_be_created_updated_and_deleted(client: TestClient) -> None:
    headers = auth_headers()

    listed = client.get("/api/v1/instrumentation/profiles", headers=headers)
    assert listed.status_code == 200
    profiles = listed.json()["profiles"]
    assert len(profiles) == 5
    assert {profile["name"] for profile in profiles} == BUILTIN_NAMES
    assert all(profile["builtin"] is True for profile in profiles)
    assert all(profile["steps"] for profile in profiles)

    by_name = {profile["name"]: profile for profile in profiles}
    assert [step["action"] for step in by_name["Root"]["steps"]] == [
        "enable_adb_root",
        "verify_root",
    ]
    assert [step["action"] for step in by_name["Magisk + Zygisk"]["steps"]] == [
        "install_magisk",
        "complete_magisk_setup",
        "enable_zygisk",
        "reboot",
        "verify",
    ]
    assert [step["action"] for step in by_name["LSPosed"]["steps"]] == [
        "install_magisk",
        "complete_magisk_setup",
        "enable_zygisk",
        "install_lsposed",
        "reboot",
        "install_lsposed_manager",
        "verify",
    ]
    assert [step["action"] for step in by_name["Xposed Instrumentation"]["steps"]] == [
        "install_magisk",
        "complete_magisk_setup",
        "enable_zygisk",
        "install_lsposed",
        "reboot",
        "install_lsposed_manager",
        "install_module",
        "verify",
    ]

    created = client.post(
        "/api/v1/instrumentation/profiles",
        json={
            "name": "My fleet profile",
            "description": "created by the API test",
            "steps": [{"action": "verify_root"}],
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["revision"] == 1
    assert body["builtin"] is False
    assert [step["action"] for step in body["steps"]] == ["verify_root"]
    profile_id = body["id"]

    updated = client.put(
        f"/api/v1/instrumentation/profiles/{profile_id}",
        json={
            "name": "My fleet profile v2",
            "description": "edited",
            "steps": [
                {"action": "verify_root"},
                {"action": "verify", "params": {"expect": {"magisk": True}}},
            ],
        },
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 2
    assert updated.json()["name"] == "My fleet profile v2"
    assert len(updated.json()["steps"]) == 2
    assert updated.json()["created_at"] == body["created_at"]

    after_update = client.get("/api/v1/instrumentation/profiles", headers=headers).json()["profiles"]
    assert [profile["revision"] for profile in after_update if profile["id"] == profile_id] == [2]

    deleted = client.delete(f"/api/v1/instrumentation/profiles/{profile_id}", headers=headers)
    assert deleted.status_code == 204
    remaining = client.get("/api/v1/instrumentation/profiles", headers=headers).json()["profiles"]
    assert profile_id not in {profile["id"] for profile in remaining}
    assert len(remaining) == 5
    assert client.delete(f"/api/v1/instrumentation/profiles/{profile_id}", headers=headers).status_code == 404


def test_builtin_profiles_are_read_only(client: TestClient) -> None:
    headers = auth_headers()

    update = client.put(
        "/api/v1/instrumentation/profiles/prof_builtin_root",
        json={"name": "Hijacked", "steps": [{"action": "verify_root"}]},
        headers=headers,
    )
    assert 400 <= update.status_code < 500, update.text
    assert update.json()["error"]["code"] in {"invalid_request", "conflict"}

    delete = client.delete("/api/v1/instrumentation/profiles/prof_builtin_root", headers=headers)
    assert delete.status_code == 409
    assert delete.json()["error"]["code"] == "conflict"

    still_there = client.get("/api/v1/instrumentation/profiles", headers=headers).json()["profiles"]
    assert "prof_builtin_root" in {profile["id"] for profile in still_there}
    assert all(profile["name"] != "Hijacked" for profile in still_there)


def test_builtin_profile_can_be_cloned_into_an_editable_copy(client: TestClient) -> None:
    headers = auth_headers()
    profiles = client.get("/api/v1/instrumentation/profiles", headers=headers).json()["profiles"]
    source = next(profile for profile in profiles if profile["id"] == "prof_builtin_lsposed")

    cloned = client.post(
        "/api/v1/instrumentation/profiles/prof_builtin_lsposed/clone",
        json={"name": "My LSPosed flow"},
        headers=headers,
    )
    assert cloned.status_code == 200, cloned.text
    body = cloned.json()
    assert body["name"] == "My LSPosed flow"
    assert body["builtin"] is False
    assert body["revision"] == 1
    assert body["id"] != source["id"]
    assert [step["action"] for step in body["steps"]] == [step["action"] for step in source["steps"]]

    edited = client.put(
        f"/api/v1/instrumentation/profiles/{body['id']}",
        json={"name": "My LSPosed flow v2", "steps": [{"action": "verify_root"}]},
        headers=headers,
    )
    assert edited.status_code == 200
    assert edited.json()["revision"] == 2

    default_name = client.post(
        "/api/v1/instrumentation/profiles/prof_builtin_custom/clone", headers=headers
    )
    assert default_name.status_code == 200
    assert default_name.json()["name"] == "Custom (copy)"

    for profile_id in (body["id"], default_name.json()["id"]):
        assert client.delete(f"/api/v1/instrumentation/profiles/{profile_id}", headers=headers).status_code == 204
    assert client.delete("/api/v1/instrumentation/profiles/prof_builtin_lsposed", headers=headers).status_code == 409

    missing = client.post("/api/v1/instrumentation/profiles/prof_missing/clone", headers=headers)
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# Deployments
# ---------------------------------------------------------------------------
def test_deployment_runs_synchronously_and_exposes_status_and_logs(client: TestClient) -> None:
    headers = auth_headers()

    created = client.post(
        "/api/v1/instrumentation/deployments",
        json={
            "profile_id": "prof_builtin_root",
            "targets": ["avd-01", "avd-02"],
            "background": False,
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    run = created.json()
    assert run["state"] == "succeeded"
    assert run["profile_id"] == "prof_builtin_root"
    assert run["targets"] == ["avd-01", "avd-02"]
    assert run["label"] == "Root -> 2 AVD(s)"
    assert run["started_at"] and run["finished_at"]
    assert [result["instance"] for result in run["results"]] == ["avd-01", "avd-02"]
    assert all(result["ok"] is True and result["skipped"] is False for result in run["results"])
    for result in run["results"]:
        assert [step["action"] for step in result["steps"]] == ["enable_adb_root", "verify_root"]
        assert all(step["ok"] is True for step in result["steps"])
        assert result["steps"][0]["state"] == "enabled"
        assert result["steps"][0]["data"]["adb_root"] is True
    run_id = run["id"]

    fetched = client.get(f"/api/v1/instrumentation/deployments/{run_id}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["state"] == "succeeded"
    assert [result["instance"] for result in fetched.json()["results"]] == ["avd-01", "avd-02"]

    logs = client.get(f"/api/v1/instrumentation/deployments/{run_id}/logs", headers=headers)
    assert logs.status_code == 200
    entries = logs.json()["logs"]
    assert entries
    assert any("deploying profile" in entry["message"] for entry in entries)
    assert any("run finished" in entry["message"] for entry in entries)
    assert all(entry["run_id"] == run_id for entry in entries)
    assert [entry["id"] for entry in entries] == sorted(entry["id"] for entry in entries)

    listed = client.get("/api/v1/instrumentation/deployments", headers=headers)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["runs"]] == [run_id]

    first_cancel = client.post(f"/api/v1/instrumentation/deployments/{run_id}/cancel", headers=headers)
    second_cancel = client.post(f"/api/v1/instrumentation/deployments/{run_id}/cancel", headers=headers)
    assert first_cancel.status_code == 200 and second_cancel.status_code == 200
    assert first_cancel.json()["state"] == "succeeded"
    assert second_cancel.json()["state"] == "succeeded"
    assert second_cancel.json()["finished_at"] == first_cancel.json()["finished_at"]


def test_deployment_records_a_skipped_target_without_failing_the_others(client: TestClient) -> None:
    headers = auth_headers()

    created = client.post(
        "/api/v1/instrumentation/deployments",
        json={
            "profile_id": "prof_builtin_root",
            "targets": ["avd-01", "avd-stopped"],
            "background": False,
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    run = created.json()
    assert run["state"] == "partial"
    by_target = {result["instance"]: result for result in run["results"]}
    assert by_target["avd-01"]["ok"] is True
    assert by_target["avd-stopped"]["ok"] is False
    assert by_target["avd-stopped"]["skipped"] is True
    assert "not running" in by_target["avd-stopped"]["error"]

    entries = client.get(
        f"/api/v1/instrumentation/deployments/{run['id']}/logs", headers=headers
    ).json()["logs"]
    assert any(entry["level"] == "warn" and "not running" in entry["message"] for entry in entries)


def test_unknown_deployment_is_a_404_contract_error(client: TestClient) -> None:
    headers = auth_headers()

    for path in (
        "/api/v1/instrumentation/deployments/irun_missing",
        "/api/v1/instrumentation/deployments/irun_missing/logs",
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 404, (path, response.text)
        assert response.json()["error"]["code"] == "not_found"

    cancel = client.post("/api/v1/instrumentation/deployments/irun_missing/cancel", headers=headers)
    assert cancel.status_code == 404


# ---------------------------------------------------------------------------
# Bad input
# ---------------------------------------------------------------------------
def test_bad_input_is_rejected_with_the_contract_envelope(client: TestClient) -> None:
    headers = auth_headers()

    unknown_action = client.post(
        "/api/v1/instrumentation/profiles",
        json={"name": "bad", "steps": [{"action": "make_it_root"}]},
        headers=headers,
    )
    assert unknown_action.status_code == 422
    assert unknown_action.json()["error"]["code"] == "invalid_request"

    empty_targets = client.post(
        "/api/v1/instrumentation/deployments",
        json={"profile_id": "prof_builtin_root", "targets": []},
        headers=headers,
    )
    assert empty_targets.status_code == 422
    assert empty_targets.json()["error"]["code"] == "invalid_request"

    no_targets = client.get("/api/v1/instrumentation/capabilities", headers=headers)
    assert no_targets.status_code == 422
    assert no_targets.json()["error"]["code"] == "invalid_request"

    empty_scan = client.post(
        "/api/v1/instrumentation/scan", json={"targets": []}, headers=headers
    )
    assert empty_scan.status_code == 422
    assert empty_scan.json()["error"]["code"] == "invalid_request"

    unknown_kind = client.get(
        "/api/v1/instrumentation/modules", params={"kind": "banana"}, headers=headers
    )
    assert unknown_kind.status_code == 422
    assert unknown_kind.json()["error"]["code"] == "invalid_request"
    assert "unknown module kind" in unknown_kind.json()["error"]["message"]

    nothing_to_import = client.post(
        "/api/v1/instrumentation/modules/import", json={}, headers=headers
    )
    assert nothing_to_import.status_code == 422
    assert nothing_to_import.json()["error"]["code"] == "invalid_request"

    unknown_profile = client.post(
        "/api/v1/instrumentation/deployments",
        json={"profile_id": "prof_missing", "targets": ["avd-01"], "background": False},
        headers=headers,
    )
    assert unknown_profile.status_code == 404
    assert unknown_profile.json()["error"]["code"] == "not_found"


def test_missing_instrumentation_service_returns_503(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AVDFLEET_TOKEN", TOKEN)
    config = _config()
    paths = _paths(tmp_path)
    app = create_app(config=config, container=EmptyContainer(config, paths), paths=paths)

    with TestClient(app, base_url=BASE_URL) as client:
        response = client.get("/api/v1/instrumentation/profiles", headers=auth_headers())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
