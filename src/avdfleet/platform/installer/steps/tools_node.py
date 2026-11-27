"""Step: install Node.js (required by Appium)."""

from __future__ import annotations

from avdfleet.core.models import OperationResult

from .base import BaseStep

MIN_NODE_MAJOR = 20


class NodeStep(BaseStep):
    id = "tools.node"
    title = "Install Node.js"
    description = "Install a Node.js LTS runtime and npm (needed by Appium and the Appium Inspector)."
    phase = "tools"
    critical = True
    depends_on = ()

    def check(self, context) -> bool:
        return context.has("node") and context.has("npm")

    def run(self, context) -> OperationResult:
        failure = self.bootstrap_package_manager(context)
        if failure is not None:
            return failure

        commands = context.tool_commands("node")
        if not commands:
            return self.failed(
                f"no Node.js install command is defined for {context.provider.id}",
                hint="install Node.js 20+ manually and re-run 'avdfleet install --repair'",
            )
        if any(command.admin for command in commands):
            failure = self.check_admin(context, "installing Node.js")
            if failure is not None:
                return failure
        ok, message, results = self.run_commands(context, commands, what="installing Node.js")
        if not ok:
            return self.failed(
                message,
                hint=f"install Node.js 20+ with {context.provider.package_manager} manually, then repair",
                commands=self.command_lines(results),
            )

        node = context.which("node")
        if node is None and not context.dry_run and not context.simulated:
            return self.failed(
                "Node.js installation finished but 'node' is not on PATH",
                hint="open a new shell or add the Node.js install directory to PATH, then repair",
                commands=self.command_lines(results),
            )
        version = ""
        required_major = self.version_major(str(getattr(context.config, "node_version", "22"))) or 22
        if node and not context.dry_run and not context.simulated:
            probe = context.run([node, "--version"], timeout_s=60)
            version = self.first_line(probe.stdout or probe.stderr)
            major = self.version_major(version)
            if major is not None and major < max(MIN_NODE_MAJOR, required_major):
                context.warning(
                    f"Node.js {version} is older than the required v{required_major}; "
                    "Appium 4 needs Node 20 or newer."
                )
        return self.done(
            f"Node.js installed{f' ({version})' if version else ''}",
            node=node or "node",
            node_version=version,
            commands=self.command_lines(results),
        )

    def uninstall(self, context) -> OperationResult:
        return self.done("Node.js is a system package and is left installed")


__all__ = ["MIN_NODE_MAJOR", "NodeStep"]
