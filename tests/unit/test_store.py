"""Unit tests for the AVD Fleet SQLite store.

Covers schema/migrations, every repository, secret handling (proxy passwords are
never stored), WAL/health/checkpoint, ``:memory:`` support, restart persistence,
foreign-key cascades and concurrent writes from many threads.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from avdfleet.core.errors import ConflictError, NotFoundError, ValidationError
from avdfleet.core.models import (
    AvdInfo,
    GpuMode,
    ImageKind,
    InstancePorts,
    InstanceResult,
    InstanceState,
    OobeRecord,
    OobeStatus,
    ProxyConfig,
    ProxyHealth,
    ProxyHealthStatus,
    ProxyScheme,
    RunState,
    ScriptDefinition,
    ScriptRun,
    ScriptType,
    SystemImage,
    Template,
    utcnow,
)
from avdfleet.store import (
    MIGRATIONS,
    SCHEMA_VERSION,
    ProxyRecord,
    Store,
    StoreClosedError,
    StoreNotInitializedError,
    StoreSchemaError,
    decode_datetime,
    encode_datetime,
)
from avdfleet.store.migrations import SCHEMA_VERSION_TABLE

# ---------------------------------------------------------------------------
# Fixtures and factories
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[Store]:
    """A migrated on-disk store with a short timeout (fast lock failures)."""
    instance = Store(tmp_path / "avdfleet.db", timeout=10)
    instance.initialize()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture()
def memory_store() -> Iterator[Store]:
    instance = Store(":memory:", timeout=10)
    instance.initialize()
    try:
        yield instance
    finally:
        instance.close()


def make_template(template_id: str = "tpl-1", **overrides: object) -> Template:
    data: dict[str, object] = {
        "id": template_id,
        "name": f"Template {template_id}",
        "image_id": "system-images;android-35;google_apis;x86_64",
        "device_id": "pixel_7",
        "ram_mb": 4096,
        "cpu_cores": 4,
        "gpu_mode": GpuMode.SWIFTSHADER_INDIRECT,
        "headless": True,
        "scripts": ["script-1"],
        "tags": ["prod", "api35"],
        "description": "test template",
        "config": {"hw.keyboard": "yes", "disk.dataPartition.size": "4G"},
    }
    data.update(overrides)
    return Template.model_validate(data)


def make_proxy(proxy_id: str = "proxy-1", **overrides: object) -> ProxyRecord:
    data: dict[str, object] = {
        "id": proxy_id,
        "name": f"Proxy {proxy_id}",
        "scheme": ProxyScheme.HTTP,
        "host": "127.0.0.1",
        "port": 8080,
        "username": "alice",
        "secret_ref": f"vault:proxies/{proxy_id}",
        "tags": ["pool-a"],
        "enabled": True,
        "source": "paste",
        "health": ProxyHealth(status=ProxyHealthStatus.UNKNOWN),
    }
    data.update(overrides)
    return ProxyRecord.model_validate(data)


def make_script(script_id: str = "script-1", **overrides: object) -> ScriptDefinition:
    data: dict[str, object] = {
        "id": script_id,
        "name": f"Script {script_id}",
        "type": ScriptType.ADB_SHELL,
        "content": "adb shell getprop sys.boot_completed",
        "description": "boot check",
        "timeout_s": 30,
        "env": {"TOKEN": "not-a-real-secret"},
        "targets": ["avd-01"],
        "builtin": False,
        "tags": ["smoke"],
    }
    data.update(overrides)
    return ScriptDefinition.model_validate(data)


def make_image(
    image_id: str = "system-images;android-35;google_apis;x86_64", **overrides: object
) -> SystemImage:
    data: dict[str, object] = {
        "id": image_id,
        "api_level": 35,
        "tag": "google_apis",
        "abi": "x86_64",
        "kind": ImageKind.GOOGLE_APIS,
        "display_name": "Google APIs API 35",
        "package": "system-images;android-35;google_apis;x86_64",
        "sha256": "0" * 64,
        "size_bytes": 1_500_000_000,
        "installed": True,
        "oobe_status": OobeStatus.UNVERIFIED,
        "metadata": {"channel": "stable"},
    }
    data.update(overrides)
    return SystemImage.model_validate(data)


# ---------------------------------------------------------------------------
# Lifecycle, schema, migrations, health
# ---------------------------------------------------------------------------


def test_initialize_creates_schema_in_wal_mode(store: Store) -> None:
    health = store.health()
    assert health["ok"] is True
    assert health["schema_version"] == SCHEMA_VERSION
    assert health["schema_current"] is True
    assert health["journal_mode"].lower() == "wal"
    assert health["foreign_keys"] is True
    assert int(health["busy_timeout_ms"]) > 0
    assert health["integrity"] == "ok"
    assert health["tables"] == 10
    assert store.schema_version == SCHEMA_VERSION
    assert store.db_path == Path(store.db_path)


def test_initialize_is_idempotent_and_records_migrations(store: Store) -> None:
    store.initialize()
    store.initialize()
    ledger = store.applied_migrations()
    assert [version for version, _, _ in ledger] == [migration.version for migration in MIGRATIONS]
    assert [name for _, name, _ in ledger] == [migration.name for migration in MIGRATIONS]
    assert all(applied_at for _, _, applied_at in ledger)


def test_store_requires_explicit_lifecycle(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.db"
    instance = Store(path)
    assert instance.health()["ok"] is False
    assert instance.health()["initialized"] is False
    assert instance.schema_version == 0
    with pytest.raises(StoreNotInitializedError):
        instance.templates.list()
    with pytest.raises(StoreNotInitializedError):
        instance.checkpoint()

    instance.initialize()
    instance.templates.upsert(make_template())
    assert instance.health()["ok"] is True
    assert instance.schema_version == SCHEMA_VERSION

    instance.close()
    assert instance.initialized is False
    assert instance.schema_version == 0
    assert instance.health()["closed"] is True
    with pytest.raises(StoreClosedError):
        instance.templates.list()
    instance.close()  # idempotent


def test_store_context_manager(tmp_path: Path) -> None:
    with Store(tmp_path / "context.db") as instance:
        instance.templates.upsert(make_template())
        assert instance.health()["ok"] is True
    assert instance.initialized is False
    with pytest.raises(StoreClosedError):
        instance.templates.list()


def test_migrations_upgrade_a_v1_database(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute(SCHEMA_VERSION_TABLE)
        for statement in MIGRATIONS[0].statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
            (MIGRATIONS[0].version, MIGRATIONS[0].name, "2026-01-01T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("legacy", '"kept"', "2026-01-01T00:00:00+00:00"),
        )
        connection.commit()
    finally:
        connection.close()

    instance = Store(path)
    instance.initialize()
    try:
        assert instance.schema_version == SCHEMA_VERSION
        assert instance.settings.get("legacy") == "kept"
        assert instance.runs.list() == []
        assert instance.oobe.list() == []
        assert [version for version, _, _ in instance.applied_migrations()] == [1, 2]
    finally:
        instance.close()


def test_newer_schema_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute(SCHEMA_VERSION_TABLE)
        connection.execute(
            "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
            (SCHEMA_VERSION + 1, "future", "2030-01-01T00:00:00+00:00"),
        )
        connection.commit()
    finally:
        connection.close()

    instance = Store(path)
    with pytest.raises(StoreSchemaError):
        instance.initialize()
    instance.close()


def test_checkpoint_reports_wal_state(store: Store) -> None:
    store.templates.upsert(make_template())
    result = store.checkpoint()
    assert result["ok"] is True
    assert result["mode"] == "wal"
    assert result["busy"] == 0
    assert store.health()["integrity"] == "ok"


def test_wal_mode_persists_across_restart_and_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "wal.db"
    instance = Store(path)
    instance.initialize()
    instance.templates.upsert(make_template())
    instance.close()
    assert path.exists()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_concurrent_initialize_is_safe(tmp_path: Path) -> None:
    instance = Store(tmp_path / "concurrent-init.db", timeout=10)
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def initializer() -> None:
        try:
            barrier.wait(timeout=10)
            instance.initialize()
        except BaseException as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)

    threads = [threading.Thread(target=initializer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    try:
        assert errors == []
        assert instance.initialized is True
        assert instance.schema_version == SCHEMA_VERSION
        assert len(instance.applied_migrations()) == len(MIGRATIONS)
    finally:
        instance.close()


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def test_template_crud(store: Store) -> None:
    created = store.templates.upsert(make_template())
    assert created.id == "tpl-1"
    assert created.created_at is not None
    assert created.updated_at is not None
    assert created.scripts == ["script-1"]
    assert created.config["hw.keyboard"] == "yes"
    assert created.gpu_mode is GpuMode.SWIFTSHADER_INDIRECT

    fetched = store.templates.get("tpl-1")
    assert fetched.model_dump() == created.model_dump()
    by_name = store.templates.get_by_name("Template tpl-1")
    assert by_name is not None and by_name.id == "tpl-1"
    assert store.templates.get_by_name("missing") is None
    assert [template.id for template in store.templates.list()] == ["tpl-1"]

    updated = store.templates.upsert(
        created.model_copy(update={"name": "Renamed", "ram_mb": 8192, "tags": ["staging"]})
    )
    assert updated.name == "Renamed"
    assert updated.ram_mb == 8192
    assert updated.tags == ["staging"]
    assert updated.created_at == created.created_at
    assert updated.updated_at is not None and updated.updated_at >= created.updated_at

    store.templates.delete("tpl-1")
    with pytest.raises(NotFoundError):
        store.templates.get("tpl-1")
    assert store.templates.list() == []
    with pytest.raises(NotFoundError):
        store.templates.delete("tpl-1")


def test_template_validation(store: Store) -> None:
    with pytest.raises(ValidationError):
        store.templates.upsert(make_template().model_copy(update={"id": " "}))
    with pytest.raises(ValidationError):
        store.templates.upsert(make_template().model_copy(update={"name": ""}))
    with pytest.raises(NotFoundError):
        store.templates.get("nope")


# ---------------------------------------------------------------------------
# Instances
# ---------------------------------------------------------------------------


def test_instance_crud_and_rename_moves_assignment(store: Store) -> None:
    proxy = store.proxies.upsert(make_proxy())
    store.assignments.assign("avd-01", proxy.id)

    instance = AvdInfo(
        name="avd-01",
        template_id="tpl-1",
        image_id="img-1",
        image_name="API 35",
        device_id="pixel_7",
        state=InstanceState.STARTING,
        ports=InstancePorts(console=5554, adb=5555, grpc=8554),
        proxy_id=proxy.id,
        adb_serial="emulator-5554",
        config={"hw.ramSize": "4096"},
    )
    created = store.instances.upsert(instance)
    assert created.created_at is not None
    assert created.ports is not None and created.ports.adb == 5555
    assert created.state is InstanceState.STARTING
    assert store.instances.get("avd-01").state is InstanceState.STARTING
    assert [item.name for item in store.instances.list()] == ["avd-01"]

    running = store.instances.upsert(
        created.model_copy(
            update={
                "state": InstanceState.RUNNING,
                "boot_completed": True,
                "uptime_seconds": 12.5,
                "screen_available": True,
            }
        )
    )
    assert running.state is InstanceState.RUNNING
    assert running.boot_completed is True
    assert running.screen_available is True
    assert running.created_at == created.created_at

    renamed = store.instances.rename("avd-01", "avd-02")
    assert renamed.name == "avd-02"
    assert store.instances.get("avd-02").name == "avd-02"
    with pytest.raises(NotFoundError):
        store.instances.get("avd-01")
    assignment = store.assignments.get("avd-02")
    assert assignment is not None and assignment.proxy_id == proxy.id
    assert store.assignments.get("avd-01") is None

    store.instances.upsert(AvdInfo(name="avd-03", state=InstanceState.DEFINED))
    with pytest.raises(ConflictError):
        store.instances.rename("avd-02", "avd-03")

    store.instances.delete("avd-03")
    with pytest.raises(NotFoundError):
        store.instances.get("avd-03")
    with pytest.raises(NotFoundError):
        store.instances.delete("avd-03")


def test_instance_rename_without_assignment(store: Store) -> None:
    store.instances.upsert(AvdInfo(name="solo"))
    renamed = store.instances.rename("solo", "solo-renamed")
    assert renamed.name == "solo-renamed"
    assert store.assignments.list() == []
    with pytest.raises(NotFoundError):
        store.instances.rename("missing", "other")


# ---------------------------------------------------------------------------
# Proxies and assignments
# ---------------------------------------------------------------------------


def test_proxy_crud_and_health(store: Store) -> None:
    first = store.proxies.upsert(make_proxy("proxy-a", enabled=False))
    assert first.secret_ref == "vault:proxies/proxy-a"
    assert first.has_credentials is True
    assert first.safe_url == "http://alice:***@127.0.0.1:8080"
    assert first.created_at is not None
    store.proxies.upsert(make_proxy("proxy-b"))

    assert [proxy.id for proxy in store.proxies.list()] == ["proxy-b"]
    assert {proxy.id for proxy in store.proxies.list(include_disabled=True)} == {"proxy-a", "proxy-b"}
    assert store.proxies.get("proxy-a").enabled is False
    assert store.proxies.get_by_name("Proxy proxy-a") is not None
    assert store.proxies.get_by_name("missing") is None

    checked_at = utcnow()
    health = ProxyHealth(
        status=ProxyHealthStatus.OK,
        latency_ms=42.5,
        checked_at=checked_at,
        egress_ip="203.0.113.7",
    )
    updated = store.proxies.update_health("proxy-a", health)
    assert updated.health.status is ProxyHealthStatus.OK
    assert updated.health.latency_ms == 42.5
    assert updated.health.checked_at == checked_at
    assert updated.health.egress_ip == "203.0.113.7"

    with pytest.raises(NotFoundError):
        store.proxies.update_health("missing", health)
    with pytest.raises(NotFoundError):
        store.proxies.get("missing")
    store.proxies.delete("proxy-a")
    with pytest.raises(NotFoundError):
        store.proxies.delete("proxy-a")


def test_assignment_crud_and_foreign_key_cascade(store: Store) -> None:
    proxy_a = store.proxies.upsert(make_proxy("proxy-a"))
    proxy_b = store.proxies.upsert(make_proxy("proxy-b"))

    assignment = store.assignments.assign("avd-01", proxy_a.id)
    assert assignment.avd_name == "avd-01"
    assert assignment.proxy_id == proxy_a.id
    assert assignment.assigned_at is not None
    assert assignment.assigned_at.utcoffset() is not None
    assert store.assignments.get("avd-01") is not None
    assert store.assignments.get("missing") is None

    reassigned = store.assignments.assign("avd-01", proxy_b.id)
    assert reassigned.proxy_id == proxy_b.id
    assert len(store.assignments.list()) == 1

    with pytest.raises(NotFoundError):
        store.assignments.assign("avd-01", "missing-proxy")

    assert store.assignments.unassign("avd-01") is True
    assert store.assignments.unassign("avd-01") is False
    assert store.assignments.get("avd-01") is None

    store.assignments.assign("avd-01", proxy_a.id)
    store.assignments.assign("avd-02", proxy_a.id)
    assert len(store.assignments.list()) == 2
    # ON DELETE CASCADE removes every assignment that referenced the proxy.
    store.proxies.delete(proxy_a.id)
    assert store.assignments.list() == []
    assert store.assignments.get("avd-01") is None
    assert store.proxies.get(proxy_b.id).id == proxy_b.id


def test_proxies_never_store_plaintext_passwords(tmp_path: Path) -> None:
    path = tmp_path / "secrets.db"
    instance = Store(path)
    instance.initialize()
    config = ProxyConfig(
        id="proxy-secret",
        name="Secure proxy",
        scheme=ProxyScheme.SOCKS5,
        host="10.0.0.5",
        port=1080,
        username="alice",
        password="hunter2-super-secret",
    )
    try:
        # A plaintext password on the core model must never reach SQLite.
        with pytest.raises(ValidationError) as caught:
            instance.proxies.upsert(config)
        assert "hunter2" not in str(caught.value)
        with pytest.raises(ValidationError):
            ProxyRecord.from_config(config)

        record = ProxyRecord.from_config(config, secret_ref="vault:proxy:proxy-secret")
        assert record.secret_ref == "vault:proxy:proxy-secret"
        stored = instance.proxies.upsert(record)
        assert stored.secret_ref == "vault:proxy:proxy-secret"
        assert stored.has_credentials is True
        assert "hunter2" not in stored.safe_url
        assert "hunter2" not in repr(stored)
        assert "password" not in type(stored).model_fields
        assert stored.to_config().password is None

        instance.checkpoint()
        connection = sqlite3.connect(path)
        try:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(proxies)")]
            assert "password" not in columns
            raw_rows = connection.execute("SELECT * FROM proxies").fetchall()
            assert len(raw_rows) == 1
        finally:
            connection.close()
    finally:
        instance.close()

    payload = path.read_bytes()
    wal_path = path.with_name(f"{path.name}-wal")
    if wal_path.exists():
        payload += wal_path.read_bytes()
    assert b"hunter2" not in payload


def test_proxy_upsert_accepts_core_config_without_password(store: Store) -> None:
    config = ProxyConfig(
        id="proxy-config",
        name="From config",
        scheme=ProxyScheme.HTTPS,
        host="proxy.example.com",
        port=8443,
        username="carol",
    )
    stored = store.proxies.upsert(config)
    assert stored.id == "proxy-config"
    assert stored.scheme is ProxyScheme.HTTPS
    assert stored.secret_ref is None
    assert stored.has_credentials is True
    with pytest.raises(ValidationError):
        store.proxies.upsert("not-a-proxy")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Scripts and runs
# ---------------------------------------------------------------------------


def test_script_crud(store: Store) -> None:
    created = store.scripts.upsert(make_script())
    assert created.created_at is not None
    assert created.env == {"TOKEN": "not-a-real-secret"}
    assert created.targets == ["avd-01"]
    assert created.type is ScriptType.ADB_SHELL
    fetched = store.scripts.get("script-1")
    assert fetched.model_dump() == created.model_dump()
    assert store.scripts.get_by_name("Script script-1") is not None
    assert store.scripts.get_by_name("missing") is None
    assert [script.id for script in store.scripts.list()] == ["script-1"]

    updated = store.scripts.upsert(
        created.model_copy(update={"content": "adb shell id", "timeout_s": 60, "tags": ["smoke", "id"]})
    )
    assert updated.content == "adb shell id"
    assert updated.timeout_s == 60
    assert updated.created_at == created.created_at

    store.scripts.delete("script-1")
    assert store.scripts.list() == []
    with pytest.raises(NotFoundError):
        store.scripts.get("script-1")
    with pytest.raises(NotFoundError):
        store.scripts.delete("script-1")


def test_run_results_roundtrip_and_finish(store: Store) -> None:
    started = utcnow()
    results = [
        InstanceResult(
            instance="avd-01",
            state=RunState.SUCCEEDED,
            exit_code=0,
            stdout="héllo ✓",
            started_at=started,
            duration_s=1.25,
        ),
        InstanceResult(
            instance="avd-02",
            state=RunState.FAILED,
            exit_code=1,
            stderr="boom",
            error="command failed",
        ),
    ]
    run = ScriptRun(
        id="run-1",
        script_id="script-1",
        script_name="Script script-1",
        type=ScriptType.ADB_SHELL,
        state=RunState.RUNNING,
        instances=["avd-01", "avd-02"],
        results=results,
        started_at=started,
        requested_by="tester",
        request={"concurrency": 2, "started": started},
    )
    stored = store.runs.upsert(run)
    assert stored.results == results
    assert stored.started_at == started
    assert stored.instances == ["avd-01", "avd-02"]
    assert stored.request["concurrency"] == 2
    assert len(store.runs.list()) == 1
    assert store.runs.list(limit=1)[0].id == "run-1"
    assert store.runs.list(limit=0) == []

    finished_at = utcnow()
    finished = store.runs.finish(
        "run-1",
        state=RunState.FAILED,
        results=results,
        error="1 of 2 failed",
        finished_at=finished_at,
    )
    assert finished.state is RunState.FAILED
    assert finished.error == "1 of 2 failed"
    assert finished.finished_at == finished_at
    assert finished.results == results

    # A live ScriptRun can be handed to finish(); explicit kwargs win.
    live = store.runs.upsert(ScriptRun(id="run-2", script_id="script-1", state=RunState.RUNNING))
    done = store.runs.finish(live.model_copy(update={"state": RunState.SUCCEEDED}))
    assert done.state is RunState.SUCCEEDED
    assert done.finished_at is not None

    with pytest.raises(NotFoundError):
        store.runs.get("missing")
    with pytest.raises(NotFoundError):
        store.runs.finish("missing", state=RunState.SUCCEEDED)
    with pytest.raises(ValidationError):
        store.runs.list(limit=-1)


def test_runs_are_listed_newest_first(store: Store) -> None:
    first = store.runs.upsert(ScriptRun(id="run-a", script_id="s1", state=RunState.QUEUED))
    store.runs.upsert(ScriptRun(id="run-b", script_id="s1", state=RunState.QUEUED))
    store.runs.upsert(first.model_copy(update={"state": RunState.RUNNING}))
    ordered = [run.id for run in store.runs.list()]
    assert ordered[0] == "run-a"
    assert set(ordered) == {"run-a", "run-b"}


# ---------------------------------------------------------------------------
# Images, OOBE, settings
# ---------------------------------------------------------------------------


def test_image_and_oobe_records(store: Store) -> None:
    image = make_image(metadata={"channel": "stable", "checked_at": utcnow()})
    created = store.images.upsert(image)
    assert created.id == image.id
    assert created.metadata["channel"] == "stable"
    assert isinstance(created.metadata["checked_at"], str)  # JSON datetimes are safe strings
    assert created.installed is True
    assert store.images.get(image.id).display_name == "Google APIs API 35"
    assert [item.id for item in store.images.list()] == [image.id]

    checked_at = utcnow()
    record = OobeRecord(
        image_id=image.id,
        api_level=35,
        tag="google_apis",
        abi="x86_64",
        status=OobeStatus.VERIFIED,
        activity="com.google.android.setupwizard/.SetupWizardExitActivity",
        first_run_component="com.google.android.setupwizard",
        screenshot_path="screenshots/oobe.png",
        boot_seconds=71.5,
        reset_seconds=18.25,
        checked_at=checked_at,
        checked_by="oobe-agent",
        emulator_version="36.2.0",
        notes="first boot reached setup wizard",
        evidence=["screenshots/oobe.png", "logs/boot.txt"],
    )
    stored_record = store.oobe.upsert(record)
    assert stored_record.checked_at == checked_at
    assert stored_record.evidence == record.evidence
    assert store.oobe.get(image.id) is not None
    assert store.oobe.get("unknown-image") is None
    assert [item.image_id for item in store.oobe.list()] == [image.id]
    assert store.oobe.get(image.id).status is OobeStatus.VERIFIED

    updated = store.oobe.upsert(record.model_copy(update={"status": OobeStatus.FAILED, "notes": "regressed"}))
    assert updated.status is OobeStatus.FAILED
    assert len(store.oobe.list()) == 1

    store.images.delete(image.id)
    with pytest.raises(NotFoundError):
        store.images.get(image.id)
    with pytest.raises(NotFoundError):
        store.images.delete(image.id)
    # OOBE evidence is deliberately independent of catalogue membership.
    assert store.oobe.get(image.id) is not None


def test_image_upsert_replaces_catalogue_fields(store: Store) -> None:
    image = make_image()
    store.images.upsert(image)
    changed = store.images.upsert(
        image.model_copy(update={"installed": False, "oobe_status": OobeStatus.VERIFIED, "notes": "verified"})
    )
    assert changed.installed is False
    assert changed.oobe_status is OobeStatus.VERIFIED
    assert changed.notes == "verified"
    assert len(store.images.list()) == 1


def test_settings_json_roundtrip(store: Store) -> None:
    assert store.settings.get("missing") is None
    assert store.settings.get("missing", 7) == 7

    store.settings.set("theme", "dark")
    store.settings.set("capacity", {"max_instances": 64, "reserved_mb": 2048})
    store.settings.set("tags", ["prod", "e2e"])
    store.settings.set("enabled", True)
    store.settings.set("count", 3)
    store.settings.set("nothing", None)

    assert store.settings.get("theme") == "dark"
    assert store.settings.get("capacity") == {"max_instances": 64, "reserved_mb": 2048}
    assert store.settings.get("tags") == ["prod", "e2e"]
    assert store.settings.get("enabled") is True
    assert store.settings.get("count") == 3
    assert store.settings.get("nothing") is None
    assert store.settings.all() == {
        "capacity": {"max_instances": 64, "reserved_mb": 2048},
        "count": 3,
        "enabled": True,
        "nothing": None,
        "tags": ["prod", "e2e"],
        "theme": "dark",
    }

    store.settings.set("theme", "light")
    assert store.settings.get("theme") == "light"
    assert store.settings.delete("theme") is True
    assert store.settings.delete("theme") is False
    assert store.settings.get("theme") is None

    with pytest.raises(ValidationError):
        store.settings.set("bad", object())
    with pytest.raises(ValidationError):
        store.settings.set(" ", "x")


def test_datetime_codec_normalises_to_utc() -> None:
    naive = datetime(2026, 1, 2, 3, 4, 5)
    encoded = encode_datetime(naive)
    assert encoded is not None and encoded.endswith("+00:00")
    decoded = decode_datetime(encoded)
    assert decoded is not None
    assert decoded.tzinfo is not None
    assert decoded.utcoffset() == UTC.utcoffset(None)
    assert decode_datetime("2026-01-02T03:04:05Z") == decoded
    assert decode_datetime("2026-01-02T04:04:05+01:00") == decoded
    assert decode_datetime(None) is None
    assert encode_datetime(None) is None
    with pytest.raises(ValueError):
        decode_datetime("not-a-timestamp")


# ---------------------------------------------------------------------------
# Persistence and concurrency
# ---------------------------------------------------------------------------


def test_restart_persistence(tmp_path: Path) -> None:
    path = tmp_path / "persist.db"

    first = Store(path)
    first.initialize()
    first.templates.upsert(make_template())
    first.proxies.upsert(make_proxy())
    first.assignments.assign("avd-01", "proxy-1")
    first.scripts.upsert(make_script())
    first.runs.upsert(ScriptRun(id="run-persist", script_id="script-1", state=RunState.SUCCEEDED))
    first.images.upsert(make_image())
    first.oobe.upsert(OobeRecord(image_id=make_image().id, status=OobeStatus.VERIFIED))
    first.settings.set("theme", "dark")
    first.close()

    second = Store(path)
    second.initialize()
    try:
        assert second.templates.get("tpl-1").name == "Template tpl-1"
        assert second.proxies.get("proxy-1").secret_ref == "vault:proxies/proxy-1"
        assert second.assignments.get("avd-01").proxy_id == "proxy-1"
        assert second.scripts.get("script-1").content.startswith("adb shell")
        assert second.runs.get("run-persist").state is RunState.SUCCEEDED
        assert second.images.get(make_image().id).installed is True
        assert second.oobe.get(make_image().id).status is OobeStatus.VERIFIED
        assert second.settings.get("theme") == "dark"
        assert second.health()["ok"] is True
    finally:
        second.close()


def test_memory_store_works_across_threads(memory_store: Store) -> None:
    assert memory_store.health()["journal_mode"].lower() == "memory"
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for slot in range(10):
                memory_store.settings.set(f"setting-{index}-{slot}", slot)
                memory_store.templates.upsert(make_template(f"tpl-{index}-{slot}"))
        except BaseException as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(memory_store.settings.all()) == 40
    assert len(memory_store.templates.list()) == 40
    assert memory_store.checkpoint()["mode"] == "memory"


def test_concurrent_writes_on_disk(tmp_path: Path) -> None:
    instance = Store(tmp_path / "threads.db", timeout=30)
    instance.initialize()
    errors: list[BaseException] = []
    ready = threading.Barrier(6)

    def worker(index: int) -> None:
        try:
            ready.wait(timeout=10)
            for slot in range(6):
                instance.settings.set(f"key-{index}-{slot}", {"worker": index, "slot": slot})
                instance.proxies.upsert(make_proxy(f"proxy-{index}-{slot}"))
                instance.instances.upsert(AvdInfo(name=f"avd-{index}-{slot}", state=InstanceState.RUNNING))
        except BaseException as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    try:
        assert errors == []
        assert len(instance.settings.all()) == 36
        assert len(instance.proxies.list(include_disabled=True)) == 36
        assert len(instance.instances.list()) == 36
        assert instance.health()["ok"] is True
        assert instance.checkpoint()["ok"] is True
    finally:
        instance.close()
