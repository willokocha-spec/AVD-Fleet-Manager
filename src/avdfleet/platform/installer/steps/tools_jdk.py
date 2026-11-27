"""Step: provide a JDK (system package or Android Studio's bundled JBR).

``sdkmanager`` needs a Java runtime.  avdfleet prefers the JBR that ships with
Android Studio when it is present (no extra install), otherwise installs a
JDK 21 through the provider and records ``JAVA_HOME`` for the later
environment/step configuration.
"""

from __future__ import annotations

from pathlib import Path

from avdfleet.core.models import OperationResult

from .base import BaseStep

#: The Android SDK tools require at least JDK 17; Android Studio bundles JBR 21.
MIN_JAVA_MAJOR = 17


def java_binary_name(platform: str) -> str:
    return "java.exe" if platform == "windows" else "java"


def _jdk_child_dirs(platform: str, root: Path) -> list[Path]:
    """Expand a JDK installation root into candidate JAVA_HOMEs."""
    try:
        if not root.is_dir():
            return []
        if platform == "macos":
            return sorted(root.glob("*/Contents/Home"))
        return sorted(path for path in root.glob("*") if path.is_dir())
    except OSError:  # pragma: no cover - unreadable install root
        return []


def candidate_java_homes(context) -> list[Path]:
    """Candidate JAVA_HOMEs in preference order (best effort)."""
    platform = context.provider.platform.value
    candidates: list[Path] = []
    env_java_home = context.env.get("JAVA_HOME") or context.provider.env_value("JAVA_HOME")
    if env_java_home:
        candidates.append(Path(env_java_home))
    jbr = context.provider.studio_jbr_home()
    if jbr is not None:
        candidates.append(jbr)
    java = context.which(java_binary_name(platform))
    if java:
        candidates.append(Path(java).parent.parent)
    for root in context.provider.java_home_roots():
        candidates.extend(_jdk_child_dirs(platform, root))
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def find_java_home(context) -> Path | None:
    """Return the first candidate that actually contains ``bin/java``."""
    binary = java_binary_name(context.provider.platform.value)
    for candidate in candidate_java_homes(context):
        if (candidate / "bin" / binary).is_file():
            return candidate
    return None


class JdkStep(BaseStep):
    id = "tools.jdk"
    title = "Install a JDK for the Android SDK tools"
    description = (
        "Use Android Studio's bundled JBR when available, otherwise install JDK 21 "
        "through the host package manager."
    )
    phase = "tools"
    critical = True
    depends_on = ()

    def check(self, context) -> bool:
        if context.facts.get("java_home"):
            return True
        return find_java_home(context) is not None

    def run(self, context) -> OperationResult:
        jbr = context.provider.studio_jbr_home()
        if jbr is not None and (jbr / "bin" / java_binary_name(context.provider.platform.value)).is_file():
            return self._adopt(context, jbr, source="Android Studio JBR")

        existing = find_java_home(context)
        if existing is not None:
            return self._adopt(context, existing, source="existing JDK")

        failure = self.bootstrap_package_manager(context)
        if failure is not None:
            return failure

        commands = context.tool_commands("jdk")
        if not commands:
            return self.failed(
                f"no JDK install command is defined for {context.provider.id}",
                hint="install JDK 21 manually and re-run 'avdfleet install --repair'",
            )
        if any(command.admin for command in commands):
            failure = self.check_admin(context, "installing a JDK")
            if failure is not None:
                return failure
        ok, message, results = self.run_commands(context, commands, what="installing the JDK")
        if not ok:
            return self.failed(
                message,
                hint=f"install JDK 21 with {context.provider.package_manager} manually, then repair",
                commands=self.command_lines(results),
            )

        installed = find_java_home(context)
        if installed is None:
            if context.dry_run or context.simulated:
                return self.done(
                    "would install JDK 21 (dry-run/recording mode)",
                    commands=self.command_lines(results),
                )
            return self.failed(
                "JDK installation finished but no java executable was found",
                hint="set JAVA_HOME to a JDK 21 home and re-run 'avdfleet install --repair'",
                commands=self.command_lines(results),
            )
        return self._adopt(context, installed, source="installed JDK", commands=self.command_lines(results))

    def _adopt(
        self,
        context,
        java_home: Path,
        *,
        source: str,
        commands: list[list[str]] | None = None,
    ) -> OperationResult:
        context.facts["java_home"] = str(java_home)
        context.env["JAVA_HOME"] = str(java_home)
        version = ""
        if not context.dry_run and not context.simulated:
            java = java_home / "bin" / java_binary_name(context.provider.platform.value)
            probe = context.run([str(java), "-version"], timeout_s=60)
            version = self.first_line(probe.stderr or probe.stdout)
            major = self.version_major(version)
            if major is not None and major < MIN_JAVA_MAJOR:
                context.warning(
                    f"JAVA_HOME points at Java {major}, but the Android SDK tools need at least "
                    f"Java {MIN_JAVA_MAJOR}: {java_home}"
                )
        return self.done(
            f"using {source} at {java_home}",
            java_home=str(java_home),
            java_version=version,
            commands=commands or [],
        )

    def uninstall(self, context) -> OperationResult:
        return self.done("the JDK is a system package and is left installed")


__all__ = [
    "MIN_JAVA_MAJOR",
    "JdkStep",
    "candidate_java_homes",
    "find_java_home",
    "java_binary_name",
]
