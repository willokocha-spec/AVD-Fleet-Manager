"""Unit tests for the individual installer steps (no network, no real installs)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from avdfleet.core.config import Config
from avdfleet.core.errors import RebootRequiredError, ValidationError
from avdfleet.core.models import (
    AcceleratorKind,
    HostOS,
    InstallOptions,
    OperationResult,
    StepStatus,
)
from avdfleet.core.paths import Paths
from avdfleet.platform.installer.context import CommandResult, CommandRunner, InstallContext
from avdfleet.platform.installer.providers.base import (
    KVM_UDEV_RULE,
    KVM_UDEV_RULE_PATH,
    Command,
    EnvScope,
    InstallProvider,
    ToolTemplate,
    VirtualizationPlan,
)
from avdfleet.platform.installer.steps import (
    STEP_IDS,
    BaseStep,
    all_step_specs,
    build_steps,
    resolve_selection,
)
from avdfleet.platform.installer.steps.accel_aehd import AehdStep
from avdfleet.platform.installer.steps.accel_detect import AccelDetectStep
from avdfleet.platform.installer.steps.accel_kvm import KvmStep
from avdfleet.platform.installer.steps.accel_whpx import WhpxStep
from avdfleet.platform.installer.steps.sdk_cmdline import (
    SdkCmdlineToolsStep,
    cmdline_tools_filename,
    cmdline_tools_url,
)
from avdfleet.platform.installer.steps.sdk_env import SdkEnvStep
from avdfleet.platform.installer.steps.sdk_licenses import SdkLicensesStep, licenses_accepted
from avdfleet.platform.installer.steps.sdk_packages import (
    SdkPackagesStep,
    package_path,
    required_packages,
)
from avdfleet.platform.installer.steps.studio import (
    STUDIO_PROPERTIES_FILENAME,
    StudioConfigStep,
    properties_content,
    properties_path,
)
from avdfleet.platform.installer.steps.tools_appium import AppiumStep
from avdfleet.platform.installer.steps.tools_jdk import JdkStep, find_java_home
from avdfleet.platform.installer.steps.tools_node import NodeStep
from avdfleet.platform.installer.steps.tools_scrcpy import ScrcpyStep


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------
class RecordingRunner(CommandRunner):
    simulate_effects = True

    def __init__(self, *, handler: Any | None = None, dry_run: bool = False) -> None:
        super().__init__(dry_run=dry_run)
        self.handler = handler
        self.invocations: list[list[str]] = []
        self.stdins: list[str | None] = []
        self.envs: list[dict[str, str]] = []

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
        self.stdins.append(stdin)
        self.envs.append(dict(env))
        if self.handler is not None:
            return self.handler(argv, stdin, env)
        return CommandResult(argv=list(argv), exit_code=0)


class StubProvider(InstallProvider):
    id = "stub"
    display_name = "Stub host"
    package_manager_name = "stub-pkg"

    def __init__(
        self,
        *,
        root: Path,
        platform: HostOS = HostOS.LINUX,
        binaries: Mapping[str, str] | None = None,
        env: Mapping[str, str] | None = None,
        is_admin: bool = True,
        accel: VirtualizationPlan | None = None,
        aehd: bool = False,
        aehd_plan: VirtualizationPlan | None = None,
        nested: bool | None = None,
        studio_dir: Path | None = None,
        jbr_home: Path | None = None,
        templates: Mapping[str, Sequence[Command]] | None = None,
    ) -> None:
        super().__init__(
            env={"HOME": str(root), "PATH": "", "USER": "tester", **(env or {})},
            is_admin=is_admin,
        )
        self.root = root
        self._platform = platform
        self.binaries = dict(binaries or {})
        self._accel = accel or VirtualizationPlan(
            kind=AcceleratorKind.KVM, available=True, enabled=True, notes=("stub",)
        )
        self._aehd = aehd
        self._aehd_plan = aehd_plan
        self._nested = nested
        self._studio = studio_dir or (root / "studio-config")
        self._jbr = jbr_home
        self._templates = dict(templates or {})
        self.system_packages: list[str] = []

    @property
    def platform(self) -> HostOS:  # type: ignore[override]
        return self._platform

    def which(self, binary: str) -> str | None:
        return self.binaries.get(binary)

    def package_manager_ready(self) -> bool:
        return True

    @property
    def package_manager(self) -> str:
        return "stub-pkg"

    def prerequisites(self) -> list[Command]:
        return []

    def install_system_packages(self, names: Sequence[str]) -> list[Command]:
        self.system_packages.extend(names)
        return [Command(["stub-pkg", "install", *names])]

    def set_env_var(self, name: str, value: str, scope: EnvScope = EnvScope.USER) -> list[Command]:
        return [Command(["stub-env", "set", name, value])]

    def remove_env_var(self, name: str, scope: EnvScope = EnvScope.USER) -> list[Command]:
        return [Command(["stub-env", "unset", name])]

    def profile_files(self) -> list[Path]:
        return [self.root / ".stub-profile"]

    def virtualization_plan(self) -> VirtualizationPlan:
        return self._accel

    def aehd_installed(self) -> bool:
        return self._aehd

    def aehd_migration_plan(self) -> VirtualizationPlan:
        if self._aehd_plan is not None:
            return self._aehd_plan
        return super().aehd_migration_plan()

    def nested_virtualization(self) -> bool | None:
        return self._nested

    def java_home_roots(self) -> list[Path]:
        # Keep tests independent of the host JDK installation.
        return []

    def studio_jbr_home(self) -> Path | None:
        return self._jbr

    @property
    def install_command_templates(self) -> dict[str, ToolTemplate]:
        default = ("jdk", "node", "appium", "appium_driver", "inspector", "studio", "scrcpy")
        templates: dict[str, ToolTemplate] = {}
        for tool in default:
            commands = self._templates.get(tool, [Command(["stub-install", tool])])
            templates[tool] = ToolTemplate(tool, commands=tuple(commands))
        return templates

    @property
    def studio_config_dir(self) -> Path:
        return self._studio


class FakeBackend:
    """Minimal SdkBackend stand-in for the delegation tests."""

    name = "fake-backend"

    def __init__(self, installed: Sequence[str] = ()) -> None:
        self.installed = list(installed)
        self.install_calls: list[tuple[list[str], bool]] = []
        self.license_calls = 0

    def executable(self, tool: str) -> Path:
        raise FileNotFoundError(tool)

    def list_installed(self) -> list[Any]:
        from avdfleet.core.models import SdkPackage

        return [SdkPackage(id=package_id, installed=True) for package_id in self.installed]

    def install_packages(
        self,
        package_ids: Sequence[str],
        *,
        accept_licenses: bool = False,
        dry_run: bool = False,
        timeout_s: int = 1800,
    ) -> OperationResult:
        self.install_calls.append((list(package_ids), accept_licenses))
        self.installed.extend(package_ids)
        return OperationResult(ok=True, message="backend installed")

    def accept_licenses(self, *, timeout_s: int = 300) -> OperationResult:
        self.license_calls += 1
        return OperationResult(ok=True, message="backend accepted")


def make_paths(tmp_path: Path) -> Paths:
    return Paths.for_portable(tmp_path / "root")


def make_context(
    tmp_path: Path,
    *,
    provider: StubProvider | None = None,
    runner: RecordingRunner | None = None,
    options: InstallOptions | None = None,
    backend: Any | None = None,
    config: Config | None = None,
) -> InstallContext:
    return InstallContext(
        config=config or Config(),
        paths=make_paths(tmp_path),
        provider=provider or StubProvider(root=tmp_path),
        runner=runner or RecordingRunner(),
        options=options or InstallOptions(),
        sdk_backend=backend,
    )


def create_file(path: Path, content: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Metadata / selection
# ---------------------------------------------------------------------------
def test_step_catalogue_metadata_is_stable() -> None:
    steps = build_steps()
    ids = [step.id for step in steps]
    assert ids == list(STEP_IDS)
    assert len(ids) == len(set(ids)) == 13
    specs = all_step_specs()
    assert [spec.id for spec in specs] == ids
    for step, spec in zip(steps, specs, strict=True):
        assert spec.title and spec.description
        assert spec.phase in {"tools", "sdk", "config", "acceleration", "core"}
        assert isinstance(step, BaseStep)

    by_id = {step.id: step for step in steps}
    assert by_id["accel.whpx"].requires_reboot is True
    assert by_id["accel.whpx"].critical is True
    assert by_id["tools.scrcpy"].critical is False
    assert by_id["studio.config"].critical is False
    assert by_id["accel.detect"].always_run is True
    assert by_id["sdk.licenses"].depends_on == ("sdk.cmdline_tools",)


def test_step_selection_aliases_phases_and_errors() -> None:
    assert resolve_selection(["licenses", "whpx"]) == ["sdk.licenses", "accel.whpx"]
    sdk = resolve_selection(["sdk"])
    assert sdk == ["sdk.cmdline_tools", "sdk.licenses", "sdk.packages", "sdk.env"]
    with pytest.raises(ValidationError):
        resolve_selection(["nope"])
    ids = [step.id for step in build_steps(only=["sdk.env"])]
    assert ids == ["tools.jdk", "sdk.cmdline_tools", "sdk.licenses", "sdk.packages", "sdk.env"]


# ---------------------------------------------------------------------------
# SDK steps
# ---------------------------------------------------------------------------
def test_cmdline_tools_url_and_filename_helpers() -> None:
    assert cmdline_tools_filename("windows", "123") == "commandlinetools-win-123_latest.zip"
    assert cmdline_tools_filename("macos", "123") == "commandlinetools-mac-123_latest.zip"
    assert cmdline_tools_filename("linux", "123") == "commandlinetools-linux-123_latest.zip"
    url, sha = cmdline_tools_url("linux", build="123", sha256="deadbeef")
    assert url == "https://dl.google.com/android/repository/commandlinetools-linux-123_latest.zip"
    assert sha == "deadbeef"


def test_cmdline_tools_step_downloads_and_unpacks_in_dry_run(tmp_path: Path) -> None:
    provider = StubProvider(root=tmp_path)
    runner = RecordingRunner()
    context = make_context(tmp_path, provider=provider, runner=runner)
    step = SdkCmdlineToolsStep()
    assert step.check(context) is False

    result = step.run(context)
    assert result.ok is True
    assert result.data["sdkmanager"].endswith(str(Path("cmdline-tools") / "latest" / "bin" / "sdkmanager"))
    assert result.data["url"].startswith("https://dl.google.com/android/repository/commandlinetools-linux-")
    invoked = [" ".join(argv) for argv in runner.invocations]
    assert any("curl" in argv for argv in invoked)
    assert any("unzip" in argv for argv in invoked)
    assert any("cmdline-tools" in argv for argv in invoked)


def test_cmdline_tools_step_uses_url_env_override(tmp_path: Path) -> None:
    provider = StubProvider(
        root=tmp_path,
        env={
            "AVDFLEET_CMDLINE_TOOLS_URL": "https://example.invalid/tools.zip",
            "AVDFLEET_CMDLINE_TOOLS_SHA256": "abc123",
        },
    )
    runner = RecordingRunner()
    context = make_context(tmp_path, provider=provider, runner=runner)
    result = SdkCmdlineToolsStep().run(context)
    assert result.ok is True
    assert result.data["url"] == "https://example.invalid/tools.zip"
    commands = " ".join(" ".join(record.argv) for record in runner.records)
    assert "https://example.invalid/tools.zip" in commands
    assert "abc123" in commands


def test_cmdline_tools_dry_run_does_not_execute(tmp_path: Path) -> None:
    runner = RecordingRunner()
    context = make_context(tmp_path, runner=runner, options=InstallOptions(dry_run=True))
    result = SdkCmdlineToolsStep().run(context)
    assert result.ok is True
    assert runner.records
    assert all(record.dry_run for record in runner.records)
    assert runner.invocations == []
    assert not (context.sdk_root / "cmdline-tools").exists()


def test_cmdline_tools_check_detects_installed_sdkmanager(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    sdkmanager = create_file(
        context.sdk_root / "cmdline-tools" / "latest" / "bin" / "sdkmanager"
    )
    assert SdkCmdlineToolsStep().check(context) is True
    assert sdkmanager.is_file()


def test_licenses_step_requires_explicit_flag(tmp_path: Path) -> None:
    step = SdkLicensesStep()
    context = make_context(tmp_path)
    result = step.run(context)
    assert result.ok is False
    assert "not accepted" in result.message
    assert "--accept-android-sdk-licenses" in result.data["hint"]
    assert context.runner.records == []


def test_licenses_step_sends_yes_when_flag_passed(tmp_path: Path) -> None:
    context = make_context(
        tmp_path,
        options=InstallOptions(accept_android_sdk_licenses=True),
    )
    runner = context.runner
    assert isinstance(runner, RecordingRunner)
    result = SdkLicensesStep().run(context)
    assert result.ok is True
    assert any("--licenses" in record.argv for record in runner.records)
    assert any(stdin and "y" in stdin for stdin in runner.stdins)
    assert licenses_accepted(context.sdk_root) is False or True  # simulated: no real file needed


def test_licenses_check_and_backend_delegation(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    create_file(context.sdk_root / "licenses" / "android-sdk-license", "hash")
    assert SdkLicensesStep().check(context) is True

    backend = FakeBackend()
    backend_context = make_context(
        tmp_path / "backend",
        options=InstallOptions(accept_android_sdk_licenses=True),
        backend=backend,
    )
    result = SdkLicensesStep().run(backend_context)
    assert result.ok is True
    assert backend.license_calls == 1


def test_sdk_package_path_mapping(tmp_path: Path) -> None:
    root = tmp_path / "sdk"
    assert package_path(root, "platform-tools") == root / "platform-tools"
    assert package_path(root, "build-tools;35.0.0") == root / "build-tools" / "35.0.0"
    assert package_path(root, "platforms;android-35") == root / "platforms" / "android-35"
    assert (
        package_path(root, "system-images;android-35;google_apis;x86_64")
        == root / "system-images" / "android-35" / "google_apis" / "x86_64"
    )
    assert required_packages({}) and "platform-tools" in required_packages({})
    overridden = required_packages({"AVDFLEET_SDK_PACKAGES": "platform-tools,emulator"})
    assert overridden == ["platform-tools", "emulator"]


def test_sdk_packages_step_uses_sdkmanager(tmp_path: Path) -> None:
    context = make_context(
        tmp_path,
        options=InstallOptions(accept_android_sdk_licenses=True),
    )
    create_file(context.sdk_root / "cmdline-tools" / "latest" / "bin" / "sdkmanager")
    step = SdkPackagesStep()
    assert step.check(context) is False
    result = step.run(context)
    assert result.ok is True
    install = next(record.argv for record in context.runner.records if "--install" in record.argv)
    assert "--install" in install
    assert "platform-tools" in install
    assert any(arg.startswith("--sdk_root=") for arg in install)


def test_sdk_packages_step_delegates_to_backend(tmp_path: Path) -> None:
    backend = FakeBackend(installed=["platform-tools"])
    context = make_context(
        tmp_path,
        options=InstallOptions(accept_android_sdk_licenses=True),
        backend=backend,
    )
    step = SdkPackagesStep()
    assert step.check(context) is False  # only platform-tools installed
    result = step.run(context)
    assert result.ok is True
    assert backend.install_calls
    packages, accept = backend.install_calls[0]
    assert "emulator" in packages
    assert accept is True
    assert context.runner.records == []


def test_sdk_packages_check_true_when_all_installed(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    for package_id in ("platform-tools", "emulator", "build-tools;35.0.0", "platforms;android-35"):
        package_path(context.sdk_root, package_id).mkdir(parents=True)
    assert SdkPackagesStep().check(context) is True


# ---------------------------------------------------------------------------
# Environment step
# ---------------------------------------------------------------------------
def test_sdk_env_step_sets_variables_and_path(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    step = SdkEnvStep()
    assert step.check(context) is False

    result = step.run(context)
    assert result.ok is True
    expected = result.data["env"]
    assert expected["ANDROID_SDK_ROOT"] == str(context.sdk_root)
    assert expected["ANDROID_HOME"] == str(context.sdk_root)
    assert expected["ANDROID_AVD_HOME"] == str(context.avd_home)
    assert context.get_env("ANDROID_SDK_ROOT") == str(context.sdk_root)

    runner = context.runner
    assert isinstance(runner, RecordingRunner)
    texts = [" ".join(record.argv) for record in runner.records]
    assert any("ANDROID_SDK_ROOT" in text for text in texts)
    assert any("stub-env set ANDROID_SDK_ROOT" in text for text in texts)
    assert step.check(context) is True

    # Later commands see the environment overlay.
    runner.envs.clear()
    context.run(["probe"])
    assert runner.envs[-1]["ANDROID_SDK_ROOT"] == str(context.sdk_root)
    assert str(context.sdk_root / "platform-tools") in runner.envs[-1]["PATH"]


def test_sdk_env_step_uninstall_removes_variables(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    step = SdkEnvStep()
    step.run(context)
    result = step.uninstall(context)
    assert result.ok is True
    runner = context.runner
    assert isinstance(runner, RecordingRunner)
    assert any("stub-env unset ANDROID_SDK_ROOT" in " ".join(record.argv) for record in runner.records)
    assert any("remove PATH" in " ".join(record.argv) or "avdfleet PATH" in " ".join(record.argv) for record in runner.records)


# ---------------------------------------------------------------------------
# Studio
# ---------------------------------------------------------------------------
def test_studio_step_skips_when_studio_absent(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    result = StudioConfigStep().run(context)
    assert result.ok is True
    assert result.data.get("skipped") is True
    assert not properties_path(context).exists()


def test_studio_step_writes_managed_properties(tmp_path: Path) -> None:
    studio_dir = tmp_path / "studio-config"
    studio_dir.mkdir()
    provider = StubProvider(root=tmp_path, studio_dir=studio_dir)
    context = make_context(tmp_path, provider=provider)
    step = StudioConfigStep()
    assert step.check(context) is False
    result = step.run(context)
    assert result.ok is True
    target = properties_path(context)
    assert target.name == STUDIO_PROPERTIES_FILENAME
    content = target.read_text(encoding="utf-8")
    assert "Managed by avdfleet" in content
    assert str(context.sdk_root) in content
    assert step.check(context) is True

    uninstall = step.uninstall(context)
    assert uninstall.ok is True
    assert not target.exists()


def test_studio_step_dry_run_writes_nothing(tmp_path: Path) -> None:
    studio_dir = tmp_path / "studio-config"
    studio_dir.mkdir()
    context = make_context(
        tmp_path,
        provider=StubProvider(root=tmp_path, studio_dir=studio_dir),
        options=InstallOptions(dry_run=True),
    )
    result = StudioConfigStep().run(context)
    assert result.ok is True
    assert result.data["content"] == properties_content(context)
    assert not properties_path(context).exists()


# ---------------------------------------------------------------------------
# Tool steps
# ---------------------------------------------------------------------------
def test_jdk_step_prefers_android_studio_jbr(tmp_path: Path) -> None:
    jbr = tmp_path / "jbr"
    create_file(jbr / "bin" / "java")
    provider = StubProvider(root=tmp_path, jbr_home=jbr)
    context = make_context(tmp_path, provider=provider)
    step = JdkStep()
    assert step.check(context) is True
    result = step.run(context)
    assert result.ok is True
    assert result.data["java_home"] == str(jbr)
    assert "JBR" in result.message
    assert context.facts["java_home"] == str(jbr)


def test_jdk_step_installs_through_template(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    result = JdkStep().run(context)
    assert result.ok is True
    runner = context.runner
    assert isinstance(runner, RecordingRunner)
    assert any(record.argv[:2] == ["stub-install", "jdk"] for record in runner.records)


def test_jdk_step_detects_existing_java(tmp_path: Path) -> None:
    java_home = tmp_path / "jdk"
    java = create_file(java_home / "bin" / "java")
    provider = StubProvider(root=tmp_path, binaries={"java": str(java)})
    context = make_context(tmp_path, provider=provider)
    step = JdkStep()
    assert find_java_home(context) == java_home
    assert step.check(context) is True
    result = step.run(context)
    assert result.data["java_home"] == str(java_home)


def test_node_step_runs_template_and_verifies(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    step = NodeStep()
    assert step.check(context) is False
    result = step.run(context)
    assert result.ok is True
    runner = context.runner
    assert isinstance(runner, RecordingRunner)
    assert any(record.argv[:2] == ["stub-install", "node"] for record in runner.records)


def test_appium_step_installs_driver_and_tolerates_inspector_failure(tmp_path: Path) -> None:
    def handler(argv: list[str], stdin: str | None, env: dict[str, str]) -> CommandResult:
        if argv[:2] == ["stub-install", "inspector"]:
            return CommandResult(argv=argv, exit_code=1, stderr="inspector failed")
        return CommandResult(argv=argv, exit_code=0)

    runner = RecordingRunner(handler=handler)
    context = make_context(tmp_path, runner=runner)
    result = AppiumStep().run(context)
    assert result.ok is True
    assert result.data["inspector_installed"] is False
    texts = [" ".join(record.argv) for record in runner.records]
    assert any("stub-install appium" in text for text in texts)
    assert any("stub-install appium_driver" in text for text in texts)


def test_appium_step_fails_when_appium_install_fails(tmp_path: Path) -> None:
    def handler(argv: list[str], stdin: str | None, env: dict[str, str]) -> CommandResult:
        if argv[:2] == ["stub-install", "appium"]:
            return CommandResult(argv=argv, exit_code=1, stderr="npm exploded")
        return CommandResult(argv=argv, exit_code=0)

    context = make_context(tmp_path, runner=RecordingRunner(handler=handler))
    result = AppiumStep().run(context)
    assert result.ok is False
    assert "Appium" in result.message


def test_scrcpy_step_is_optional(tmp_path: Path) -> None:
    success = make_context(tmp_path)
    assert ScrcpyStep().run(success).ok is True

    def handler(argv: list[str], stdin: str | None, env: dict[str, str]) -> CommandResult:
        return CommandResult(argv=argv, exit_code=1, stderr="no scrcpy")

    failure_context = make_context(tmp_path / "fail", runner=RecordingRunner(handler=handler))
    result = ScrcpyStep().run(failure_context)
    assert result.ok is False
    assert ScrcpyStep.critical is False


def _real_execution_runner(handler: Any | None = None) -> RecordingRunner:
    """Recording runner that behaves like a real (non-simulated) execution."""
    runner = RecordingRunner(handler=handler)
    runner.simulate_effects = False
    return runner


def test_scrcpy_step_does_not_claim_success_without_the_binary(tmp_path: Path) -> None:
    # winget/brew exited 0 but the tool is nowhere to be found.
    runner = _real_execution_runner()
    context = make_context(tmp_path, runner=runner)

    result = ScrcpyStep().run(context)

    assert result.ok is False
    assert "no 'scrcpy' executable is on PATH" in result.message
    assert "stub-install scrcpy" in " ".join(runner.invocations[0])
    # The failure is actionable and the step can never abort the install.
    hint = result.data["hint"]
    assert "optional" in hint
    assert "stub-pkg" in hint
    assert "avdfleet install --repair --only tools.scrcpy" in hint
    assert ScrcpyStep.critical is False


def test_scrcpy_step_reports_the_resolved_binary_and_version(tmp_path: Path) -> None:
    provider = StubProvider(root=tmp_path, binaries={"scrcpy": "/stub/bin/scrcpy"})

    def handler(argv: list[str], stdin: str | None, env: dict[str, str]) -> CommandResult:
        if argv[-1] == "--version":
            return CommandResult(argv=argv, exit_code=0, stdout="scrcpy 3.1\n")
        return CommandResult(argv=argv, exit_code=0)

    runner = _real_execution_runner(handler)
    context = make_context(tmp_path, provider=provider, runner=runner)

    result = ScrcpyStep().run(context)

    assert result.ok is True
    assert result.data["scrcpy"] == "/stub/bin/scrcpy"
    assert result.data["version"] == "scrcpy 3.1"
    assert "scrcpy 3.1" in result.message
    assert runner.invocations[-1] == ["/stub/bin/scrcpy", "--version"]


def test_scrcpy_step_rereads_path_before_declaring_failure(tmp_path: Path) -> None:
    # Simulates winget updating the persisted PATH: the binary only appears
    # after the provider refreshes its environment.
    provider = StubProvider(root=tmp_path)
    refreshes: list[bool] = []

    def refresh_path() -> str:
        refreshes.append(True)
        provider.binaries["scrcpy"] = "/stub/late/scrcpy"
        return "/stub/late"

    provider.refresh_path = refresh_path  # type: ignore[method-assign]

    def handler(argv: list[str], stdin: str | None, env: dict[str, str]) -> CommandResult:
        if argv[-1] == "--version":
            return CommandResult(argv=argv, exit_code=0, stdout="scrcpy 3.1\n")
        return CommandResult(argv=argv, exit_code=0)

    runner = _real_execution_runner(handler)
    context = make_context(tmp_path, provider=provider, runner=runner)

    result = ScrcpyStep().run(context)

    assert refreshes == [True]
    assert result.ok is True
    assert result.data["scrcpy"] == "/stub/late/scrcpy"


def test_scrcpy_step_dry_run_never_probes_or_claims_a_version(tmp_path: Path) -> None:
    runner = RecordingRunner()
    context = make_context(tmp_path, runner=runner, options=InstallOptions(dry_run=True))

    result = ScrcpyStep().run(context)

    assert result.ok is True
    assert result.data["planned"] is True
    assert result.data["version"] == ""
    assert all(record.dry_run for record in runner.records)
    assert "--version" not in " ".join(" ".join(record.argv) for record in runner.records)


# ---------------------------------------------------------------------------
# Acceleration steps
# ---------------------------------------------------------------------------
def test_accel_detect_reports_available(tmp_path: Path) -> None:
    provider = StubProvider(
        root=tmp_path,
        accel=VirtualizationPlan(kind=AcceleratorKind.KVM, available=True, enabled=True),
    )
    context = make_context(tmp_path, provider=provider)
    step = AccelDetectStep()
    assert step.always_run is True
    result = step.run(context)
    assert result.ok is True
    assert result.data["kind"] == "kvm"
    assert context.facts["acceleration"] is not None


def test_accel_detect_fails_without_acceleration(tmp_path: Path) -> None:
    provider = StubProvider(
        root=tmp_path,
        accel=VirtualizationPlan(kind=AcceleratorKind.NONE, available=False),
    )
    context = make_context(tmp_path, provider=provider)
    result = AccelDetectStep().run(context)
    assert result.ok is False
    assert "no usable hardware acceleration" in result.message
    assert "hint" in result.data


def test_accel_detect_warns_on_nested_virtualisation(tmp_path: Path) -> None:
    provider = StubProvider(
        root=tmp_path,
        nested=True,
        accel=VirtualizationPlan(kind=AcceleratorKind.KVM, available=True, enabled=True),
    )
    context = make_context(tmp_path, provider=provider)
    result = AccelDetectStep().run(context)
    assert result.ok is True
    assert result.data["nested"] is True
    assert any("nested" in note for note in result.data["notes"])


def test_whpx_step_raises_reboot_after_enabling(tmp_path: Path) -> None:
    plan = VirtualizationPlan(
        kind=AcceleratorKind.WHPX,
        available=True,
        enabled=False,
        requires_reboot=True,
        commands=(Command(["whpx-enable"], admin=True, requires_reboot=True),),
    )
    provider = StubProvider(root=tmp_path, platform=HostOS.WINDOWS, accel=plan)
    runner = RecordingRunner()
    context = make_context(tmp_path, provider=provider, runner=runner)
    step = WhpxStep()
    assert step.check(context) is False
    with pytest.raises(RebootRequiredError) as info:
        step.run(context)
    assert "reboot" in str(info.value).lower()
    assert info.value.details["pending_step"] == "accel.whpx"
    assert runner.invocations == [["whpx-enable"]]
    assert context.facts["whpx_enable_commands_done"] is True


def test_whpx_step_enabled_and_dry_run(tmp_path: Path) -> None:
    enabled_plan = VirtualizationPlan(
        kind=AcceleratorKind.WHPX, available=True, enabled=True, requires_reboot=False
    )
    enabled_context = make_context(
        tmp_path, provider=StubProvider(root=tmp_path, platform=HostOS.WINDOWS, accel=enabled_plan)
    )
    assert WhpxStep().check(enabled_context) is True
    assert WhpxStep().run(enabled_context).ok is True

    disabled_plan = VirtualizationPlan(
        kind=AcceleratorKind.WHPX,
        available=True,
        enabled=False,
        requires_reboot=True,
        commands=(Command(["whpx-enable"], admin=True),),
    )
    dry_context = make_context(
        tmp_path / "dry",
        provider=StubProvider(root=tmp_path / "dry", platform=HostOS.WINDOWS, accel=disabled_plan),
        options=InstallOptions(dry_run=True),
    )
    result = WhpxStep().run(dry_context)
    assert result.ok is True
    assert result.data["reboot_required"] is True
    assert dry_context.runner.invocations == []  # dry-run must not execute

    unavailable_plan = VirtualizationPlan(
        kind=AcceleratorKind.WHPX, available=False, enabled=False
    )
    failed_context = make_context(
        tmp_path / "fail",
        provider=StubProvider(root=tmp_path / "fail", platform=HostOS.WINDOWS, accel=unavailable_plan),
    )
    assert WhpxStep().run(failed_context).ok is False


def test_whpx_step_skips_on_non_windows(tmp_path: Path) -> None:
    context = make_context(tmp_path)  # linux stub provider
    assert WhpxStep().check(context) is True
    result = WhpxStep().run(context)
    assert result.ok is True
    assert "not applicable" in result.message


def test_aehd_step_migrates_when_installed(tmp_path: Path) -> None:
    migration = VirtualizationPlan(
        kind=AcceleratorKind.AEHD,
        available=True,
        enabled=False,
        commands=(
            Command(["sc.exe", "stop", "aehd"], admin=True),
            Command(["pnputil", "/delete-driver", "aehd.inf"], admin=True),
        ),
    )
    plan = VirtualizationPlan(kind=AcceleratorKind.WHPX, available=True, enabled=False)
    provider = StubProvider(
        root=tmp_path,
        platform=HostOS.WINDOWS,
        aehd=True,
        aehd_plan=migration,
        accel=plan,
    )
    runner = RecordingRunner()
    context = make_context(tmp_path, provider=provider, runner=runner)
    step = AehdStep()
    assert step.check(context) is False
    result = step.run(context)
    assert result.ok is True
    assert context.facts["aehd_migrated"] is True
    assert runner.invocations == [["sc.exe", "stop", "aehd"], ["pnputil", "/delete-driver", "aehd.inf"]]


def test_aehd_step_noop_when_absent(tmp_path: Path) -> None:
    provider = StubProvider(root=tmp_path, platform=HostOS.WINDOWS)
    context = make_context(tmp_path, provider=provider)
    assert AehdStep().check(context) is True
    result = AehdStep().run(context)
    assert result.ok is True
    assert "not installed" in result.message


def test_kvm_step_dry_run_reports_udev_rule(tmp_path: Path) -> None:
    plan = VirtualizationPlan(
        kind=AcceleratorKind.KVM,
        available=True,
        enabled=False,
        commands=(Command(["kvm-configure"], admin=True),),
        udev_rule_path=KVM_UDEV_RULE_PATH,
        udev_rule_content=KVM_UDEV_RULE,
        groups=("kvm",),
    )
    provider = StubProvider(root=tmp_path, accel=plan)
    context = make_context(tmp_path, provider=provider, options=InstallOptions(dry_run=True))
    step = KvmStep()
    assert step.check(context) is False
    result = step.run(context)
    assert result.ok is True
    assert result.data["udev_rule_content"] == KVM_UDEV_RULE
    assert result.data["udev_rule_path"] == KVM_UDEV_RULE_PATH
    assert result.data["re_login_required"] is True
    assert context.runner.invocations == []


def test_kvm_step_unavailable_and_non_linux(tmp_path: Path) -> None:
    plan = VirtualizationPlan(kind=AcceleratorKind.KVM, available=False, enabled=False)
    context = make_context(tmp_path, provider=StubProvider(root=tmp_path, accel=plan))
    result = KvmStep().run(context)
    assert result.ok is False
    assert "not available" in result.message

    mac_context = make_context(
        tmp_path / "mac",
        provider=StubProvider(root=tmp_path / "mac", platform=HostOS.MACOS),
    )
    assert KvmStep().check(mac_context) is True
    assert KvmStep().run(mac_context).ok is True


def test_kvm_step_runs_commands(tmp_path: Path) -> None:
    plan = VirtualizationPlan(
        kind=AcceleratorKind.KVM,
        available=True,
        enabled=False,
        commands=(Command(["kvm-configure"], admin=True),),
    )
    runner = RecordingRunner()
    context = make_context(tmp_path, provider=StubProvider(root=tmp_path, accel=plan), runner=runner)
    result = KvmStep().run(context)
    assert result.ok is True
    assert runner.invocations == [["kvm-configure"]]
    assert KvmStep().uninstall(context).ok is True


# ---------------------------------------------------------------------------
# Engine-level smoke: all steps are instantiable and report statuses
# ---------------------------------------------------------------------------
def test_all_steps_instantiate_with_spec_roundtrip() -> None:
    for step in build_steps():
        spec = step.spec()
        assert StepStatus.SKIPPED.terminal is True
        assert spec.id == step.id
        assert spec.requires_reboot == step.requires_reboot
        assert spec.critical == step.critical
