"""``avdfleet instrumentation`` - root, Magisk, Zygisk, LSPosed and modules.

Thin adapter over :class:`avdfleet.instrumentation.InstrumentationService`; all
business logic lives in the service so the REST API, the GUI and this CLI cannot
drift apart.
"""

from __future__ import annotations

from typing import Any

import typer

from ..context import get_context, invoke_callable, service
from ..errors import CliUsageError
from ..output import json_flag, jsonable

instrumentation_app = typer.Typer(
    help="Root, Magisk, Zygisk, LSPosed and module management.",
    no_args_is_help=True,
)
modules_app = typer.Typer(help="The central module library.", no_args_is_help=True)
profiles_app = typer.Typer(help="Reusable instrumentation profiles.", no_args_is_help=True)
toolchain_app = typer.Typer(
    help="Pinned Magisk/LSPosed releases from their official sources.",
    no_args_is_help=True,
)
instrumentation_app.add_typer(modules_app, name="modules")
instrumentation_app.add_typer(profiles_app, name="profiles")
instrumentation_app.add_typer(toolchain_app, name="toolchain")


def _service(context: Any) -> Any:
    return service(context, "instrumentation")


def _device_row(device: Any) -> dict[str, Any]:
    caps = getattr(device, "capabilities", None)
    return jsonable(
        {
            "instance": getattr(device, "instance", ""),
            "running": bool(getattr(device, "running", False)),
            "api_level": getattr(device, "api_level", 0),
            "abi": getattr(device, "abi", ""),
            "image_tag": getattr(device, "image_tag", ""),
            "root": str(getattr(device, "root_implementation", "")),
            "compatible": bool(getattr(device, "compatible", False)),
            "adb_root": bool(getattr(caps, "adb_root", False)),
            "writable_system": bool(getattr(caps, "writable_system", False)),
            "magisk": bool(getattr(caps, "magisk", False)),
            "magisk_version": getattr(caps, "magisk_version", ""),
            "magisk_environment_ready": bool(getattr(caps, "magisk_environment_ready", False)),
            "zygisk_enabled": bool(getattr(caps, "zygisk_enabled", False)),
            "zygisk_active": bool(getattr(caps, "zygisk_active", False)),
            "lsposed_installed": bool(getattr(caps, "lsposed_installed", False)),
            "lsposed_version": getattr(caps, "lsposed_version", ""),
            "lsposed_active": bool(getattr(caps, "lsposed_active", False)),
            "reboot_required": bool(getattr(caps, "reboot_required", False)),
            "magisk_modules": [m.identifier for m in getattr(device, "magisk_modules", []) or []],
            "blockers": list(getattr(device, "blockers", []) or []),
            "notes": list(getattr(caps, "notes", []) or []),
        }
    )


@instrumentation_app.command("scan")
def scan(
    ctx: typer.Context,
    targets: list[str] = typer.Argument(None, help="AVD names to probe."),
    json_output: bool = json_flag(),
) -> None:
    """Probe AVDs for root, Magisk, Zygisk and LSPosed capabilities."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    names = [str(name) for name in (targets or [])]
    if not names:
        raise CliUsageError("pass at least one AVD name", hint="avdfleet instances list")
    devices = [_device_row(device) for device in invoke_callable(_service(context).scan, names)]
    out.emit({"devices": devices}, lambda: _render_devices(out, devices))


def _render_devices(out: Any, devices: list[dict[str, Any]]) -> None:
    out.table(
        ["instance", "api", "abi", "root", "magisk", "zygisk", "lsposed", "compatible"],
        [
            [
                device["instance"],
                device["api_level"],
                device["abi"],
                device["root"],
                (device["magisk_version"] or "no") if device["magisk"] else "no",
                "active" if device["zygisk_active"] else ("enabled" if device["zygisk_enabled"] else "no"),
                "active" if device["lsposed_active"] else ("installed" if device["lsposed_installed"] else "no"),
                "yes" if device["compatible"] else "no",
            ]
            for device in devices
        ],
        title="instrumentation",
    )
    for device in devices:
        for blocker in device.get("blockers") or []:
            out.warning(f"{device['instance']}: {blocker}")


# ---------------------------------------------------------------------------
# Module library
# ---------------------------------------------------------------------------


@modules_app.command("list")
def modules_list(
    ctx: typer.Context,
    kind: str = typer.Option("", "--kind", help="Filter by module kind."),
    json_output: bool = json_flag(),
) -> None:
    """List imported modules."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    from ...instrumentation import ModuleKind

    module_kind = None
    if kind:
        try:
            module_kind = ModuleKind(kind)
        except ValueError as exc:
            raise CliUsageError(
                f"unknown module kind {kind!r}",
                hint="one of: " + ", ".join(item.value for item in ModuleKind),
            ) from exc
    records = list(invoke_callable(_service(context).list_modules, module_kind) or [])
    rows = [
        {
            "id": record.id,
            "kind": str(record.kind),
            "name": record.name or record.package_name,
            "version": record.version,
            "size": record.size_bytes,
            "sha256": record.sha256[:16],
            "source": record.source,
        }
        for record in records
    ]
    out.emit({"modules": rows}, lambda: out.table(["id", "kind", "name", "version", "sha256", "source"], [[r["id"], r["kind"], r["name"], r["version"], r["sha256"], r["source"]] for r in rows], title="modules"))


@modules_app.command("import")
def modules_import(
    ctx: typer.Context,
    path: str = typer.Argument("", help="Local .apk/.zip to import."),
    url: str = typer.Option("", "--url", help="Import from an https URL instead."),
    json_output: bool = json_flag(),
) -> None:
    """Import an Xposed APK or a Magisk/Zygisk module ZIP into the library."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    svc = _service(context)
    if url:
        record = invoke_callable(svc.import_url, url)
    elif path:
        record = invoke_callable(svc.import_path, path)
    else:
        raise CliUsageError("pass a path, or --url", hint="avdfleet instrumentation modules import <file>")
    payload = jsonable(
        {
            "id": record.id,
            "kind": str(record.kind),
            "name": record.name,
            "package_name": record.package_name,
            "version": record.version,
            "sha256": record.sha256,
            "source": record.source,
        }
    )
    out.emit(payload, lambda: out.info(f"imported {payload['kind']} {payload['name']} as {payload['id']}"))


@modules_app.command("delete")
def modules_delete(ctx: typer.Context, module_id: str = typer.Argument(...)) -> None:
    """Remove a module from the library (installed copies are untouched)."""
    context = get_context(ctx)
    out = context.output(json_output=False)
    invoke_callable(_service(context).delete_module, module_id)
    out.info(f"module {module_id} removed from the library")


@toolchain_app.command("list")
def toolchain_list(ctx: typer.Context, json_output: bool = json_flag()) -> None:
    """Show the pinned upstream artifacts and whether they are downloaded."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    payload = invoke_callable(_service(context).toolchain_status) or {}
    rows = [
        {
            "key": item.get("key", ""),
            "version": item.get("version", ""),
            "kind": item.get("kind", ""),
            "present": bool(item.get("present")),
            "verified": bool(item.get("verified")),
            "sha256": str(item.get("pinned_sha256", ""))[:16],
        }
        for item in payload.get("artifacts", [])
    ]
    out.emit(
        payload,
        lambda: out.table(
            ["key", "version", "kind", "downloaded", "verified", "sha256"],
            [[r["key"], r["version"], r["kind"], "yes" if r["present"] else "no", "yes" if r["verified"] else "no", r["sha256"]] for r in rows],
            title="toolchain",
        ),
    )


@toolchain_app.command("acquire")
def toolchain_acquire(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="magisk or lsposed"),
    confirm: bool = typer.Option(
        False, "--confirm", help="Accept the GPL-3.0 download and its pinned hash."
    ),
    json_output: bool = json_flag(),
) -> None:
    """Download and verify one pinned release (requires --confirm)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    payload = invoke_callable(_service(context).acquire_toolchain, key, confirm=confirm)
    out.emit(
        payload,
        lambda: out.info(
            f"acquired {key}: verified={payload['artifact']['verified']}"
            f" sha256={payload['artifact']['sha256'][:16]}"
        ),
    )


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@profiles_app.command("list")
def profiles_list(ctx: typer.Context, json_output: bool = json_flag()) -> None:
    """List instrumentation profiles."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    profiles = [
        {
            "id": profile.id,
            "name": profile.name,
            "revision": profile.revision,
            "builtin": profile.builtin,
            "steps": [str(step.action) for step in profile.steps],
        }
        for profile in invoke_callable(_service(context).list_profiles) or []
    ]
    out.emit(
        {"profiles": profiles},
        lambda: out.table(
            ["id", "name", "revision", "builtin", "steps"],
            [[p["id"], p["name"], p["revision"], "yes" if p["builtin"] else "no", " -> ".join(p["steps"])] for p in profiles],
            title="profiles",
        ),
    )


@profiles_app.command("delete")
def profiles_delete(ctx: typer.Context, profile_id: str = typer.Argument(...)) -> None:
    """Delete a user profile (built-ins are protected)."""
    context = get_context(ctx)
    out = context.output(json_output=False)
    invoke_callable(_service(context).delete_profile, profile_id)
    out.info(f"profile {profile_id} deleted")


# ---------------------------------------------------------------------------
# Deployments
# ---------------------------------------------------------------------------


@instrumentation_app.command("deploy")
def deploy(
    ctx: typer.Context,
    profile_id: str = typer.Argument(..., help="Profile id (see 'profiles list')."),
    target: list[str] = typer.Option(..., "--target", help="AVD name (repeatable)."),
    label: str = typer.Option("", "--label"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Block until the run finishes."),
    json_output: bool = json_flag(),
) -> None:
    """Deploy a profile to one or more AVDs."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    svc = _service(context)
    run = invoke_callable(svc.start_run, profile_id=profile_id, targets=list(target), label=label)
    if wait:
        run = invoke_callable(svc.execute_run, run.id)
    payload = jsonable(
        {
            "id": run.id,
            "state": str(run.state),
            "targets": run.targets,
            "results": [
                {
                    "instance": result.instance,
                    "ok": result.ok,
                    "error": result.error,
                    "steps": [
                        {"action": str(step.action), "ok": step.ok, "message": step.message}
                        for step in result.steps
                    ],
                }
                for result in run.results
            ],
        }
    )
    out.emit(payload, lambda: _render_run(out, payload))


def _render_run(out: Any, payload: dict[str, Any]) -> None:
    out.info(f"deployment {payload['id']}: {payload['state']}")
    for result in payload["results"]:
        out.table(
            ["step", "ok", "message"],
            [[step["action"], "yes" if step["ok"] else "no", step["message"][:80]] for step in result["steps"]],
            title=f"{result['instance']} ({'ok' if result['ok'] else 'failed'})",
        )
        if result.get("error"):
            out.warning(f"{result['instance']}: {result['error']}")


@instrumentation_app.command("runs")
def runs(ctx: typer.Context, limit: int = typer.Option(20, "--limit"), json_output: bool = json_flag()) -> None:
    """List recent deployments."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    rows = [
        {"id": run.id, "state": str(run.state), "targets": ",".join(run.targets), "label": run.label}
        for run in invoke_callable(_service(context).list_runs, limit) or []
    ]
    out.emit({"runs": rows}, lambda: out.table(["id", "state", "targets", "label"], [[r["id"], r["state"], r["targets"], r["label"]] for r in rows], title="runs"))


@instrumentation_app.command("logs")
def logs(ctx: typer.Context, run_id: str = typer.Argument(...), json_output: bool = json_flag()) -> None:
    """Show the log lines of a deployment."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    entries = [
        {"at": str(entry.at), "instance": entry.instance, "level": entry.level, "message": entry.message}
        for entry in invoke_callable(_service(context).logs, run_id) or []
    ]
    out.emit({"logs": entries}, lambda: _render_logs(out, entries))


def _render_logs(out: Any, entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        out.info(
            f"{entry['at']} {entry['instance'] or '-'} {entry['level']}: {entry['message']}"
        )


def register(app: typer.Typer) -> None:
    app.add_typer(instrumentation_app, name="instrumentation")


__all__ = ["instrumentation_app", "register"]
