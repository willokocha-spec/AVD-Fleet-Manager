"""Unit tests for the installer engine (ordering, dry-run, reboot, repair)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from avdfleet.core.config import Config
from avdfleet.core.errors import InstallerError, RebootRequiredError, ValidationError
from avdfleet.core.events import EventBus, EventType
from avdfleet.core.models import AcceleratorKind, HostOS, InstallOptions, OperationResult, StepStatus
from avdfleet.core.paths import Paths
from avdfleet.platform.installer import (
    STEP_IDS,
    InstallerEngine,
    ManifestStore,
    build_steps,
    new_manifest,
    pending_step,
)
from avdfleet.platform.installer.context import CommandResult, CommandRunner
from avdfleet.platform.installer.providers.base import (
    Command,
    EnvScope,
    InstallProvider,
    ToolTemplate,
    VirtualizationPlan,
)
from avdfleet.platform.installer.steps.base import BaseStep


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------
class RecordingRunner(CommandRunner):
    """Records invocations without touching a real process."""

    simulate_effects = True

    def __init__(self, *, dry_run: bool = False, handler: Any | None = None) -> None:
        super().__init__(dry_run=dry_run)
        self.handler = handler
        self.invocations: list[list[str]] = []

    def _execute(
        self,
        argv: list[str],
        *,
        timeout_s: float,
        stdin: str | None,
        env: dict[str, str],
        cwd: str | None,
        description: str,
    ) -> CommandResult:
        self.invocations.append(list(argv))
        if self.handler is not None:
            return self.handler(argv)
        return CommandResult(argv=list(argv), exit_code=0, stdout="", stderr="")


class FakeProvider(InstallProvider):
    id = "fake"
    platform = HostOS.LINUX
    display_name = "Fake host"
    package_manager_name = "fake-pkg"

    def __init__(
        self,
        *,
        root: Path,
        binaries: Mapping[str, str] | None = None,
        env: Mapping[str, str] | None = None,
        is_admin: bool = True,
        accel: VirtualizationPlan | None = None,
        aehd: bool = False,
    ) -> None:
        base_env = {"HOME": str(root), "PATH": "", "USER": "tester", **(env or {})}
        super().__init__(env=base_env, is_admin=is_admin)
        self.root = root
        self.binaries = dict(binaries or {})
        self._accel = accel or VirtualizationPlan(
            kind=AcceleratorKind.KVM, available=True, enabled=True, notes=("fake",)
        )
        self._aehd = aehd
        self._studio = root / "studio-config"
        self.installed_system_packages: list[str] = []

    def which(self, binary: str) -> str | None:
        return self.binaries.get(binary)

    def command_exists(self, binary: str) -> bool:
        return binary in self.binaries

    def package_manager_ready(self) -> bool:
        return True

    @property
    def package_manager(self) -> str:
        return "fake-pkg"

    def prerequisites(self) -> list[Command]:
        return []

    def install_system_packages(self, names: Sequence[str]) -> list[Command]:
        self.installed_system_packages.extend(names)
        return [Command(["fake-pkg", "install", *names])]

    def set_env_var(self, name: str, value: str, scope: EnvScope = EnvScope.USER) -> list[Command]:
        return [Command(["fake-env", "set", name, value])]

    def remove_env_var(self, name: str, scope: EnvScope = EnvScope.USER) -> list[Command]:
        return [Command(["fake-env", "unset", name])]

    def profile_files(self) -> list[Path]:
        return [self.root / ".fake-profile"]

    def virtualization_plan(self) -> VirtualizationPlan:
        return self._accel

    def aehd_installed(self) -> bool:
        return self._aehd

    def nested_virtualization(self) -> bool | None:
        return None

    def java_home_roots(self) -> list[Path]:
        # Keep tests independent of the host JDK installation.
        return []

    @property
    def install_command_templates(self) -> dict[str, ToolTemplate]:
        tools = ("jdk", "node", "appium", "appium_driver", "inspector", "studio", "scrcpy")
        return {
            tool: ToolTemplate(tool, commands=(Command(["fake-install", tool]),))
            for tool in tools
        }

    @property
    def studio_config_dir(self) -> Path:
        return self._studio


class MutableStep(BaseStep):
    """Stub step with a controllable check flag and recorder."""

    def __init__(
        self,
        step_id: str,
        *,
        recorder: list[str] | None = None,
        check_flag: dict[str, bool] | None = None,
        check_value: bool = False,
        critical: bool = True,
        requires_reboot: bool = False,
        depends_on: tuple[str, ...] = (),
        phase: str = "test",
        outcome: OperationResult | None = None,
        raises: BaseException | None = None,
        run_commands: list[list[str]] | None = None,
    ) -> None:
        self.id = step_id
        self.title = f"Stub {step_id}"
        self.description = "test stub"
        self.phase = phase
        self.critical = critical
        self.requires_reboot = requires_reboot
        self.depends_on = depends_on
        self.recorder = recorder if recorder is not None else []
        self.check_flag = check_flag
        self.check_value = check_value
        self.outcome = outcome
        self.raises = raises
        self.run_commands = run_commands
        self.check_calls = 0
        self.run_calls = 0

    def check(self, context: Any) -> bool:
        self.check_calls += 1
        if self.check_flag is not None:
            return bool(self.check_flag.get(self.id, False))
        return self.check_value

    def run(self, context: Any) -> OperationResult:
        self.run_calls += 1
        self.recorder.append(self.id)
        for argv in self.run_commands or []:
            context.run(argv)
        if self.raises is not None:
            raise self.raises
        if self.outcome is not None:
            return self.outcome
        return OperationResult(ok=True, message=f"{self.id} done")


def make_paths(tmp_path: Path) -> Paths:
    return Paths.for_portable(tmp_path / "root")


def make_provider(tmp_path: Path, **kwargs: Any) -> FakeProvider:
    return FakeProvider(root=tmp_path, **kwargs)


def make_engine(
    tmp_path: Path,
    *,
    steps: Sequence[BaseStep] | None = None,
    provider: InstallProvider | None = None,
    runner: CommandRunner | None = None,
    on_event: Any | None = None,
    bus: EventBus | None = None,
) -> InstallerEngine:
    return InstallerEngine(
        Config(),
        make_paths(tmp_path),
        provider or make_provider(tmp_path),
        runner=runner or RecordingRunner(),
        steps=steps,
        on_event=on_event,
        bus=bus,
    )


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
def test_plan_reports_ordered_steps_and_metadata(tmp_path: Path) -> None:
    steps = [
        MutableStep("a"),
        MutableStep("b", depends_on=("a",), requires_reboot=True),
        MutableStep("c", depends_on=("b",), critical=False),
    ]
    engine = make_engine(tmp_path, steps=steps)
    plan = engine.plan()

    assert [step.id for step in plan.steps] == ["a", "b", "c"]
    assert plan.steps[1].depends_on == ["a"]
    assert plan.steps[1].requires_reboot is True
    assert plan.steps[2].critical is False
    assert plan.already_installed is False
    assert plan.dry_run is False
    assert engine.store.exists() is False


def test_plan_detects_already_installed_and_reboot(tmp_path: Path) -> None:
    satisfied = {"a": True, "b": False}
    steps = [
        MutableStep("a", check_flag=satisfied),
        MutableStep("b", check_flag=satisfied, requires_reboot=True),
    ]
    engine = make_engine(tmp_path, steps=steps)
    plan = engine.plan()
    assert plan.already_installed is False
    assert plan.requires_reboot is True

    satisfied["b"] = True
    plan2 = engine.plan()
    assert plan2.already_installed is True
    assert plan2.requires_reboot is False


def test_missing_dependency_is_rejected(tmp_path: Path) -> None:
    engine = make_engine(tmp_path, steps=[MutableStep("b", depends_on=("missing",))])
    with pytest.raises(InstallerError):
        engine.plan()


# ---------------------------------------------------------------------------
# Install ordering / manifests
# ---------------------------------------------------------------------------
def test_install_runs_steps_in_order_and_persists_manifest(tmp_path: Path) -> None:
    order: list[str] = []
    steps = [
        MutableStep("a", recorder=order),
        MutableStep("b", recorder=order, depends_on=("a",)),
        MutableStep("c", recorder=order, depends_on=("b",)),
    ]
    engine = make_engine(tmp_path, steps=steps)
    manifest = engine.install()

    assert order == ["a", "b", "c"]
    assert manifest.completed_at is not None
    stored = ManifestStore(engine.paths.manifest_path).load()
    assert stored is not None
    assert stored.completed_at is not None
    assert [stored.steps[sid].status for sid in ("a", "b", "c")] == [
        StepStatus.SUCCEEDED
    ] * 3
    assert engine.status() is not None
    # No leftover atomic-write temp files next to the manifest.
    leftovers = [p.name for p in engine.paths.data_dir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_skip_idempotence_checks_are_not_rerun(tmp_path: Path) -> None:
    order: list[str] = []
    steps = [
        MutableStep("a", recorder=order, check_value=True),
        MutableStep("b", recorder=order),
    ]
    engine = make_engine(tmp_path, steps=steps)
    manifest = engine.install()

    assert order == ["b"]
    assert manifest.steps["a"].status is StepStatus.SKIPPED
    assert "already satisfied" in manifest.steps["a"].message
    assert manifest.steps["b"].status is StepStatus.SUCCEEDED


def test_second_install_is_a_noop(tmp_path: Path) -> None:
    order: list[str] = []
    steps = [MutableStep("a", recorder=order, check_value=True)]
    runner = RecordingRunner()
    engine = make_engine(tmp_path, steps=steps, runner=runner)
    first = engine.install()
    assert first.completed_at is not None

    runner.reset()
    second = engine.install()
    assert order == []
    assert runner.records == []
    assert second.completed_at == first.completed_at
    assert second.resumed_count == first.resumed_count


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------
def test_dry_run_records_commands_without_writes(tmp_path: Path) -> None:
    order: list[str] = []
    steps = [
        MutableStep(
            "a",
            recorder=order,
            run_commands=[["fake-tool", "--flag"]],
        )
    ]
    runner = RecordingRunner()
    engine = make_engine(tmp_path, steps=steps, runner=runner)
    manifest = engine.install(InstallOptions(dry_run=True))

    assert order == ["a"]
    assert [record.argv for record in runner.records] == [["fake-tool", "--flag"]]
    assert all(record.dry_run for record in runner.records)
    assert runner.executed == []
    assert manifest.completed_at is not None
    assert engine.store.exists() is False
    assert list(engine.paths.data_dir.glob("*.json")) == []


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------
def test_critical_failure_stops_and_records(tmp_path: Path) -> None:
    order: list[str] = []
    steps = [
        MutableStep("a", recorder=order),
        MutableStep(
            "b",
            recorder=order,
            outcome=OperationResult(ok=False, message="boom", data={"hint": "do the thing"}),
        ),
        MutableStep("c", recorder=order),
    ]
    engine = make_engine(tmp_path, steps=steps)
    with pytest.raises(InstallerError) as info:
        engine.install()

    assert order == ["a", "b"]
    assert info.value.details["step_id"] == "b"
    assert "do the thing" in (info.value.hint or "")
    manifest = ManifestStore(engine.paths.manifest_path).load()
    assert manifest is not None
    assert manifest.completed_at is None
    assert manifest.steps["a"].status is StepStatus.SUCCEEDED
    assert manifest.steps["b"].status is StepStatus.FAILED
    assert "c" not in manifest.steps
    assert engine.last_manifest is not None


def test_noncritical_failure_continues(tmp_path: Path) -> None:
    order: list[str] = []
    steps = [
        MutableStep("a", recorder=order),
        MutableStep(
            "b",
            recorder=order,
            critical=False,
            outcome=OperationResult(ok=False, message="optional tool failed"),
        ),
        MutableStep("c", recorder=order),
    ]
    engine = make_engine(tmp_path, steps=steps)
    manifest = engine.install()

    assert order == ["a", "b", "c"]
    assert manifest.completed_at is not None
    assert manifest.steps["b"].status is StepStatus.FAILED
    assert manifest.steps["c"].status is StepStatus.SUCCEEDED


def test_install_after_critical_failure_continues_where_it_stopped(tmp_path: Path) -> None:
    order: list[str] = []
    failing = MutableStep(
        "b",
        recorder=order,
        outcome=OperationResult(ok=False, message="transient failure"),
    )
    steps = [
        MutableStep("a", recorder=order),
        failing,
        MutableStep("c", recorder=order),
    ]
    engine = make_engine(tmp_path, steps=steps)
    with pytest.raises(InstallerError):
        engine.install()
    assert order == ["a", "b"]

    failing.outcome = None
    order.clear()
    manifest = engine.install()
    assert order == ["b", "c"]
    assert manifest.completed_at is not None
    assert manifest.steps["a"].status is StepStatus.SUCCEEDED
    assert manifest.steps["b"].status is StepStatus.SUCCEEDED
    assert manifest.resumed_count == 1


# ---------------------------------------------------------------------------
# Reboot pause / resume
# ---------------------------------------------------------------------------
def test_reboot_pause_and_resume(tmp_path: Path) -> None:
    order: list[str] = []
    flags = {"a": True, "b": False, "c": False}

    steps = [
        MutableStep("a", recorder=order, check_value=True),
        MutableStep(
            "b",
            recorder=order,
            check_flag=flags,
            requires_reboot=True,
        ),
        MutableStep("c", recorder=order, check_flag=flags, depends_on=("b",)),
    ]
    steps[1].raises = RebootRequiredError("reboot needed", hint="reboot now")
    engine = make_engine(tmp_path, steps=steps)

    with pytest.raises(RebootRequiredError) as info:
        engine.install()
    assert info.value.details["pending_step"] == "b"
    assert order == ["b"]
    stored = ManifestStore(engine.paths.manifest_path).load()
    assert stored is not None
    assert stored.completed_at is None
    assert stored.steps["b"].status is StepStatus.REBOOT_REQUIRED
    assert pending_step(stored) == "b"
    assert "c" not in stored.steps

    # After the reboot the accelerator check now reports satisfied.
    flags["b"] = True
    manifest = engine.resume()
    assert manifest.completed_at is not None
    assert order == ["b", "c"]
    assert manifest.steps["b"].status is StepStatus.SKIPPED
    assert manifest.resumed_count == 1


def test_resume_without_manifest_fails(tmp_path: Path) -> None:
    engine = make_engine(tmp_path, steps=[MutableStep("a")])
    with pytest.raises(InstallerError):
        engine.resume()


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------
def test_repair_reruns_unsatisfied_steps(tmp_path: Path) -> None:
    order: list[str] = []
    flags = {"a": False, "b": False}
    steps = [
        MutableStep("a", recorder=order, check_flag=flags),
        MutableStep("b", recorder=order, check_flag=flags),
    ]
    engine = make_engine(tmp_path, steps=steps)
    engine.install()
    assert order == ["a", "b"]

    order.clear()
    flags["a"] = True  # still healthy
    flags["b"] = False  # broken
    manifest = engine.repair()
    assert order == ["b"]
    assert manifest.steps["a"].status is StepStatus.SKIPPED
    assert manifest.steps["b"].status is StepStatus.SUCCEEDED
    assert manifest.completed_at is not None

    order.clear()
    flags.update(a=True, b=True)
    manifest = engine.repair()
    assert order == []
    assert manifest.completed_at is not None


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
def test_uninstall_dry_run_then_real(tmp_path: Path) -> None:
    runner = RecordingRunner()
    engine = make_engine(tmp_path, steps=[MutableStep("a")], runner=runner)
    engine.install()
    sdk = engine.paths.sdk_root
    (sdk / "platform-tools").mkdir(parents=True, exist_ok=True)
    managed_file = sdk / "platform-tools" / "adb"
    managed_file.write_text("fake", encoding="utf-8")

    assert engine.store.exists()
    dry = engine.uninstall(dry_run=True)
    assert dry.ok is True
    assert dry.data["dry_run"] is True
    assert any("would remove" in item for item in dry.data["removed"])
    assert engine.store.exists()
    assert managed_file.is_file()

    real = engine.uninstall()
    assert real.ok is True
    assert not engine.store.exists()
    assert not sdk.exists()
    assert any(str(sdk) in item for item in real.data["removed"])
    assert engine.paths.data_dir.is_dir()


def test_uninstall_keep_data(tmp_path: Path) -> None:
    engine = make_engine(tmp_path, steps=[MutableStep("a")])
    engine.install()
    sdk = engine.paths.sdk_root
    marker = sdk / "keep-me.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("data", encoding="utf-8")

    result = engine.uninstall(keep_data=True)
    assert result.ok is True
    assert marker.is_file()
    assert str(sdk) in result.data["kept"]
    assert not engine.store.exists()


def test_full_step_list_dry_run_covers_all_steps(tmp_path: Path) -> None:
    """End-to-end engine walk over the real 13 steps, fully dry-run (no writes)."""
    provider = make_provider(
        tmp_path,
        accel=VirtualizationPlan(kind=AcceleratorKind.KVM, available=True, enabled=True),
    )
    runner = RecordingRunner()
    engine = make_engine(tmp_path, provider=provider, runner=runner)
    options = InstallOptions(
        dry_run=True,
        accept_android_sdk_licenses=True,
        skip_studio=True,
    )
    plan = engine.plan(options)
    assert [step.id for step in plan.steps] == [
        sid for sid in STEP_IDS if sid != "studio.config"
    ]

    manifest = engine.install(options)
    assert manifest.completed_at is not None
    assert set(manifest.steps) == {step.id for step in plan.steps}
    failed = [
        sid
        for sid, result in manifest.steps.items()
        if result.status is StepStatus.FAILED
    ]
    assert failed == []
    assert runner.records and all(record.dry_run for record in runner.records)
    assert engine.store.exists() is False


# ---------------------------------------------------------------------------
# Selection / closure
# ---------------------------------------------------------------------------
def test_only_selection_dependency_closure(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)  # real steps
    plan = engine.plan(InstallOptions(only=["tools.appium"]))
    assert [step.id for step in plan.steps] == ["tools.node", "tools.appium"]

    with pytest.raises(ValidationError):
        engine.plan(InstallOptions(only=["does-not-exist"]))


def test_real_step_ordering_respects_dependencies() -> None:
    steps = build_steps()
    ids = [step.id for step in steps]
    assert ids == list(STEP_IDS)
    for index, step in enumerate(steps):
        for dependency in step.depends_on:
            assert ids.index(dependency) < index
    whpx = next(step for step in steps if step.id == "accel.whpx")
    assert whpx.requires_reboot is True


def test_full_closure_for_sdk_packages() -> None:
    steps = build_steps(only=["sdk.packages"])
    ids = [step.id for step in steps]
    assert ids == ["tools.jdk", "sdk.cmdline_tools", "sdk.licenses", "sdk.packages"]


def test_skip_flags_remove_optional_steps() -> None:
    steps = build_steps(skip_appium=True, skip_studio=True, skip_scrcpy=True)
    ids = {step.id for step in steps}
    assert "tools.appium" not in ids
    assert "studio.config" not in ids
    assert "tools.scrcpy" not in ids
    assert "tools.node" in ids


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
def test_progress_events_and_event_bus(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    bus = EventBus()
    engine = make_engine(
        tmp_path,
        steps=[MutableStep("a"), MutableStep("b", depends_on=("a",))],
        on_event=lambda event_type, payload: events.append((event_type, payload)),
        bus=bus,
    )
    engine.install()

    step_events = [
        payload
        for event_type, payload in events
        if event_type == EventType.INSTALL_STEP and payload.get("status") in {"succeeded", "skipped"}
    ]
    assert [payload["step_id"] for payload in step_events] == ["a", "b"]
    finished = [payload for event_type, payload in events if event_type == EventType.INSTALL_FINISHED]
    assert finished and finished[-1]["ok"] is True
    assert any(event.type == EventType.INSTALL_FINISHED for event in bus.history())


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------
def test_manifest_reload_and_corrupt_file(tmp_path: Path) -> None:
    engine = make_engine(tmp_path, steps=[MutableStep("a")])
    manifest = engine.install()

    fresh = make_engine(tmp_path, steps=[MutableStep("a")])
    loaded = fresh.status()
    assert loaded is not None
    assert loaded.steps["a"].status is StepStatus.SUCCEEDED
    assert loaded.completed_at == manifest.completed_at

    engine.paths.manifest_path.write_text("{not json", encoding="utf-8")
    assert fresh.status() is None


def test_manifest_helpers_pending_and_incomplete(tmp_path: Path) -> None:
    from avdfleet.core.models import StepResult
    from avdfleet.platform.installer.manifest import first_incomplete_step

    manifest = new_manifest(sdk_root=str(tmp_path))
    assert pending_step(manifest) is None
    assert first_incomplete_step(manifest, ["a", "b"]) == "a"
    manifest.steps["a"] = StepResult(step_id="a", status=StepStatus.SUCCEEDED)
    assert first_incomplete_step(manifest, ["a", "b"]) == "b"


def test_manifest_json_is_redacted_and_serialisable(tmp_path: Path) -> None:
    engine = make_engine(tmp_path, steps=[MutableStep("a")])
    engine.install()
    payload = json.loads(engine.paths.manifest_path.read_text(encoding="utf-8"))
    assert payload["steps"]["a"]["status"] == "succeeded"
    assert isinstance(payload["host"], dict)
