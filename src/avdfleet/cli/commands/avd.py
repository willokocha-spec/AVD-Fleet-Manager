"""avdfleet avd - templates, instances, config.ini and YAML export/import.

Thin dispatch to the AVD manager service (platform/avd): template CRUD,
instance creation/deletion/cloning, bulk deploy, typed config.ini editing,
device-profile listing and the fleet YAML export/import round-trip.
"""

import sys
from pathlib import Path
from typing import Any

import typer

from avdfleet.core.models import GpuMode, Template, slugify

from ..context import get_context, invoke_callable, service
from ..errors import CliUsageError, command_errors
from ..output import json_flag, jsonable

avd_app = typer.Typer(help="Manage AVD templates and instances.", no_args_is_help=True)
templates_app = typer.Typer(help="Create, list and delete AVD templates.", no_args_is_help=True)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def instance_rows(instances: list[Any]) -> list[tuple[str, str, str, str, str, str, str]]:
    rows = []
    for info in instances:
        ports = getattr(info, "ports", None)
        console = getattr(ports, "console", "") if ports is not None else ""
        state = getattr(info, "state", "")
        rows.append(
            (
                str(getattr(info, "name", "")),
                str(getattr(info, "template_id", "") or ""),
                str(getattr(info, "image_id", "") or getattr(info, "image_name", "") or ""),
                str(getattr(state, "value", state)),
                str(getattr(info, "adb_serial", "") or ""),
                str(console or ""),
                str(getattr(info, "proxy_id", "") or ""),
            )
        )
    return rows


def instance_payload(info: Any) -> dict[str, Any]:
    return jsonable(info)


def merged_instances(context: Any) -> list[Any]:
    """Merge AVD definitions with live runtime state (runtime wins per name)."""
    merged: dict[str, Any] = {}
    avds = context.optional_service("avds")
    if avds is not None:
        lister = getattr(avds, "list_instances", None)
        if callable(lister):
            for info in invoke_callable(lister) or []:
                merged[str(getattr(info, "name", ""))] = info
    runtime = context.optional_service("runtime")
    if runtime is not None:
        lister = getattr(runtime, "list_running", None)
        if callable(lister):
            for info in invoke_callable(lister) or []:
                merged[str(getattr(info, "name", ""))] = info
    return sorted(merged.values(), key=lambda info: str(getattr(info, "name", "")))


def lookup_instance(context: Any, name: str) -> Any:
    """Return an instance, preferring the live runtime state."""
    runtime = context.optional_service("runtime")
    if runtime is not None:
        status = getattr(runtime, "status", None)
        if callable(status):
            try:
                return invoke_callable(status, name)
            except Exception as exc:  # noqa: BLE001 - fall through to the definition
                if isinstance(exc, typer.Exit):
                    raise
                last_error = exc
        else:
            last_error = None
    else:
        last_error = None
    avds = context.optional_service("avds")
    if avds is not None:
        getter = getattr(avds, "get_instance", None)
        if callable(getter):
            try:
                return invoke_callable(getter, name)
            except Exception as exc:  # noqa: BLE001 - report the lookup failure below
                last_error = exc
    if last_error is not None:
        raise last_error
    raise CliUsageError(f"instance {name!r} was not found")


def template_rows(templates: list[Any]) -> list[tuple[str, str, str, str, str, str, str]]:
    rows = []
    for template in templates:
        gpu = getattr(getattr(template, "gpu_mode", ""), "value", getattr(template, "gpu_mode", ""))
        rows.append(
            (
                str(getattr(template, "id", "")),
                str(getattr(template, "name", "")),
                str(getattr(template, "image_id", "")),
                str(getattr(template, "device_id", "")),
                str(getattr(template, "ram_mb", "")),
                str(getattr(template, "cpu_cores", "")),
                f"{gpu}{' headless' if getattr(template, 'headless', False) else ''}",
            )
        )
    return rows


def _render_templates(out: Any, templates: list[Any], *, title: str) -> None:
    payload = jsonable(templates)

    def render() -> None:
        out.table(
            ("id", "name", "image", "device", "ram", "cpu", "gpu"),
            template_rows(templates),
            title=title,
        )
        out.info(f"{len(templates)} template(s)")

    out.emit(payload, render)


def _render_instances(out: Any, instances: list[Any], *, title: str) -> None:
    payload = jsonable(instances)

    def render() -> None:
        out.table(
            ("name", "template", "image", "state", "adb", "console", "proxy"),
            instance_rows(instances),
            title=title,
            status_columns=(3,),
        )
        out.info(f"{len(instances)} instance(s)")

    out.emit(payload, render)


def _render_instance(out: Any, info: Any, *, title: str) -> None:
    state = getattr(info, "state", "")
    payload = instance_payload(info)

    def render() -> None:
        ports = getattr(info, "ports", None)
        out.kv(
            (
                ("name", getattr(info, "name", "")),
                ("template", getattr(info, "template_id", "") or ""),
                ("image", getattr(info, "image_id", "") or getattr(info, "image_name", "") or ""),
                ("state", str(getattr(state, "value", state))),
                ("adb serial", getattr(info, "adb_serial", "") or ""),
                ("pid", getattr(info, "pid", "") or ""),
                ("console port", getattr(ports, "console", "") if ports is not None else ""),
                ("adb port", getattr(ports, "adb", "") if ports is not None else ""),
                ("proxy", getattr(info, "proxy_id", "") or ""),
                ("boot completed", "yes" if getattr(info, "boot_completed", False) else "no"),
                ("error", getattr(info, "error", "") or ""),
            ),
            title=title,
        )

    out.emit(payload, render)


def _parse_sets(values: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values or []:
        text = str(item)
        if "=" not in text:
            raise CliUsageError(
                f"invalid --set value {text!r}",
                hint="use --set KEY=VALUE (repeat the flag for several keys)",
            )
        key, _, value = text.partition("=")
        key = key.strip()
        if not key:
            raise CliUsageError("--set requires a non-empty KEY")
        result[key] = value
    return result


def _apply_config(
    ctx: typer.Context,
    *,
    name: str,
    set_values: list[str] | None,
    unset_values: list[str] | None,
    dry_run: bool,
    json_output: bool,
) -> None:
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    avds = service(ctx, "avds")
    values = _parse_sets(set_values)
    for key in unset_values or []:
        identifier = str(key).strip()
        if identifier:
            values[identifier] = ""
    if not values:
        reader = getattr(avds, "read_config", None)
        if callable(reader):
            current = invoke_callable(reader, name)
            payload = {"instance": name, "config": jsonable(current)}
            out.emit(payload, lambda: out.kv(sorted(current.items()), title=f"config.ini - {name}"))
            return
        raise CliUsageError("provide --set KEY=VALUE or use 'instances config' to read the file")
    info = invoke_callable(avds.update_config, name, values, dry_run=dry_run)
    payload = {"instance": name, "dry_run": dry_run, "values": values, "result": jsonable(info)}

    def render() -> None:
        for key, value in values.items():
            out.info(f"{key} = {value}")
        out.operation(
            type("_R", (), {"ok": True, "message": "dry run: no change written" if dry_run else "config.ini updated", "data": {}})(),
            title=f"config.ini - {name}",
        )

    out.emit(payload, render)


# ---------------------------------------------------------------------------
# Template commands
# ---------------------------------------------------------------------------


def templates_list(
    ctx: typer.Context,
    json_output: bool = json_flag(),
) -> None:
    """List AVD templates."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    avds = service(ctx, "avds")
    _render_templates(out, list(invoke_callable(avds.list_templates) or []), title="AVD templates")


def templates_create(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", "-n", help="Human-readable template name."),
    image: str = typer.Option(..., "--image", help="System image id or SDK package."),
    template_id: str = typer.Option(None, "--id", help="Stable template id (defaults to a slug of --name)."),
    device: str = typer.Option("pixel_7", "--device", help="Device profile id."),
    ram_mb: int = typer.Option(2048, "--ram", min=512, help="RAM in MiB."),
    heap_mb: int = typer.Option(256, "--heap", min=16, help="Dalvik heap in MiB."),
    cpu_cores: int = typer.Option(2, "--cpu", min=1, help="Virtual CPU cores."),
    gpu: str = typer.Option("swiftshader_indirect", "--gpu", help="GPU mode (auto/host/swiftshader_indirect/off)."),
    headless: bool = typer.Option(True, "--headless/--windowed", help="Run headless (default) or with a window."),
    proxy: str = typer.Option(None, "--proxy", help="Default proxy id for instances from this template."),
    description: str = typer.Option("", "--description", help="Free-form description."),
    tag: list[str] = typer.Option(None, "--tag", help="Template tag (repeatable)."),
    json_output: bool = json_flag(),
) -> None:
    """Create or update an AVD template."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    avds = service(ctx, "avds")
    identifier = template_id or slugify(name)
    try:
        gpu_mode = GpuMode(gpu)
    except ValueError as exc:
        raise CliUsageError(
            f"invalid GPU mode {gpu!r}",
            hint="use one of: " + ", ".join(mode.value for mode in GpuMode),
        ) from exc
    template = Template(
        id=identifier,
        name=name,
        image_id=image,
        device_id=device,
        ram_mb=ram_mb,
        heap_mb=heap_mb,
        cpu_cores=cpu_cores,
        gpu_mode=gpu_mode,
        headless=bool(headless),
        proxy_id=proxy,
        description=description,
        tags=[str(item) for item in (tag or [])],
    )
    saved = invoke_callable(avds.save_template, template)
    _render_templates(out, [saved if saved is not None else template], title="saved template")


def templates_delete(
    ctx: typer.Context,
    template_id: str = typer.Argument(..., help="Template id to delete."),
    json_output: bool = json_flag(),
) -> None:
    """Delete an AVD template (instances already deployed are unaffected)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    avds = service(ctx, "avds")
    invoke_callable(avds.delete_template, template_id)
    payload = {"deleted": template_id, "ok": True}
    out.emit(payload, lambda: out.success(f"template {template_id!r} deleted"))


# ---------------------------------------------------------------------------
# Instance commands
# ---------------------------------------------------------------------------


def avd_list(
    ctx: typer.Context,
    json_output: bool = json_flag(),
) -> None:
    """List deployed AVD instances with their live state."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    _render_instances(out, merged_instances(context), title="AVD instances")


def avd_create(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="New instance name."),
    template: str = typer.Option(..., "--template", "-t", help="Template id to deploy from."),
    ram_mb: int = typer.Option(None, "--ram", min=512, help="Override template RAM."),
    json_output: bool = json_flag(),
) -> None:
    """Create one instance from a template."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    avds = service(ctx, "avds")
    overrides: dict[str, Any] = {}
    if ram_mb is not None:
        overrides["ram_mb"] = ram_mb
    info = invoke_callable(avds.create_instance, template, name, overrides or None)
    _render_instance(out, info, title="instance created")


def avd_delete(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name to delete."),
    keep_files: bool = typer.Option(False, "--keep-files", help="Keep the AVD files on disk."),
    json_output: bool = json_flag(),
) -> None:
    """Delete an AVD instance."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    avds = service(ctx, "avds")
    info = None
    getter = getattr(avds, "get_instance", None)
    if callable(getter):
        try:
            info = invoke_callable(getter, name)
        except Exception:  # noqa: BLE001 - deletion below reports not-found properly
            info = None
    if info is not None and bool(getattr(getattr(info, "state", None), "active", False)):
        from avdfleet.core.errors import ConflictError

        raise ConflictError(f"instance {name!r} is running", hint="stop the instance before deleting it")
    invoke_callable(avds.delete_instance, name, delete_files=not keep_files)
    payload = {"deleted": name, "delete_files": not keep_files, "ok": True}
    out.emit(payload, lambda: out.success(f"instance {name!r} deleted"))


def avd_clone(
    ctx: typer.Context,
    source: str = typer.Argument(..., help="Source instance name."),
    name: str = typer.Option(..., "--name", "-n", help="Name for the clone."),
    json_output: bool = json_flag(),
) -> None:
    """Clone an AVD instance."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    avds = service(ctx, "avds")
    info = invoke_callable(avds.clone_instance, source, name)
    _render_instance(out, info, title="instance cloned")


def avd_deploy(
    ctx: typer.Context,
    template: str = typer.Argument(..., help="Template id to deploy."),
    count: int = typer.Option(1, "--count", "-n", min=1, max=64, help="How many instances to deploy."),
    prefix: str = typer.Option("avd", "--prefix", help="Instance name prefix."),
    json_output: bool = json_flag(),
) -> None:
    """Bulk-deploy instances from one template (capacity checked by the service)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    avds = service(ctx, "avds")
    deployed = list(invoke_callable(avds.bulk_deploy, template, count=count, prefix=prefix) or [])
    _render_instances(out, deployed, title=f"deployed {len(deployed)} instance(s)")


def avd_config(
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


def avd_export(
    ctx: typer.Context,
    output_file: Path = typer.Option(None, "-o", "--output", help="Write the YAML to this file (default: stdout)."),
    json_output: bool = json_flag(),
) -> None:
    """Export templates and instances as fleet YAML."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    avds = service(ctx, "avds")
    content = invoke_callable(avds.export_yaml)
    text = content if isinstance(content, str) else str(content)
    if output_file is not None:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(text, encoding="utf-8")
        payload = {"output": str(output_file), "yaml": text, "ok": True}
        out.emit(payload, lambda: out.success(f"exported to {output_file}"))
        return
    payload = {"output": None, "yaml": text, "ok": True}
    out.emit(payload, lambda: out.write(text.rstrip()))


def avd_import(
    ctx: typer.Context,
    file: Path = typer.Argument(..., help="YAML file to import ('-' reads stdin)."),
    json_output: bool = json_flag(),
) -> None:
    """Import templates and instances from fleet YAML."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    avds = service(ctx, "avds")
    if str(file) == "-":
        content = sys.stdin.read()
        source = "<stdin>"
    else:
        try:
            content = Path(file).read_text(encoding="utf-8")
        except OSError as exc:
            raise CliUsageError(f"could not read {file}: {exc}") from exc
        source = str(file)
    imported = list(invoke_callable(avds.import_yaml, content) or [])
    payload = {"source": source, "instances": jsonable(imported)}

    def render() -> None:
        out.table(
            ("name", "template", "image", "state", "adb", "console", "proxy"),
            instance_rows(imported),
            title=f"imported from {source}",
            status_columns=(3,),
        )
        out.info(f"{len(imported)} instance(s) imported")

    out.emit(payload, render)


def avd_devices(
    ctx: typer.Context,
    json_output: bool = json_flag(),
) -> None:
    """List device profiles known to the Android tooling."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    avds = service(ctx, "avds")
    profiles = list(invoke_callable(avds.list_device_profiles) or [])
    payload = jsonable(profiles)

    def render() -> None:
        rows = [
            (
                str(getattr(profile, "id", "")),
                str(getattr(profile, "name", "")),
                str(getattr(profile, "resolution", "") or ""),
                str(getattr(profile, "density", "") or ""),
                str(getattr(profile, "ram_mb", "") or ""),
            )
            for profile in profiles
        ]
        out.table(("id", "name", "resolution", "density", "ram"), rows, title="device profiles")
        out.info(f"{len(profiles)} profile(s)")

    out.emit(payload, render)


def register(app: typer.Typer) -> None:
    """Register the avd command group (including avd templates)."""
    templates_app.command("list")(command_errors("avd templates list")(templates_list))
    templates_app.command("create")(command_errors("avd templates create")(templates_create))
    templates_app.command("delete")(command_errors("avd templates delete")(templates_delete))
    avd_app.add_typer(templates_app, name="templates")
    avd_app.command("list")(command_errors("avd list")(avd_list))
    avd_app.command("create")(command_errors("avd create")(avd_create))
    avd_app.command("delete")(command_errors("avd delete")(avd_delete))
    avd_app.command("clone")(command_errors("avd clone")(avd_clone))
    avd_app.command("deploy")(command_errors("avd deploy")(avd_deploy))
    avd_app.command("config")(command_errors("avd config")(avd_config))
    avd_app.command("export")(command_errors("avd export")(avd_export))
    avd_app.command("import")(command_errors("avd import")(avd_import))
    avd_app.command("devices")(command_errors("avd devices")(avd_devices))
    app.add_typer(avd_app, name="avd")


__all__ = [
    "avd_app",
    "instance_rows",
    "lookup_instance",
    "merged_instances",
    "register",
    "templates_app",
]
