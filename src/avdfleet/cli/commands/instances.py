"""avdfleet instances - lifecycle, snapshots, screen, config and capacity.

The runtime service is authoritative for live state; the AVD manager owns the
definition.  Listings merge both (as the API does) so a partially wired build
still renders the deployed fleet.
"""

from pathlib import Path
from typing import Any

import typer

from ..context import get_context, invoke_callable, optional_service, service
from ..errors import CliUsageError, command_errors
from ..output import json_flag, jsonable
from .avd import (
    _apply_config,
    _render_instance,
    _render_instances,
    lookup_instance,
    merged_instances,
)

instances_app = typer.Typer(help="Manage AVD instances, snapshots and capacity.", no_args_is_help=True)
snapshots_app = typer.Typer(help="Save, load, list and delete instance snapshots.", no_args_is_help=True)


def _screenshot_path(context: Any, name: str, output: Path | None) -> Path:
    if output is not None:
        target = Path(output)
    else:
        safe = "".join(character if character.isalnum() or character in "._-" else "_" for character in name)
        target = Path(context.paths.screenshots_dir) / f"{safe or 'instance'}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


# ---------------------------------------------------------------------------
# Definition + lifecycle
# ---------------------------------------------------------------------------


def instances_list(
    ctx: typer.Context,
    json_output: bool = json_flag(),
) -> None:
    """List instances (definitions merged with live runtime state)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    _render_instances(out, merged_instances(context), title="instances")


def instances_get(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    json_output: bool = json_flag(),
) -> None:
    """Show one instance."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    _render_instance(out, lookup_instance(context, name), title=f"instance {name}")


def instances_start(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    proxy_id: str = typer.Option(None, "--proxy", help="Proxy id to route this instance through."),
    headless: bool = typer.Option(None, "--headless/--windowed", help="Override headless mode."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and show the launch plan without booting."),
    json_output: bool = json_flag(),
) -> None:
    """Start an instance (optionally through a proxy)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    runtime = service(ctx, "runtime")
    info = invoke_callable(runtime.start, name, proxy_id=proxy_id, headless=headless, dry_run=dry_run)
    _render_instance(out, info, title=f"instance {name} started")


def instances_stop(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    force: bool = typer.Option(False, "--force", help="Kill the emulator if it does not stop in time."),
    timeout_s: float = typer.Option(None, "--timeout", min=0, help="Graceful shutdown timeout in seconds."),
    json_output: bool = json_flag(),
) -> None:
    """Stop an instance."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    runtime = service(ctx, "runtime")
    info = invoke_callable(runtime.stop, name, timeout_s=timeout_s, force=force)
    _render_instance(out, info, title=f"instance {name} stopped")


def instances_restart(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    proxy_id: str = typer.Option(None, "--proxy", help="Proxy id for the restarted instance."),
    json_output: bool = json_flag(),
) -> None:
    """Restart an instance."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    runtime = service(ctx, "runtime")
    info = invoke_callable(runtime.restart, name, proxy_id=proxy_id)
    _render_instance(out, info, title=f"instance {name} restarted")


def instances_wait_boot(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    timeout_s: float = typer.Option(None, "--timeout", min=0.1, help="Boot timeout in seconds."),
    json_output: bool = json_flag(),
) -> None:
    """Wait until an instance has finished booting."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    runtime = service(ctx, "runtime")
    info = invoke_callable(runtime.wait_boot, name, timeout_s=timeout_s)
    _render_instance(out, info, title=f"instance {name} booted")


def instances_reset_oobe(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    strategy: str = typer.Option(None, "--strategy", help="Reset strategy: snapshot, wipe, package or reboot."),
    verify: bool = typer.Option(True, "--verify/--no-verify", help="Verify the Setup Wizard afterwards."),
    json_output: bool = json_flag(),
) -> None:
    """Reset an instance to the out-of-box experience."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    oobe = service(ctx, "oobe")
    result = invoke_callable(oobe.reset, name, strategy=strategy, verify=verify)
    payload = jsonable(result)
    ok = bool(getattr(result, "ok", False))

    def render() -> None:
        (out.success if ok else out.error)(
            f"{name}: OOBE reset {'succeeded' if ok else 'failed'} in "
            f"{getattr(result, 'seconds', '') or '?'}s"
        )
        activity = getattr(result, "activity", "") or ""
        if activity:
            out.info(f"top activity: {activity}")

    out.emit(payload, render)
    if not ok:
        raise typer.Exit(1)


def instances_restart_placeholder() -> None:  # pragma: no cover - kept for docs tooling
    """Unused."""


# ---------------------------------------------------------------------------
# Screen / config / clone / delete
# ---------------------------------------------------------------------------


def instances_screenshot(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    output: Path = typer.Option(None, "-o", "--output", help="Write the PNG here (default: <data_dir>/screenshots)."),
    json_output: bool = json_flag(),
) -> None:
    """Capture a PNG screenshot of a running instance."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    screen = service(ctx, "screen")
    data = invoke_callable(screen.capture, name)
    blob = bytes(data) if isinstance(data, (bytes, bytearray, memoryview)) else b""
    if not blob:
        raise CliUsageError(
            "screenshot capture returned no image data",
            hint="check that the instance is running and screen sharing is available",
        )
    target = _screenshot_path(context, name, output)
    target.write_bytes(blob)
    payload = {"instance": name, "output": str(target), "bytes": len(blob), "ok": True}
    out.emit(payload, lambda: out.success(f"screenshot saved to {target}"))


def instances_config(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    set_values: list[str] = typer.Option(None, "--set", help="Set a config.ini key: --set hw.ramSize=4096."),
    unset_values: list[str] = typer.Option(None, "--unset", help="Remove a config.ini key."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and show the change without writing."),
    json_output: bool = json_flag(),
) -> None:
    """Read or update typed values in an instance's config.ini."""
    _apply_config(
        ctx,
        name=name,
        set_values=set_values,
        unset_values=unset_values,
        dry_run=dry_run,
        json_output=json_output,
    )


def instances_clone(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Source instance name."),
    new_name: str = typer.Option(..., "--name", "-n", help="Name for the clone."),
    json_output: bool = json_flag(),
) -> None:
    """Clone an instance."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    avds = service(ctx, "avds")
    info = invoke_callable(avds.clone_instance, name, new_name)
    _render_instance(out, info, title=f"instance {name} cloned")


def instances_delete(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name to delete."),
    keep_files: bool = typer.Option(False, "--keep-files", help="Keep the AVD files on disk."),
    json_output: bool = json_flag(),
) -> None:
    """Delete an instance (refuses while it is running)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    avds = service(ctx, "avds")
    info = lookup_instance(context, name)
    if bool(getattr(getattr(info, "state", None), "active", False)):
        from avdfleet.core.errors import ConflictError

        raise ConflictError(f"instance {name!r} is running", hint="stop the instance before deleting it")
    invoke_callable(avds.delete_instance, name, delete_files=not keep_files)
    payload = {"deleted": name, "delete_files": not keep_files, "ok": True}
    out.emit(payload, lambda: out.success(f"instance {name!r} deleted"))


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def snapshots_list(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    json_output: bool = json_flag(),
) -> None:
    """List an instance's snapshots."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    runtime = service(ctx, "runtime")
    snapshots = list(invoke_callable(runtime.list_snapshots, name) or [])
    payload = jsonable(snapshots)

    def render() -> None:
        rows = [
            (
                str(getattr(snapshot, "name", "")),
                str(getattr(snapshot, "created_at", "") or ""),
                str(getattr(snapshot, "size_bytes", "") or ""),
                "yes" if getattr(snapshot, "is_golden", False) else "",
            )
            for snapshot in snapshots
        ]
        out.table(("name", "created", "size", "golden"), rows, title=f"snapshots - {name}")
        out.info(f"{len(snapshots)} snapshot(s)")

    out.emit(payload, render)


def snapshots_save(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    snapshot: str = typer.Argument(..., help="Snapshot name."),
    golden: bool = typer.Option(False, "--golden", help="Mark as the golden OOBE snapshot."),
    json_output: bool = json_flag(),
) -> None:
    """Save an instance snapshot."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    runtime = service(ctx, "runtime")
    saved = invoke_callable(runtime.save_snapshot, name, snapshot, golden=golden)
    payload = jsonable(saved)

    def render() -> None:
        out.kv(
            (
                ("instance", name),
                ("snapshot", getattr(saved, "name", snapshot)),
                ("golden", "yes" if getattr(saved, "is_golden", golden) else "no"),
                ("size", getattr(saved, "size_bytes", "") or ""),
            ),
            title="snapshot saved",
        )

    out.emit(payload, render)


def snapshots_load(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    snapshot: str = typer.Argument(..., help="Snapshot name."),
    json_output: bool = json_flag(),
) -> None:
    """Restore an instance snapshot."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    runtime = service(ctx, "runtime")
    loaded = invoke_callable(runtime.load_snapshot, name, snapshot)
    payload = jsonable(loaded)
    out.emit(payload, lambda: out.success(f"loaded snapshot {snapshot!r} into {name!r}"))


def snapshots_delete(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    snapshot: str = typer.Argument(..., help="Snapshot name."),
    json_output: bool = json_flag(),
) -> None:
    """Delete an instance snapshot."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    runtime = service(ctx, "runtime")
    invoke_callable(runtime.delete_snapshot, name, snapshot)
    payload = {"instance": name, "snapshot": snapshot, "deleted": True, "ok": True}
    out.emit(payload, lambda: out.success(f"deleted snapshot {snapshot!r} from {name!r}"))


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------


def instances_capacity(
    ctx: typer.Context,
    json_output: bool = json_flag(),
) -> None:
    """Show fleet capacity (RAM, ports, CPU) for starting more instances."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    runtime = optional_service(ctx, "runtime")
    data: dict[str, Any] = {}
    if runtime is not None:
        capacity_fn = getattr(runtime, "capacity", None)
        if callable(capacity_fn):
            result = invoke_callable(capacity_fn)
            data = jsonable(result) if not isinstance(result, dict) else jsonable(dict(result))
    if not data:
        avds = optional_service(ctx, "avds")
        deployed = []
        if avds is not None and callable(getattr(avds, "list_instances", None)):
            deployed = list(invoke_callable(avds.list_instances) or [])
        data = {
            "max_instances": context.config.max_instances,
            "deployed": len(deployed),
            "running": 0,
            "warnings": ["the runtime capacity service is not available; reporting static values"],
        }

    def render() -> None:
        out.kv(
            [
                (str(key), jsonable(value))
                for key, value in data.items()
                if not isinstance(jsonable(value), (dict, list))
            ],
            title="fleet capacity",
        )
        rows = [
            (str(key), jsonable(value))
            for key, value in data.items()
            if isinstance(jsonable(value), (dict, list))
        ]
        if rows:
            out.kv(rows, title="capacity details")
        for warning in data.get("warnings", []) or []:
            out.warning(str(warning))

    out.emit(data, render)


def register(app: typer.Typer) -> None:
    """Register the instances command group (including snapshots)."""
    instances_app.command("list")(command_errors("instances list")(instances_list))
    instances_app.command("get")(command_errors("instances get")(instances_get))
    instances_app.command("start")(command_errors("instances start")(instances_start))
    instances_app.command("stop")(command_errors("instances stop")(instances_stop))
    instances_app.command("restart")(command_errors("instances restart")(instances_restart))
    instances_app.command("wait-boot")(command_errors("instances wait-boot")(instances_wait_boot))
    instances_app.command("reset-oobe")(command_errors("instances reset-oobe")(instances_reset_oobe))
    instances_app.command("screenshot")(command_errors("instances screenshot")(instances_screenshot))
    instances_app.command("config")(command_errors("instances config")(instances_config))
    instances_app.command("clone")(command_errors("instances clone")(instances_clone))
    instances_app.command("delete")(command_errors("instances delete")(instances_delete))
    instances_app.command("capacity")(command_errors("instances capacity")(instances_capacity))
    snapshots_app.command("list")(command_errors("instances snapshots list")(snapshots_list))
    snapshots_app.command("save")(command_errors("instances snapshots save")(snapshots_save))
    snapshots_app.command("load")(command_errors("instances snapshots load")(snapshots_load))
    snapshots_app.command("delete")(command_errors("instances snapshots delete")(snapshots_delete))
    instances_app.add_typer(snapshots_app, name="snapshots")
    app.add_typer(instances_app, name="instances")


__all__ = [
    "instances_app",
    "register",
    "snapshots_app",
]
