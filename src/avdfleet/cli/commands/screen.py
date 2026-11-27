"""avdfleet screen - screenshot capture and input injection.

The screen service (screen/) owns the gRPC/adb/scrcpy fallback chain; the
commands here only capture PNG bytes or dispatch one input event.
"""

from pathlib import Path
from typing import Any

import typer

from ..context import get_context, invoke_callable, service
from ..errors import CliUsageError, command_errors
from ..output import json_flag, jsonable

screen_app = typer.Typer(help="Capture screenshots and inject tap/swipe/text/key input.", no_args_is_help=True)


def _target(context: Any, name: str, output: Path | None) -> Path:
    if output is not None:
        target = Path(output)
    else:
        safe = "".join(character if character.isalnum() or character in "._-" else "_" for character in name)
        target = Path(context.paths.screenshots_dir) / f"{safe or 'instance'}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _render_operation(out: Any, result: Any, *, action: str, instance: str) -> None:
    payload = jsonable(result)
    ok = bool(getattr(result, "ok", True))
    message = str(getattr(result, "message", "") or "")

    def render() -> None:
        if ok:
            out.success(message or f"{action} sent to {instance}")
        else:
            out.error(message or f"{action} failed on {instance}")

    out.emit(payload, render)
    if not ok:
        raise typer.Exit(1)


def screen_capture(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    output: Path = typer.Option(None, "-o", "--output", help="Write the PNG here (default: <data_dir>/screenshots)."),
    json_output: bool = json_flag(),
) -> None:
    """Capture one PNG screenshot (gRPC, then adb, then cache)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    data = invoke_callable(service(ctx, "screen").capture, name)
    blob = bytes(data) if isinstance(data, (bytes, bytearray, memoryview)) else b""
    if not blob:
        raise CliUsageError(
            "screenshot capture returned no image data",
            hint="check that the instance is running and screen sharing is available",
        )
    target = _target(context, name, output)
    target.write_bytes(blob)
    payload = {"instance": name, "output": str(target), "bytes": len(blob), "ok": True}
    out.emit(payload, lambda: out.success(f"screenshot saved to {target}"))


def screen_tap(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    x: int = typer.Argument(..., help="X coordinate."),
    y: int = typer.Argument(..., help="Y coordinate."),
    json_output: bool = json_flag(),
) -> None:
    """Inject a tap at (x, y)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    result = invoke_callable(service(ctx, "screen").tap, name, x, y)
    _render_operation(out, result, action="tap", instance=name)


def screen_swipe(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    x1: int = typer.Argument(..., help="Start X."),
    y1: int = typer.Argument(..., help="Start Y."),
    x2: int = typer.Argument(..., help="End X."),
    y2: int = typer.Argument(..., help="End Y."),
    duration_ms: int = typer.Option(300, "--duration", min=0, max=60000, help="Swipe duration in milliseconds."),
    json_output: bool = json_flag(),
) -> None:
    """Inject a swipe."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    result = invoke_callable(
        service(ctx, "screen").swipe, name, x1, y1, x2, y2, duration_ms=duration_ms
    )
    _render_operation(out, result, action="swipe", instance=name)


def screen_text(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    text: str = typer.Argument(..., help="Text to type."),
    json_output: bool = json_flag(),
) -> None:
    """Inject text into the focused field."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    result = invoke_callable(service(ctx, "screen").text, name, text)
    _render_operation(out, result, action="text", instance=name)


def screen_key(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    keycode: str = typer.Argument(..., help="Android keycode (e.g. HOME, BACK, 66)."),
    json_output: bool = json_flag(),
) -> None:
    """Inject a key event."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    result = invoke_callable(service(ctx, "screen").key, name, keycode)
    _render_operation(out, result, action="key", instance=name)


def register(app: typer.Typer) -> None:
    screen_app.command("capture")(command_errors("screen capture")(screen_capture))
    screen_app.command("tap")(command_errors("screen tap")(screen_tap))
    screen_app.command("swipe")(command_errors("screen swipe")(screen_swipe))
    screen_app.command("text")(command_errors("screen text")(screen_text))
    screen_app.command("key")(command_errors("screen key")(screen_key))
    app.add_typer(screen_app, name="screen")


__all__ = ["register", "screen_app"]
