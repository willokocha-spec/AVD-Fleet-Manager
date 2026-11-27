"""Pytest fixtures exposing the executable fakes to the whole test suite.

Registered from ``tests/conftest.py`` via ``pytest_plugins``.  Fixture surface
(fakes-spec section 5):

* ``fake_tools`` (session) - :class:`tests.fakes.runtime.FakeTools`; shims are
  created once per session.
* ``fake_env`` (function) - prepends the shim directory to ``PATH`` and sets
  ``ANDROID_SDK_ROOT`` / ``ANDROID_HOME`` / ``ANDROID_AVD_HOME`` / ``JAVA_HOME``
  and every ``AVDFLEET_FAKE_*`` variable; returns the ``FakeTools`` object.
* ``fake_state`` (function) - mutable, typed accessor over the fake state.
* ``fake_sdk`` (function) - a ``core.models.SdkPaths`` rooted in the fake SDK.
* ``fake_installer_context`` (function) - isolated ``Config``/``Paths`` under
  ``tmp_path`` with the fake tools on ``PATH``.

The fixtures intentionally import ``avdfleet.core`` lazily so collecting or
importing this plugin stays cheap.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from .runtime import (
    DEFAULT_DEVICE,
    LAUNCHER_ACTIVITY,
    SETUP_WIZARD_ACTIVITY,
    FakeState,
    FakeTools,
    build_avd_entry,
    build_fake_tools,
    canonical_device,
    grpc_port_for,
    mutate_state,
    next_console_port,
    write_avd_files,
)

__all__ = [
    "FakeInstallerContext",
    "FakeStateHelper",
    "fake_env",
    "fake_installer_context",
    "fake_sdk",
    "fake_state",
    "fake_tools",
]


# ---------------------------------------------------------------------------
# Session-scoped FakeTools
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def fake_tools(tmp_path_factory: pytest.TempPathFactory) -> Iterator[FakeTools]:
    """Session-scoped factory that builds the shims once and seeds state."""
    # A space in the root deliberately exercises path quoting on every shim.
    root = tmp_path_factory.mktemp("avdfleet fakes")
    # The fake catalogue deliberately simulates a deterministic x86_64 device
    # lab even on an ARM64 CI runner: system images, product names and ABI
    # assertions are written for that catalogue.  Host-architecture behaviour
    # is covered separately with synthetic HostInfo in the doctor tests.
    tools = build_fake_tools(root, arch="x86_64")
    try:
        yield tools
    finally:
        tools.cleanup()


@pytest.fixture()
def _fake_reset(fake_tools: FakeTools) -> Iterator[None]:
    """Function-scoped reset so each test starts from the seeded catalogue."""
    fake_tools.reset()
    yield
    fake_tools.cleanup_processes()


# ---------------------------------------------------------------------------
# Environment fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_env(fake_tools: FakeTools, monkeypatch: pytest.MonkeyPatch, _fake_reset: None) -> FakeTools:
    """Activate the fake toolchain for the current test.

    Usage::

        def test_boots(fake_env, fake_state):
            fake_state.seed_avds()
            subprocess.run(["adb", "devices"], check=True, text=True)
    """
    for key, value in fake_tools.env.items():
        monkeypatch.setenv(key, value)
    return fake_tools


# ---------------------------------------------------------------------------
# Mutable state helper
# ---------------------------------------------------------------------------


class FakeStateHelper:
    """Typed convenience wrapper around ``$AVDFLEET_FAKE_HOME/state.json``."""

    def __init__(self, tools: FakeTools) -> None:
        self.tools = tools

    # -- raw access --------------------------------------------------------
    @property
    def path(self) -> Path:
        return self.tools.state

    def read(self) -> FakeState:
        return self.tools.read_state()

    @property
    def raw(self) -> FakeState:
        return self.read()

    def update(self, **updates: Any) -> FakeState:
        return self.tools.update_state(updates)

    # -- accessors ---------------------------------------------------------
    def installed(self) -> list[str]:
        installed = self.read().get("installed") or []
        return [str(item) for item in installed]

    def avds(self) -> dict[str, dict[str, Any]]:
        avds = self.read().get("avds") or {}
        return {str(name): entry for name, entry in avds.items() if isinstance(entry, dict)}

    def avd(self, name: str) -> dict[str, Any]:
        entry = self.avds().get(name)
        if entry is None:
            raise KeyError(f"unknown fake AVD: {name}")
        return entry

    def set_oobe(self, name: str, value: str) -> dict[str, Any]:
        if value not in {"setup", "complete"}:
            raise ValueError("oobe must be 'setup' or 'complete'")
        activity = SETUP_WIZARD_ACTIVITY if value == "setup" else LAUNCHER_ACTIVITY
        return _mutate_entry(self.tools, name, {"oobe": value, "activity": activity})

    def set_avd(self, name: str, **updates: Any) -> dict[str, Any]:
        return _mutate_entry(self.tools, name, updates)

    def commands(self, tool: str | None = None) -> list[dict[str, Any]]:
        return self.tools.commands(tool)

    # -- seeding -----------------------------------------------------------
    def seed_avds(self) -> list[str]:
        return self.tools.seed_avds()

    def add_avd(self, name: str, **kwargs: Any) -> dict[str, Any]:
        """Add an AVD to state (and write its ``.ini``/``config.ini`` files)."""
        force = bool(kwargs.pop("force", False))
        write_files = bool(kwargs.pop("write_files", True))
        package = str(kwargs.pop("package", f"system-images;android-35;google_apis_playstore;{self.tools.host_abi}"))
        device = str(kwargs.pop("device", DEFAULT_DEVICE))
        canonical = canonical_device(device) or DEFAULT_DEVICE
        state = self.read()
        existing = self.avds().get(name)
        if existing is not None and not force:
            raise ValueError(f"fake AVD already exists: {name}")
        entry = build_avd_entry(name, package, canonical, **kwargs)
        if entry.get("state") in {"booting", "running"}:
            if entry.get("console_port") is None:
                console = next_console_port(state)
                entry["console_port"] = console
                entry["adb_port"] = console + 1
                entry["grpc_port"] = grpc_port_for(console)
            entry.setdefault("pid", 424200 + len(self.avds()) + 1)
        if write_files:
            write_avd_files(self.tools.avd_home, entry, path=entry.get("avd_path"))
        if package not in self.installed():
            self.tools.install_image(package)

        def _mutator(current: FakeState) -> dict[str, Any]:
            current.setdefault("avds", {})[name] = entry
            return entry

        return _mutate_state(self.tools, _mutator)

    def add_image(
        self,
        package: str | None = None,
        *,
        api: int = 35,
        tag: str = "google_apis",
        abi: str | None = None,
        install: bool = True,
    ) -> dict[str, Any]:
        """Seed a system image (installs it unless ``install=False``)."""
        resolved = package or f"system-images;android-{api};{tag};{abi or self.tools.host_abi}"
        meta = self.tools.install_image(resolved, install=install)
        return {"package": resolved, **meta}


def _mutate_state(tools: FakeTools, mutator: Any) -> Any:
    return mutate_state(mutator, tools.home)


def _mutate_entry(tools: FakeTools, name: str, updates: dict[str, Any]) -> dict[str, Any]:
    def _mutator(state: FakeState) -> dict[str, Any]:
        entry = state.setdefault("avds", {}).get(name)
        if not isinstance(entry, dict):
            raise KeyError(f"unknown fake AVD: {name}")
        entry.update(updates)
        return entry

    return _mutate_state(tools, _mutator)


@pytest.fixture()
def fake_state(fake_env: FakeTools) -> FakeStateHelper:
    """Mutable typed accessors over the seeded fake state."""
    return FakeStateHelper(fake_env)


# ---------------------------------------------------------------------------
# SdkPaths / installer context
# ---------------------------------------------------------------------------


def _exe_suffix() -> str:
    return ".exe" if sys.platform == "win32" else ""


def _sdk_paths(tools: FakeTools) -> Any:
    from avdfleet.core.models import SdkPaths

    root = tools.sdk_root
    suffix = _exe_suffix()
    return SdkPaths(
        sdk_root=str(root),
        cmdline_tools=str(root / "cmdline-tools" / "latest"),
        platform_tools=str(root / "platform-tools"),
        emulator=str(root / "emulator" / f"emulator{suffix}"),
        build_tools=str(root / "build-tools" / "35.0.0"),
        avd_home=str(tools.avd_home),
        licenses_dir=str(root / "licenses"),
        android_cli=str(root / "cmdline-tools" / "latest" / "bin" / f"android{suffix}"),
        env={
            "ANDROID_SDK_ROOT": str(root),
            "ANDROID_HOME": str(root),
            "ANDROID_AVD_HOME": str(tools.avd_home),
            "JAVA_HOME": str(root / "jdk"),
        },
    )


@pytest.fixture()
def fake_sdk(fake_env: FakeTools) -> Any:
    """``SdkPaths`` pointing at the fake SDK rooted in the fake home."""
    return _sdk_paths(fake_env)


@dataclass
class FakeInstallerContext:
    """Isolated installer ``Config``/``Paths`` with the fake tools on PATH."""

    config: Any
    paths: Any
    env: dict[str, str]
    tools: FakeTools


@pytest.fixture()
def fake_installer_context(fake_env: FakeTools, tmp_path: Path) -> FakeInstallerContext:
    """Isolated ``Config``/``Paths`` under ``tmp_path`` for installer tests."""
    from avdfleet.core.config import Config

    base = tmp_path / "installer"
    (base / "data").mkdir(parents=True, exist_ok=True)
    (base / "config").mkdir(parents=True, exist_ok=True)
    (base / "cache").mkdir(parents=True, exist_ok=True)
    (base / "logs").mkdir(parents=True, exist_ok=True)
    config = Config(
        edition="installed",
        data_dir=str(base / "data"),
        config_dir=str(base / "config"),
        cache_dir=str(base / "cache"),
        log_dir=str(base / "logs"),
        sdk_root=str(fake_env.sdk_root),
        avd_home=str(fake_env.avd_home),
        oobe_matrix_path=str(fake_env.oobe_matrix_path),
        headless=True,
    )
    paths = config.resolved_paths().ensure()
    matrix = FakeInstallerContext(
        config=config,
        paths=paths,
        env=dict(fake_env.env),
        tools=fake_env,
    )
    source = fake_env.oobe_matrix_path
    if source.exists():
        (paths.data_dir / "oobe_matrix.json").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    (paths.data_dir / "installer-context.json").write_text(
        json.dumps({"root": str(base), "sdk_root": str(fake_env.sdk_root)}), encoding="utf-8"
    )
    return matrix
