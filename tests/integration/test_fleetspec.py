"""Integration tests for the declarative fleet engine (``avdfleet.fleetspec``).

Everything here is offline and hermetic: the real managers are replaced by in-memory
fakes that implement the duck-typed service shapes, the image catalogue is a small
in-memory object, and ``password_env`` values are injected through an explicit
environment mapping.  No Android SDK, no emulator and no credentials are involved.
"""

from __future__ import annotations

import json
import logging
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
import yaml

from avdfleet.core.errors import ConflictError, FleetSpecError, NotFoundError
from avdfleet.core.events import EventBus, EventType
from avdfleet.core.models import (
    AvdInfo,
    FleetPlan,
    FleetSpec,
    ImageKind,
    InstanceState,
    PlanAction,
    PlanChange,
    ProxyAssignment,
    ProxyConfig,
    ProxyScheme,
    SystemImage,
    Template,
)
from avdfleet.fleetspec import (
    FLEET_API_VERSION,
    FleetLoader,
    FleetPlanner,
    FleetReconciler,
    ResolvedFleet,
    build_fleet_plan,
    fleet_schema_example,
    load_fleet,
    load_fleet_dict,
    load_fleet_text,
    validate_document,
)

pytestmark = pytest.mark.integration

IMAGE = "system-images;android-35;google_apis_playstore;x86_64"
SECOND_IMAGE = "system-images;android-36;google_apis;x86_64"
PASSWORD_ENV = "AVDFLEET_PROXY_1_PASSWORD"
PASSWORD_ENV_2 = "AVDFLEET_PROXY_2_PASSWORD"
SECRET = "s3cr3t-proxy-password"


# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


class FakeImageCatalog:
    """Duck-typed ``ImageCatalog`` with a fixed in-memory image set."""

    def __init__(self, images: Sequence[SystemImage] | None = None) -> None:
        self.images: dict[str, SystemImage] = {image.id: image for image in images or []}
        self.calls: list[str] = []

    @classmethod
    def with_image(
        cls,
        image_id: str = IMAGE,
        *,
        installed: bool = True,
    ) -> FakeImageCatalog:
        return cls([_system_image(image_id, installed=installed)])

    def list_images(
        self,
        *,
        include_remote: bool = True,
        installed_only: bool = False,
    ) -> list[SystemImage]:
        self.calls.append("list_images")
        images = list(self.images.values())
        if installed_only:
            images = [image for image in images if image.installed]
        return images

    def get_image(self, image_id: str) -> SystemImage:
        self.calls.append(f"get_image:{image_id}")
        image = self.images.get(image_id)
        if image is None:
            raise NotFoundError(f"system image not found: {image_id}")
        return image


def _system_image(image_id: str, *, installed: bool, api_level: int = 35) -> SystemImage:
    return SystemImage(
        id=image_id,
        package=image_id,
        api_level=api_level,
        tag="google_apis_playstore",
        abi="x86_64",
        kind=ImageKind.GOOGLE_APIS_PLAYSTORE,
        installed=installed,
    )


class FakeAvdManager:
    """Duck-typed ``AvdManager`` recording every mutation."""

    MUTATIONS = frozenset({"save_template", "create_instance", "delete_instance", "update_config"})

    def __init__(self) -> None:
        self.templates: dict[str, Template] = {}
        self.instances: dict[str, AvdInfo] = {}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.fail_on: dict[tuple[str, str], Exception] = {}

    # -- call accounting ----------------------------------------------------
    def _call(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        key = (name, str(args[0]) if args else "")
        failure = self.fail_on.get(key)
        if failure is not None:
            raise failure

    def mutations(self) -> list[str]:
        return [name for name, _args, _kwargs in self.calls if name in self.MUTATIONS]

    # -- AvdManager protocol ------------------------------------------------
    def list_templates(self) -> list[Template]:
        return sorted(self.templates.values(), key=lambda template: template.id)

    def get_template(self, template_id: str) -> Template:
        try:
            return self.templates[template_id]
        except KeyError:
            raise NotFoundError(f"template not found: {template_id}") from None

    def save_template(self, template: Template) -> Template:
        self._call("save_template", template.id)
        self.templates[template.id] = template
        return template

    def delete_template(self, template_id: str) -> None:
        self._call("delete_template", template_id)
        self.templates.pop(template_id, None)

    def list_instances(self) -> list[AvdInfo]:
        return sorted(self.instances.values(), key=lambda instance: instance.name)

    def get_instance(self, name: str) -> AvdInfo:
        try:
            return self.instances[name]
        except KeyError:
            raise NotFoundError(f"instance not found: {name}") from None

    def create_instance(
        self,
        template_id: str,
        name: str,
        overrides: dict[str, Any] | None = None,
    ) -> AvdInfo:
        self._call("create_instance", name, template_id=template_id, overrides=overrides)
        config: dict[str, str] = {}
        if overrides and overrides.get("ram_mb") is not None:
            config["hw.ramSize"] = str(overrides["ram_mb"])
        instance = AvdInfo(
            name=name,
            template_id=template_id,
            image_id=IMAGE,
            state=InstanceState.STOPPED,
            config=config,
        )
        self.instances[name] = instance
        return instance

    def update_config(
        self,
        name: str,
        values: dict[str, str],
        *,
        dry_run: bool = False,
    ) -> AvdInfo:
        self._call("update_config", name, values=values, dry_run=dry_run)
        instance = self.instances[name]
        instance.config = {**instance.config, **{str(key): str(value) for key, value in values.items()}}
        return instance

    def delete_instance(self, name: str, *, delete_files: bool = True) -> None:
        self._call("delete_instance", name, delete_files=delete_files)
        self.instances.pop(name, None)

    # -- seeding helpers ----------------------------------------------------
    def seed_template(
        self,
        template_id: str = "pixel-api35",
        *,
        image_id: str = IMAGE,
        ram_mb: int = 4096,
        proxy_id: str | None = "proxy-1",
    ) -> Template:
        template = Template(
            id=template_id,
            name=template_id,
            image_id=image_id,
            device_id="pixel_7",
            ram_mb=ram_mb,
            heap_mb=256,
            cpu_cores=2,
            proxy_id=proxy_id,
        )
        self.templates[template_id] = template
        return template

    def seed_instance(
        self,
        name: str,
        template_id: str = "pixel-api35",
        *,
        state: InstanceState = InstanceState.STOPPED,
        ram_override: int | None = None,
    ) -> AvdInfo:
        config = {"hw.ramSize": str(ram_override)} if ram_override is not None else {}
        instance = AvdInfo(
            name=name,
            template_id=template_id,
            image_id=IMAGE,
            state=state,
            config=config,
        )
        self.instances[name] = instance
        return instance


class FakeProxyPool:
    """Duck-typed ``ProxyPool`` keeping passwords in a separate secret store."""

    def __init__(self) -> None:
        self.proxies: dict[str, ProxyConfig] = {}
        self.secrets: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    def list_proxies(self, *, include_disabled: bool = False) -> list[ProxyConfig]:
        return sorted(self.proxies.values(), key=lambda proxy: proxy.id)

    def get(self, proxy_id: str) -> ProxyConfig:
        try:
            return self.proxies[proxy_id]
        except KeyError:
            raise NotFoundError(f"proxy not found: {proxy_id}") from None

    def add(self, proxy: ProxyConfig) -> ProxyConfig:
        self.calls.append(("add", proxy.id))
        if proxy.id in self.proxies:
            raise ConflictError(f"proxy already exists: {proxy.id}")
        self.proxies[proxy.id] = proxy
        if proxy.password is not None:
            self.secrets[proxy.id] = proxy.password.get_secret_value()
        return proxy

    def update(self, proxy_id: str, values: Mapping[str, Any]) -> ProxyConfig:
        self.calls.append(("update", proxy_id))
        current = self.get(proxy_id)
        payload = dict(values)
        password = payload.pop("password", None)
        updated = ProxyConfig(
            id=current.id,
            name=current.name,
            scheme=payload.get("scheme", current.scheme),
            host=payload.get("host", current.host),
            port=payload.get("port", current.port),
            username=payload.get("username", current.username),
            password=current.password if password is None else password,
            tags=payload.get("tags", current.tags),
            enabled=current.enabled,
            source=current.source,
        )
        self.proxies[proxy_id] = updated
        if password is not None:
            self.secrets[proxy_id] = (
                password.get_secret_value() if hasattr(password, "get_secret_value") else str(password)
            )
        return updated

    def delete(self, proxy_id: str) -> None:
        self.calls.append(("delete", proxy_id))
        self.proxies.pop(proxy_id, None)
        self.secrets.pop(proxy_id, None)

    def seed(self, proxy_id: str = "proxy-1") -> ProxyConfig:
        proxy = ProxyConfig(
            id=proxy_id,
            name=proxy_id,
            scheme=ProxyScheme.HTTP,
            host="127.0.0.1",
            port=8081,
            username="fleet",
            tags=[],
        )
        self.proxies[proxy_id] = proxy
        return proxy


class FakeAssigner:
    """Duck-typed ``ProxyAssigner``."""

    def __init__(self) -> None:
        self.assignments: dict[str, ProxyAssignment] = {}
        self.calls: list[tuple[str, str, str | None]] = []

    def assign(self, avd_name: str, proxy_id: str) -> ProxyAssignment:
        self.calls.append(("assign", avd_name, proxy_id))
        assignment = ProxyAssignment(avd_name=avd_name, proxy_id=proxy_id)
        self.assignments[avd_name] = assignment
        return assignment

    def unassign(self, avd_name: str) -> None:
        self.calls.append(("unassign", avd_name, None))
        self.assignments.pop(avd_name, None)

    def get(self, avd_name: str) -> ProxyAssignment | None:
        return self.assignments.get(avd_name)

    def list_assignments(self) -> list[ProxyAssignment]:
        return sorted(self.assignments.values(), key=lambda assignment: assignment.avd_name)


class FakeSettings:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class FakeStore:
    def __init__(self) -> None:
        self.settings = FakeSettings()


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class Harness:
    avds: FakeAvdManager = field(default_factory=FakeAvdManager)
    proxies: FakeProxyPool = field(default_factory=FakeProxyPool)
    assignments: FakeAssigner = field(default_factory=FakeAssigner)
    images: FakeImageCatalog = field(default_factory=lambda: FakeImageCatalog.with_image())
    store: FakeStore = field(default_factory=FakeStore)
    events: EventBus = field(default_factory=EventBus)
    environ: dict[str, str] = field(
        default_factory=lambda: {PASSWORD_ENV: SECRET, PASSWORD_ENV_2: SECRET + "-two"}
    )

    def loader(self, **kwargs: Any) -> FleetLoader:
        kwargs.setdefault("image_catalog", self.images)
        kwargs.setdefault("environ", self.environ)
        return FleetLoader(**kwargs)

    def planner(self, *, loader: FleetLoader | None = None, **kwargs: Any) -> FleetPlanner:
        kwargs.setdefault("proxies", self.proxies)
        kwargs.setdefault("assignments", self.assignments)
        kwargs.setdefault("images", self.images)
        kwargs.setdefault("store", self.store)
        kwargs.setdefault("loader", loader or self.loader())
        return FleetPlanner(self.avds, **kwargs)

    def reconciler(self, *, loader: FleetLoader | None = None, **kwargs: Any) -> FleetReconciler:
        environ = kwargs.get("environ", self.environ)
        kwargs.setdefault("proxies", self.proxies)
        kwargs.setdefault("assignments", self.assignments)
        kwargs.setdefault("images", self.images)
        kwargs.setdefault("store", self.store)
        kwargs.setdefault("events", self.events)
        kwargs.setdefault("loader", loader or self.loader(environ=environ))
        kwargs.setdefault("environ", environ)
        return FleetReconciler(self.avds, **kwargs)

    def fleet_events(self) -> list[dict[str, Any]]:
        return [event.payload for event in self.events.history() if event.type == EventType.FLEET_APPLIED]


@pytest.fixture()
def harness() -> Harness:
    return Harness()


# ---------------------------------------------------------------------------
# Document helpers
# ---------------------------------------------------------------------------


def example_document() -> dict[str, Any]:
    return fleet_schema_example()


def example_spec(harness: Harness) -> FleetSpec:
    return harness.loader().from_dict(example_document())


def example_yaml() -> str:
    return yaml.safe_dump(example_document(), sort_keys=False)


def two_instance_document() -> dict[str, Any]:
    document = example_document()
    document["instances"] = [
        {"name": "avd-01", "template": "pixel-api35"},
        {"name": "avd-02", "template": "pixel-api35"},
    ]
    return document


def add_second_proxy(document: dict[str, Any]) -> dict[str, Any]:
    document["proxies"] = [
        *document["proxies"],
        {
            "id": "proxy-2",
            "scheme": "socks5",
            "host": "127.0.0.1",
            "port": 1080,
            "username": "fleet",
            "password_env": PASSWORD_ENV_2,
        },
    ]
    return document


def change_summary(plan: FleetPlan) -> dict[str, int]:
    return dict(plan.summary)


def plan_names(plan: FleetPlan, action: PlanAction) -> list[str]:
    return [change.name for change in plan.changes if change.action is action]


# ===========================================================================
# Loader: parsing and schema validation
# ===========================================================================


def test_load_schema_example_from_dict() -> None:
    loader = FleetLoader()
    spec = loader.from_dict(example_document())

    assert isinstance(spec, FleetSpec)
    assert spec.apiVersion == FLEET_API_VERSION
    assert spec.kind == "Fleet"
    assert spec.name == "demo"
    assert spec.capacity == 8
    assert len(spec.templates) == 1
    assert spec.templates[0].image == IMAGE
    assert spec.templates[0].proxy == "proxy-1"
    assert [instance.name for instance in spec.instances] == ["avd-01", "avd-02", "avd-03"]
    assert spec.proxies[0].password_env == PASSWORD_ENV
    assert loader.validate(spec) == []


def test_load_yaml_file_and_text(tmp_path: Any) -> None:
    loader = FleetLoader()
    text = example_yaml()

    spec_from_text = loader.loads(text)
    assert spec_from_text.name == "demo"

    path = tmp_path / "fleet.yaml"
    path.write_text(text, encoding="utf-8")
    assert loader.load(path).name == "demo"

    with pytest.raises(FileNotFoundError):
        loader.load(tmp_path / "missing.yaml")


def test_loader_supports_multiline_documents(harness: Harness) -> None:
    text = textwrap.dedent(
        f"""
        apiVersion: avdfleet/v1
        kind: Fleet
        name: multiline
        settings:
          operator_notes: |
            line one
            line two
        templates:
          - name: pixel-api35
            image: {IMAGE}
        instances:
          - name: avd-01
            template: pixel-api35
        """
    )
    spec = harness.loader().loads(text)
    assert spec.name == "multiline"
    assert spec.settings["operator_notes"] == "line one\nline two\n"


def test_loader_rejects_unknown_field_with_json_path() -> None:
    document = example_document()
    document["templates"][0]["unexpected"] = True

    with pytest.raises(FleetSpecError) as raised:
        FleetLoader().from_dict(document)

    error = raised.value
    assert error.code == "fleet_spec_error"
    paths = [item["path"] for item in error.details["errors"]]
    assert "$.templates[0].unexpected" in paths
    assert "unknown" in str(error).lower()
    assert any("unknown field" in str(item["hint"]).lower() for item in error.details["errors"])


def test_loader_rejects_wrong_api_version_with_hint() -> None:
    document = example_document()
    document["apiVersion"] = "v1"
    document["kind"] = "fleet"

    with pytest.raises(FleetSpecError) as raised:
        FleetLoader().from_dict(document)

    paths = [item["path"] for item in raised.value.details["errors"]]
    assert "$.apiVersion" in paths
    assert "avdfleet/v1" in str(raised.value.hint)


def test_loader_never_accepts_passwords_in_yaml() -> None:
    document = example_document()
    document["proxies"][0]["password"] = "hunter2"

    with pytest.raises(FleetSpecError) as raised:
        FleetLoader().from_dict(document)

    error = raised.value
    paths = [item["path"] for item in error.details["errors"]]
    assert "$.proxies[0].password" in paths
    hints = " ".join(str(item.get("hint", "")) for item in error.details["errors"])
    assert "password_env" in hints
    assert "hunter2" not in str(error)


def test_loader_validates_yaml_syntax_error_with_line_number() -> None:
    text = "apiVersion: avdfleet/v1\n\tkind: Fleet\nname: demo\n"
    with pytest.raises(FleetSpecError) as raised:
        FleetLoader().loads(text, source="fleet.yaml")

    error = raised.value
    assert "line 2" in str(error)
    assert error.details["line"] == 2
    assert error.details["column"] is not None


def test_loader_rejects_multiple_yaml_documents() -> None:
    text = """
apiVersion: avdfleet/v1
kind: Fleet
name: fleet-a
templates: []
---
apiVersion: avdfleet/v1
kind: Fleet
name: fleet-b
templates: []
"""
    with pytest.raises(FleetSpecError) as raised:
        FleetLoader().loads(text)
    assert "2 YAML documents" in str(raised.value)
    assert "exactly one document" in str(raised.value.hint)


def test_loader_resolves_template_and_proxy_references(harness: Harness) -> None:
    document = example_document()
    document["instances"][0]["template"] = "missing-template"
    document["templates"][0]["proxy"] = "missing-proxy"

    with pytest.raises(FleetSpecError) as raised:
        harness.loader().from_dict(document)

    paths = [item["path"] for item in raised.value.details["errors"]]
    assert "$.instances[0].template" in paths
    assert "$.templates[0].proxy" in paths
    hints = " ".join(str(item.get("hint", "")) for item in raised.value.details["errors"])
    assert "pixel-api35" in hints
    assert "proxy-1" in hints


def test_loader_detects_name_collisions_and_capacity(harness: Harness) -> None:
    document = example_document()
    document["instances"].append({"name": "avd-01", "template": "pixel-api35"})
    document["proxies"].append(dict(document["proxies"][0]))
    document["capacity"] = 2

    with pytest.raises(FleetSpecError) as raised:
        harness.loader().from_dict(document)

    paths = [item["path"] for item in raised.value.details["errors"]]
    assert "$.instances[3].name" in paths
    assert "$.proxies[1].id" in paths
    assert "$.instances" in paths
    assert "capacity" in str(raised.value).lower()


def test_loader_checks_image_requirements(harness: Harness) -> None:
    document = example_document()
    document["templates"][0]["image"] = "system-images;android-99;google_apis;x86_64"

    loader = harness.loader()
    problems = loader.validate(loader.from_dict(document))
    assert any("not present in the image catalogue" in problem for problem in problems)

    spec = loader.from_dict(document)
    with pytest.raises(FleetSpecError) as raised:
        loader.resolve(spec, require_images=True)
    message = str(raised.value)
    assert "not present in the image catalogue" in message
    assert "install or import" in str(raised.value.hint)


def test_loader_image_policy_for_known_but_uninstalled_image(harness: Harness) -> None:
    harness.images = FakeImageCatalog([_system_image(IMAGE, installed=False)])
    warn_loader = harness.loader(image_policy="warn")
    problems = warn_loader.validate(warn_loader.from_dict(example_document()))
    assert problems and all("not present" not in problem for problem in problems)
    assert any("not installed" in problem for problem in problems)

    fail_loader = harness.loader(image_policy="fail")
    fail_spec = fail_loader.from_dict(example_document())
    fail_problems = fail_loader.validate(fail_spec)
    assert any("not installed" in problem for problem in fail_problems)
    with pytest.raises(FleetSpecError) as raised:
        fail_loader.resolve(fail_spec, require_images=True)
    assert "download" in str(raised.value.hint).lower()


def test_resolve_expands_instance_defaults(harness: Harness) -> None:
    document = example_document()
    document["instances"] = [
        {"name": "inherits", "template": "pixel-api35"},
        {"name": "override", "template": "pixel-api35", "ram_mb": 8192},
    ]
    loader = harness.loader()
    resolved = loader.resolve(loader.from_dict(document))

    assert isinstance(resolved, ResolvedFleet)
    by_name = {instance.name: instance for instance in resolved.instances}
    assert by_name["inherits"].ram_mb == 4096
    assert by_name["inherits"].proxy == "proxy-1"
    assert by_name["override"].ram_mb == 8192
    assert by_name["override"].proxy == "proxy-1"
    assert resolved.image_ids() == (IMAGE,)


def test_password_env_resolution_never_leaks_secret(harness: Harness) -> None:
    loader = harness.loader()
    resolved = loader.resolve(loader.from_dict(example_document()))

    passwords = loader.resolve_proxy_passwords(resolved)
    assert passwords["proxy-1"].get_secret_value() == SECRET
    assert SECRET not in repr(passwords)
    assert SECRET not in str(passwords)

    spec = loader.from_dict(example_document())
    assert "password_env" in example_yaml()
    assert SECRET not in example_yaml()
    assert spec.proxies[0].password_env == PASSWORD_ENV


def test_password_env_missing_raises_before_any_work(harness: Harness) -> None:
    loader = FleetLoader(image_catalog=harness.images, environ={})
    resolved = loader.resolve(loader.from_dict(example_document()))

    with pytest.raises(FleetSpecError) as raised:
        loader.resolve_proxy_passwords(resolved)

    error = raised.value
    assert PASSWORD_ENV in str(error)
    assert error.details["missing"] == [{"proxy": "proxy-1", "variable": PASSWORD_ENV}]
    assert "fleet.yaml" in str(error.hint)


def test_schema_validate_document_helper() -> None:
    issues = validate_document({"apiVersion": "nope", "kind": "Fleet", "name": "x", "templates": []})
    assert issues
    assert any(issue.path == "$.apiVersion" for issue in issues)
    assert all(issue.hint for issue in issues if issue.path == "$.apiVersion")


# ===========================================================================
# Planner
# ===========================================================================


def test_plan_creates_everything_on_clean_host(harness: Harness) -> None:
    planner = harness.planner()
    plan = planner.plan(example_spec(harness))

    assert plan.applyable
    assert plan.errors == []
    assert plan.spec_name == "demo"
    assert change_summary(plan) == {"create": 5, "noop": 1}
    assert plan_names(plan, PlanAction.CREATE) == [
        "proxy-1",
        "pixel-api35",
        "avd-01",
        "avd-02",
        "avd-03",
    ]
    proxy_change = plan.changes[0]
    assert proxy_change.kind == "proxy"
    assert proxy_change.before is None
    assert proxy_change.after["password_env"] == PASSWORD_ENV
    template_change = next(change for change in plan.changes if change.kind == "template")
    assert template_change.after["image"] == IMAGE
    assert template_change.after["id"] == "pixel-api35"
    image_change = next(change for change in plan.changes if change.kind == "image")
    assert image_change.action is PlanAction.NOOP
    assert image_change.after["installed"] is True


def test_plan_is_stable_and_deterministic(harness: Harness) -> None:
    planner = harness.planner()
    first = planner.plan(example_spec(harness))
    second = planner.plan(example_spec(harness))
    assert [(change.action, change.kind, change.name) for change in first.changes] == [
        (change.action, change.kind, change.name) for change in second.changes
    ]


def test_plan_is_all_noop_after_apply(harness: Harness) -> None:
    reconciler = harness.reconciler()
    spec = example_spec(harness)
    applied = reconciler.apply(spec)
    assert applied.applyable, applied.errors

    plan = reconciler.plan(spec)
    assert change_summary(plan) == {"noop": 6}
    assert plan.applyable
    assert all(change.action is PlanAction.NOOP for change in plan.changes)


def test_plan_detects_template_and_instance_updates(harness: Harness) -> None:
    harness.avds.seed_template(ram_mb=2048)
    harness.avds.seed_instance("avd-01")

    plan = harness.planner().plan(example_spec(harness))

    template_change = next(change for change in plan.changes if change.kind == "template")
    assert template_change.action is PlanAction.UPDATE
    assert "ram_mb" in template_change.detail
    assert template_change.before["ram_mb"] == 2048
    assert template_change.after["ram_mb"] == 4096

    instance_change = next(change for change in plan.changes if change.name == "avd-01")
    assert instance_change.action is PlanAction.UPDATE
    assert instance_change.before["proxy"] is None
    assert instance_change.after["proxy"] == "proxy-1"


def test_plan_prune_only_deletes_instances(harness: Harness) -> None:
    harness.avds.seed_template()
    harness.avds.seed_instance("avd-01")
    harness.avds.seed_instance("avd-03", state=InstanceState.STOPPED)
    harness.avds.seed_instance("manual-01", template_id="manual-template")

    planner = harness.planner()
    spec = harness.loader().from_dict(two_instance_document())

    without_prune = planner.plan(spec, prune=False)
    assert plan_names(without_prune, PlanAction.DELETE) == []

    plan = planner.plan(spec, prune=True)
    assert plan_names(plan, PlanAction.DELETE) == ["avd-03"]
    delete_change = next(change for change in plan.changes if change.action is PlanAction.DELETE)
    assert delete_change.before["name"] == "avd-03"
    assert "manual-01" not in json.dumps(plan.model_dump(mode="json"))
    # Templates and proxies are never pruned by the fleet reconciler.
    assert all(
        change.kind == "instance"
        for change in plan.changes
        if change.action is PlanAction.DELETE
    )


def test_plan_prune_uses_store_ownership_after_apply(harness: Harness) -> None:
    reconciler = harness.reconciler()
    full_spec = harness.loader().from_dict(example_document())
    reconciler.apply(full_spec)

    recorded = harness.store.settings.get("fleet/demo/instances")
    assert recorded == ["avd-01", "avd-02", "avd-03"]

    smaller = harness.loader().from_dict(two_instance_document())
    plan = reconciler.plan(smaller, prune=True)
    assert plan_names(plan, PlanAction.DELETE) == ["avd-03"]

    # The same fleet with an additional manually created instance never prunes it.
    harness.avds.seed_instance("manual-01", template_id="manual-template")
    plan_again = reconciler.plan(smaller, prune=True)
    assert plan_names(plan_again, PlanAction.DELETE) == ["avd-03"]


def test_planner_image_policy(harness: Harness) -> None:
    harness.images = FakeImageCatalog([_system_image(IMAGE, installed=False)])
    spec = example_spec(harness)

    warn_plan = harness.planner(image_policy="warn").plan(spec)
    assert warn_plan.applyable
    image_change = next(change for change in warn_plan.changes if change.kind == "image")
    assert image_change.action is PlanAction.CREATE
    assert "not installed" in image_change.detail

    fail_plan = harness.planner(image_policy="fail").plan(spec)
    assert not fail_plan.applyable
    assert any("not installed" in error for error in fail_plan.errors)

    ignore_plan = harness.planner(image_policy="ignore").plan(spec)
    assert all(change.kind != "image" for change in ignore_plan.changes)


def test_planner_reports_invalid_spec_as_non_applyable_plan(harness: Harness) -> None:
    document = example_document()
    document["instances"][0]["template"] = "nope"
    body = harness.loader()
    with pytest.raises(FleetSpecError):
        body.from_dict(document)

    # A raw FleetSpec bypassing the loader is still reported, not raised.
    spec = FleetSpec.model_validate(
        {**example_document(), "instances": [{"name": "x", "template": "nope"}]}
    )
    plan = harness.planner().plan(spec)
    assert not plan.applyable
    assert any("nope" in error for error in plan.errors)


# ===========================================================================
# Reconciler: apply, idempotency, dry run
# ===========================================================================


def test_apply_creates_fleet_assignments_and_event(harness: Harness) -> None:
    reconciler = harness.reconciler()
    result = reconciler.apply(example_spec(harness))

    assert result.applyable, result.errors
    assert set(harness.avds.templates) == {"pixel-api35"}
    assert set(harness.avds.instances) == {"avd-01", "avd-02", "avd-03"}
    assert set(harness.proxies.proxies) == {"proxy-1"}
    assert harness.proxies.secrets["proxy-1"] == SECRET
    assert set(harness.assignments.assignments) == {"avd-01", "avd-02", "avd-03"}
    assert all(
        assignment.proxy_id == "proxy-1"
        for assignment in harness.assignments.assignments.values()
    )
    assert result.summary == {"create": 5, "noop": 1}

    events = harness.fleet_events()
    assert len(events) == 1
    payload = events[0]
    assert payload["fleet"] == "demo"
    assert payload["counts"]["created"] == 5
    assert payload["counts"]["failed"] == 0
    assert payload["applyable"] is True
    assert SECRET not in json.dumps(payload)
    # Passwords are never part of the plan either.
    serialised = json.dumps(result.model_dump(mode="json"))
    assert SECRET not in serialised
    assert "s3cr3t" not in serialised.lower()


def test_apply_is_idempotent(harness: Harness) -> None:
    reconciler = harness.reconciler()
    spec = example_spec(harness)
    first = reconciler.apply(spec)
    assert first.applyable

    mutations_after_first = list(harness.avds.mutations())
    second = reconciler.apply(spec)

    assert second.applyable
    assert second.summary == {"noop": 6}
    assert harness.avds.mutations() == mutations_after_first
    assert set(harness.avds.instances) == {"avd-01", "avd-02", "avd-03"}
    assert len(harness.fleet_events()) == 2
    assert harness.fleet_events()[1]["counts"]["created"] == 0


def test_apply_dry_run_makes_no_calls(harness: Harness) -> None:
    reconciler = harness.reconciler()
    result = reconciler.apply(example_spec(harness), dry_run=True)

    assert result.summary == {"create": 5, "noop": 1}
    assert harness.avds.mutations() == []
    assert harness.proxies.calls == []
    assert harness.assignments.calls == []
    assert harness.fleet_events() == []


def test_apply_missing_password_env_blocks_before_mutations(harness: Harness) -> None:
    reconciler = harness.reconciler(environ={})
    result = reconciler.apply(example_spec(harness))

    assert not result.applyable
    assert any(PASSWORD_ENV in error for error in result.errors)
    assert harness.avds.mutations() == []
    assert harness.proxies.calls == []
    assert harness.fleet_events() == []


def test_apply_precomputed_plan_positionally(harness: Harness) -> None:
    reconciler = harness.reconciler()
    spec = example_spec(harness)
    plan = reconciler.plan(spec)
    assert plan.summary["create"] == 5

    result = reconciler.apply(plan)
    assert result.applyable, result.errors
    assert set(harness.avds.instances) == {"avd-01", "avd-02", "avd-03"}
    assert result.summary == plan.summary


def test_apply_non_applyable_plan_is_a_noop(harness: Harness) -> None:
    harness.images = FakeImageCatalog([_system_image(IMAGE, installed=False)])
    reconciler = harness.reconciler(image_policy="fail")
    result = reconciler.apply(example_spec(harness))

    assert not result.applyable
    assert harness.avds.mutations() == []
    assert harness.proxies.calls == []


def test_apply_updates_template_config_and_assignment(harness: Harness) -> None:
    reconciler = harness.reconciler()
    first_spec = harness.loader().from_dict(example_document())
    assert reconciler.apply(first_spec).applyable

    document = add_second_proxy(example_document())
    document["templates"][0]["ram_mb"] = 6144
    document["instances"][0]["ram_mb"] = 8192
    document["instances"][0]["proxy"] = "proxy-2"
    updated_spec = harness.loader().from_dict(document)

    updated = reconciler.apply(updated_spec)
    assert updated.applyable, updated.errors

    assert harness.avds.templates["pixel-api35"].ram_mb == 6144
    assert harness.avds.instances["avd-01"].config["hw.ramSize"] == "8192"
    assert harness.assignments.assignments["avd-01"].proxy_id == "proxy-2"
    assert harness.proxies.secrets["proxy-2"] == SECRET + "-two"

    reapply = reconciler.apply(updated_spec)
    assert reapply.applyable
    assert reapply.summary == {"noop": 7}


def test_apply_never_logs_secrets(harness: Harness, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="avdfleet.fleetspec")
    reconciler = harness.reconciler()
    result = reconciler.apply(example_spec(harness))

    assert result.applyable
    assert SECRET not in caplog.text
    assert SECRET not in json.dumps(result.model_dump(mode="json"))
    assert SECRET not in json.dumps(harness.fleet_events())


def test_reconciler_load_and_validate_helpers(tmp_path: Any, harness: Harness) -> None:
    path = tmp_path / "fleet.yaml"
    path.write_text(example_yaml(), encoding="utf-8")

    reconciler = harness.reconciler()
    spec = reconciler.load(path)
    assert spec.name == "demo"
    assert reconciler.validate(spec) == []

    invalid = example_document()
    invalid["instances"].append({"name": "avd-01", "template": "pixel-api35"})
    with pytest.raises(FleetSpecError):
        harness.loader().from_dict(invalid)


# ===========================================================================
# Reconciler: prune protection and partial failures
# ===========================================================================


def test_prune_protects_running_instances_unless_force(harness: Harness) -> None:
    reconciler = harness.reconciler()
    reconciler.apply(harness.loader().from_dict(example_document()))
    harness.avds.instances["avd-03"].state = InstanceState.RUNNING

    smaller = harness.loader().from_dict(two_instance_document())
    protected = reconciler.apply(smaller, prune=True)

    assert not protected.applyable
    assert "avd-03" in harness.avds.instances
    assert any("running" in error.lower() for error in protected.errors)
    delete_change = next(change for change in protected.changes if change.action is PlanAction.DELETE)
    assert "failed" in delete_change.detail

    forced = reconciler.apply(smaller, prune=True, force=True)
    assert forced.applyable, forced.errors
    assert "avd-03" not in harness.avds.instances
    assert forced.summary["delete"] == 1
    assert harness.store.settings.get("fleet/demo/instances") == ["avd-01", "avd-02"]


def test_partial_failure_continue_policy(harness: Harness) -> None:
    harness.avds.fail_on[("create_instance", "avd-02")] = RuntimeError("adapter exploded")
    reconciler = harness.reconciler(on_error="continue")
    result = reconciler.apply(example_spec(harness))

    assert not result.applyable
    assert any("adapter exploded" in error for error in result.errors)
    assert set(harness.avds.instances) == {"avd-01", "avd-03"}
    assert result.summary == {"create": 5, "noop": 1}
    failed = [change for change in result.changes if "failed" in change.detail]
    assert [change.name for change in failed] == ["avd-02"]
    payload = harness.fleet_events()[0]
    assert payload["counts"]["failed"] == 1
    assert payload["counts"]["created"] == 4


def test_partial_failure_stop_policy(harness: Harness) -> None:
    harness.avds.fail_on[("create_instance", "avd-02")] = RuntimeError("stop here")
    reconciler = harness.reconciler(on_error="stop")
    result = reconciler.apply(example_spec(harness))

    assert not result.applyable
    assert set(harness.avds.instances) == {"avd-01"}
    skipped = [
        change.name
        for change in result.changes
        if change.action is not PlanAction.NOOP and "result=skipped" in change.detail
    ]
    assert skipped == ["avd-03"]


# ===========================================================================
# Extra edge cases: redaction, template migration, convenience API
# ===========================================================================


def test_load_fleet_convenience_functions(tmp_path: Any) -> None:
    document = example_document()
    assert load_fleet(document).name == "demo"
    assert load_fleet_dict(document).name == "demo"

    text = example_yaml()
    assert load_fleet_text(text).name == "demo"
    assert load_fleet(text).name == "demo"

    path = tmp_path / "fleet.yaml"
    path.write_text(text, encoding="utf-8")
    assert load_fleet(path).name == "demo"
    assert load_fleet(str(path)).name == "demo"


def test_build_fleet_plan_computes_summary() -> None:
    changes = [
        PlanChange(action=PlanAction.CREATE, kind="proxy", name="p"),
        PlanChange(action=PlanAction.NOOP, kind="instance", name="a"),
        PlanChange(action=PlanAction.NOOP, kind="instance", name="b"),
    ]
    plan = build_fleet_plan(spec_name="demo", changes=changes)
    assert plan.summary == {"create": 1, "noop": 2}
    assert plan.spec_name == "demo"
    assert plan.applyable


def test_manager_failure_messages_are_redacted(harness: Harness) -> None:
    harness.avds.fail_on[("create_instance", "avd-02")] = RuntimeError(
        "proxy exploded: http://fleet:hunter2@127.0.0.1:8081"
    )
    reconciler = harness.reconciler()
    result = reconciler.apply(example_spec(harness))

    assert not result.applyable
    rendered = json.dumps(result.model_dump(mode="json"))
    assert "hunter2" not in rendered
    assert "hunter2" not in json.dumps(harness.fleet_events())
    assert any("proxy exploded" in error for error in result.errors)


def _add_second_template(document: dict[str, Any]) -> dict[str, Any]:
    document["templates"] = [
        *document["templates"],
        {"name": "pixel-api36", "image": SECOND_IMAGE, "proxy": "proxy-1"},
    ]
    return document


def test_apply_instance_template_change_without_redeploy_is_reported(harness: Harness) -> None:
    harness.images.images[SECOND_IMAGE] = _system_image(SECOND_IMAGE, installed=True, api_level=36)
    reconciler = harness.reconciler()
    document = _add_second_template(example_document())
    assert reconciler.apply(harness.loader().from_dict(document)).applyable

    document["instances"][0]["template"] = "pixel-api36"
    result = reconciler.apply(harness.loader().from_dict(document))

    assert not result.applyable
    assert any("cannot change the template" in error for error in result.errors)
    assert any("redeploy" in str(error) for error in result.errors)
    assert harness.avds.instances["avd-01"].template_id == "pixel-api35"


def test_apply_instance_template_change_uses_redeploy_hook(harness: Harness) -> None:
    harness.images.images[SECOND_IMAGE] = _system_image(SECOND_IMAGE, installed=True, api_level=36)
    reconciler = harness.reconciler()
    document = _add_second_template(example_document())
    assert reconciler.apply(harness.loader().from_dict(document)).applyable

    redeploys: list[tuple[str, str]] = []

    def recreate_instance(name: str, template_id: str) -> None:
        redeploys.append((name, template_id))
        harness.avds.instances[name].template_id = template_id

    harness.avds.recreate_instance = recreate_instance  # type: ignore[attr-defined]

    document["instances"][0]["template"] = "pixel-api36"
    result = reconciler.apply(harness.loader().from_dict(document))

    assert result.applyable, result.errors
    assert redeploys == [("avd-01", "pixel-api36")]
    assert harness.avds.instances["avd-01"].template_id == "pixel-api36"
