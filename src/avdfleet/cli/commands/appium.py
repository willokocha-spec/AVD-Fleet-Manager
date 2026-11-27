"""avdfleet appium - Appium server lifecycle and UiAutomator2 capabilities."""

from typing import Any

import typer

from ..context import get_context, invoke_callable, optional_service, service
from ..errors import command_errors
from ..output import json_flag, jsonable
from .avd import lookup_instance

appium_app = typer.Typer(help="Manage the Appium server and session capabilities.", no_args_is_help=True)


def _render_status(out: Any, status: Any, *, title: str) -> None:
    payload = jsonable(status)
    running = bool(getattr(status, "running", False))

    def render() -> None:
        (out.success if running else out.warning)(
            f"Appium: {'running' if running else 'stopped'}"
            + (f" - {getattr(status, 'url', '') or ''}" if running else "")
        )
        out.kv(
            (
                ("running", "yes" if running else "no"),
                ("pid", getattr(status, "pid", "") or ""),
                ("port", getattr(status, "port", "") or ""),
                ("url", getattr(status, "url", "") or ""),
                ("version", getattr(status, "version", "") or ""),
                ("drivers", ", ".join(getattr(status, "drivers", []) or [])),
                ("sessions", getattr(status, "sessions", "") or ""),
                ("message", getattr(status, "message", "") or ""),
            ),
            title=title,
        )

    out.emit(payload, render)


def appium_status(
    ctx: typer.Context,
    json_output: bool = json_flag(),
) -> None:
    """Show the Appium server status."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    status = invoke_callable(service(ctx, "appium").status)
    _render_status(out, status, title="appium status")


def appium_start(
    ctx: typer.Context,
    port: int = typer.Option(None, "--port", min=1, max=65535, help="Server port (default: first free Appium port)."),
    json_output: bool = json_flag(),
) -> None:
    """Start the Appium server."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    status = invoke_callable(service(ctx, "appium").start, port=port)
    _render_status(out, status, title="appium start")
    if not bool(getattr(status, "running", False)):
        raise typer.Exit(1)


def appium_stop(
    ctx: typer.Context,
    json_output: bool = json_flag(),
) -> None:
    """Stop the Appium server."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    status = invoke_callable(service(ctx, "appium").stop)
    _render_status(out, status, title="appium stop")


def _template_for(context: Any, info: Any) -> Any | None:
    template_id = getattr(info, "template_id", None)
    if not template_id:
        return None
    avds = optional_service(context, "avds")
    if avds is None:
        return None
    getter = getattr(avds, "get_template", None)
    if not callable(getter):
        return None
    try:
        return invoke_callable(getter, template_id)
    except Exception:  # noqa: BLE001 - capabilities can be built without the template
        return None


def appium_capabilities(
    ctx: typer.Context,
    avd_name: str = typer.Argument(..., help="Instance name."),
    json_output: bool = json_flag(),
) -> None:
    """Build the UiAutomator2 capabilities for an instance."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    info = lookup_instance(context, avd_name)
    template = _template_for(context, info)
    capabilities = invoke_callable(service(ctx, "appium").build_capabilities, info, template=template)
    payload = jsonable(capabilities)

    def render() -> None:
        out.kv([(str(key), jsonable(value)) for key, value in dict(capabilities).items()], title="capabilities")

    out.emit(payload, render)


def appium_cleanup(
    ctx: typer.Context,
    avd_name: str = typer.Argument(..., help="Instance name."),
    json_output: bool = json_flag(),
) -> None:
    """Remove adb forwards/session state left behind by Appium runs."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    result = invoke_callable(service(ctx, "appium").cleanup_forwards, avd_name)
    out.operation(result, title=f"appium cleanup - {avd_name}")
    if not bool(getattr(result, "ok", True)):
        raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    appium_app.command("status")(command_errors("appium status")(appium_status))
    appium_app.command("start")(command_errors("appium start")(appium_start))
    appium_app.command("stop")(command_errors("appium stop")(appium_stop))
    appium_app.command("capabilities")(command_errors("appium capabilities")(appium_capabilities))
    appium_app.command("cleanup")(command_errors("appium cleanup")(appium_cleanup))
    app.add_typer(appium_app, name="appium")


__all__ = ["appium_app", "register"]
