"""Provider registry and host-family detection.

The installer never guesses from ``sys.platform`` alone on Linux: Debian,
Fedora and Arch need different package managers.  Detection order is

1. explicit ``provider_id`` (``avdfleet install --platform-provider``),
2. ``AVDFLEET_INSTALL_PROVIDER``,
3. the fakes' ``AVDFLEET_FAKE_HOST_OS`` override (fakes-spec §8),
4. the real OS + ``/etc/os-release`` (or an injected ``os_release`` text),
5. package-manager probing via the injected ``command_exists`` callable.

Unknown values raise :class:`avdfleet.core.errors.ValidationError` so the CLI can
show a helpful message instead of silently installing the wrong package manager.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from avdfleet.core.errors import ValidationError

from .arch import ArchInstallProvider
from .base import (
    KVM_UDEV_RULE,
    KVM_UDEV_RULE_PATH,
    REQUIRED_TOOL_TEMPLATES,
    Command,
    EnvScope,
    InstallProvider,
    ToolTemplate,
    VirtualizationPlan,
)
from .debian import DebianInstallProvider
from .fedora import FedoraInstallProvider
from .macos import MacOSInstallProvider
from .windows import WindowsInstallProvider

PROVIDERS: dict[str, type[InstallProvider]] = {
    "windows": WindowsInstallProvider,
    "macos": MacOSInstallProvider,
    "debian": DebianInstallProvider,
    "fedora": FedoraInstallProvider,
    "arch": ArchInstallProvider,
}

PROVIDER_IDS: tuple[str, ...] = tuple(PROVIDERS)

#: Convenience aliases accepted from the CLI/config/environment.
PROVIDER_ALIASES: dict[str, str] = {
    "win": "windows",
    "windows11": "windows",
    "win11": "windows",
    "mac": "macos",
    "macosx": "macos",
    "osx": "macos",
    "darwin": "macos",
    "ubuntu": "debian",
    "mint": "debian",
    "pop": "debian",
    "linuxmint": "debian",
    "rhel": "fedora",
    "rocky": "fedora",
    "rockylinux": "fedora",
    "centos": "fedora",
    "almalinux": "fedora",
    "amzn": "fedora",
    "manjaro": "arch",
    "endeavouros": "arch",
    "linux": "debian",
}

_RHEL_LIKE = {
    "fedora",
    "rhel",
    "centos",
    "rocky",
    "almalinux",
    "amzn",
    "ol",
    "oracle",
    "scientific",
}
_DEBIAN_LIKE = {"debian", "ubuntu", "linuxmint", "mint", "pop", "elementary", "neon", "raspbian"}
_ARCH_LIKE = {"arch", "manjaro", "endeavouros", "artix", "garuda"}

ENV_PROVIDER = "AVDFLEET_INSTALL_PROVIDER"
ENV_FAKE_HOST_OS = "AVDFLEET_FAKE_HOST_OS"
ENV_OS_RELEASE = "AVDFLEET_OS_RELEASE"


def provider_ids() -> tuple[str, ...]:
    return PROVIDER_IDS


def provider_class(name: str) -> type[InstallProvider]:
    """Resolve a provider id/alias to its class (raises ``ValidationError``)."""
    resolved = resolve_provider_name(name)
    return PROVIDERS[resolved]


def resolve_provider_name(name: str | None) -> str:
    if not name:
        raise ValidationError(
            "no install provider selected",
            hint=f"choose one of: {', '.join(PROVIDER_IDS)}",
        )
    candidate = name.strip().lower()
    candidate = PROVIDER_ALIASES.get(candidate, candidate)
    if candidate not in PROVIDERS:
        raise ValidationError(
            f"unknown install provider: {name!r}",
            hint=f"supported providers: {', '.join(PROVIDER_IDS)}",
            details={"providers": list(PROVIDER_IDS)},
        )
    return candidate


def linux_provider_from_os_release(text: str) -> str | None:
    """Map an ``/etc/os-release`` document to a provider id (``None`` if unknown)."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip().upper()] = value.strip().strip('"').strip("'")
    tokens: set[str] = set()
    for field in ("ID", "ID_LIKE"):
        for token in values.get(field, "").replace(",", " ").split():
            tokens.add(token.lower())
    if tokens & _ARCH_LIKE:
        return "arch"
    if tokens & _RHEL_LIKE or "rhel" in tokens:
        return "fedora"
    if tokens & _DEBIAN_LIKE:
        return "debian"
    return None


def _read_os_release(environ: Mapping[str, str]) -> str | None:
    override = environ.get(ENV_OS_RELEASE)
    if override:
        return override
    try:
        path = Path("/etc/os-release")
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - defensive
        return None
    return None


def detect_provider_id(
    *,
    provider_id: str | None = None,
    environ: Mapping[str, str] | None = None,
    os_release: str | None = None,
    command_exists: Callable[[str], bool] | None = None,
) -> str:
    """Detect the install provider id for this host."""
    env = dict(os.environ if environ is None else environ)
    if provider_id:
        return resolve_provider_name(provider_id)
    if env.get(ENV_PROVIDER):
        return resolve_provider_name(env[ENV_PROVIDER])

    fake_os = (env.get(ENV_FAKE_HOST_OS) or "").strip().lower()
    host_os = fake_os
    if not host_os:
        if sys.platform == "win32":
            host_os = "windows"
        elif sys.platform == "darwin":
            host_os = "macos"
        else:
            host_os = "linux"
    if host_os in {"windows", "win32"}:
        return "windows"
    if host_os in {"macos", "darwin", "mac"}:
        return "macos"
    if host_os not in {"linux", "unknown"}:
        raise ValidationError(
            f"unknown host OS for installer: {host_os!r}",
            hint="use --platform-provider windows|macos|debian|fedora|arch",
        )

    text = os_release if os_release is not None else _read_os_release(env)
    if text:
        detected = linux_provider_from_os_release(text)
        if detected:
            return detected
    if command_exists is not None:
        if command_exists("apt-get"):
            return "debian"
        if command_exists("dnf"):
            return "fedora"
        if command_exists("pacman"):
            return "arch"
    # Last resort: Debian-family is the most common CI/container base.
    return "debian"


def get_provider(
    name: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    is_admin: bool | None = None,
    which: Callable[[str], str | None] | None = None,
    arch: Any | None = None,
    os_release: str | None = None,
    **provider_kwargs: Any,
) -> InstallProvider:
    """Instantiate the provider for *name* (detected when omitted).

    Extra keyword arguments are forwarded to the concrete provider so callers
    (and tests) can inject host state such as ``features=`` or ``kvm_available_now=``.
    """
    env = dict(environ) if environ is not None else None
    resolved = detect_provider_id(
        provider_id=name,
        environ=env,
        os_release=os_release,
        command_exists=(lambda binary: which(binary) is not None) if which is not None else None,
    )
    cls = PROVIDERS[resolved]
    return cls(env=env, is_admin=is_admin, which=which, arch=arch, **provider_kwargs)


#: Alias used by container/CLI wiring code.
create_provider = get_provider


__all__ = [
    "KVM_UDEV_RULE",
    "KVM_UDEV_RULE_PATH",
    "PROVIDERS",
    "PROVIDER_ALIASES",
    "PROVIDER_IDS",
    "REQUIRED_TOOL_TEMPLATES",
    "Command",
    "EnvScope",
    "InstallProvider",
    "ToolTemplate",
    "VirtualizationPlan",
    "create_provider",
    "detect_provider_id",
    "get_provider",
    "linux_provider_from_os_release",
    "provider_class",
    "provider_ids",
    "resolve_provider_name",
]
