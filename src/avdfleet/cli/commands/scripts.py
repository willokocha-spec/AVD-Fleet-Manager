"""avdfleet scripts - script definitions and fan-out runs.

The script service (scripts/) owns execution, persistence, live events,
cancellation and timeouts.  The CLI starts synchronous or detached runs and
renders the aggregated per-instance results.
"""

import sys
from pathlib import Path
from typing import Any

import typer

from avdfleet.core.models import ScriptDefinition, ScriptRunRequest, ScriptType, slugify

from ..context import get_context, invoke_callable, optional_service, service
from ..errors import CliUsageError, command_errors
from ..output import json_flag, jsonable

scripts_app = typer.Typer(help="Create scripts and run them across instances.", no_args_is_help=True)
runs_app = typer.Typer(help="Inspect and cancel script runs.", no_args_is_help=True)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def run_payload(run: Any) -> dict[str, Any]:
    payload = jsonable(run)
    if isinstance(payload, dict):
        results = payload.get("results") or []
        payload.setdefault(
            "summary",
            {
                "total": len(results),
                "succeeded": sum(1 for item in results if item.get("state") == "succeeded"),
                "failed": sum(
                    1
                    for item in results
                    if item.get("state") in {"failed", "timed_out", "cancelled"}
                ),
            },
        )
    return payload


def _render_run(out: Any, run: Any, *, title: str) -> None:
    payload = run_payload(run)
    results = list(getattr(run, "results", []) or [])

    def render() -> None:
        rows = []
        for result in results:
            state = getattr(result, "state", "")
            stdout = str(getattr(result, "stdout", "") or "").strip().replace("\n", " ")
            if len(stdout) > 80:
                stdout = stdout[:77] + "..."
            rows.append(
                (
                    str(getattr(result, "instance", "")),
                    str(getattr(state, "value", state)),
                    str(getattr(result, "exit_code", "") if getattr(result, "exit_code", None) is not None else ""),
                    str(getattr(result, "duration_s", "") or ""),
                    stdout,
                )
            )
        out.table(
            ("instance", "state", "exit", "duration s", "output"),
            rows,
            title=title,
            status_columns=(1,),
        )
        state = getattr(run, "state", "")
        state_text = str(getattr(state, "value", state))
        total = len(results)
        succeeded = int(getattr(run, "succeeded", 0) or 0)
        failed = int(getattr(run, "failed", 0) or 0)
        out.info(
            f"run {getattr(run, 'id', '')}: {state_text} - {succeeded} succeeded, "
            f"{failed} failed/cancelled, {total} total"
        )
        for result in results:
            error = getattr(result, "error", "") or ""
            if error:
                out.warning(f"{getattr(result, 'instance', '')}: {error}")

    out.emit(payload, render)


def _render_runs(out: Any, runs: list[Any], *, title: str) -> None:
    payload = [run_payload(run) for run in runs]

    def render() -> None:
        rows = []
        for run in runs:
            state = getattr(run, "state", "")
            rows.append(
                (
                    str(getattr(run, "id", "")),
                    str(getattr(run, "script_name", "") or getattr(run, "script_id", "")),
                    str(getattr(state, "value", state)),
                    str(getattr(run, "started_at", "") or ""),
                    str(len(getattr(run, "instances", []) or [])),
                )
            )
        out.table(("run", "script", "state", "started", "instances"), rows, title=title, status_columns=(2,))
        out.info(f"{len(runs)} run(s)")

    out.emit(payload, render)


# ---------------------------------------------------------------------------
# Definition commands
# ---------------------------------------------------------------------------


def scripts_list(
    ctx: typer.Context,
    json_output: bool = json_flag(),
) -> None:
    """List script definitions (including built-ins)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    scripts = service(ctx, "scripts")
    definitions = list(invoke_callable(scripts.list_scripts) or [])
    payload = jsonable(definitions)

    def render() -> None:
        rows = [
            (
                str(getattr(item, "id", "")),
                str(getattr(item, "name", "")),
                str(getattr(getattr(item, "type", ""), "value", getattr(item, "type", ""))),
                str(getattr(item, "timeout_s", "") or ""),
                "builtin" if getattr(item, "builtin", False) else "",
                str(getattr(item, "description", "") or ""),
            )
            for item in definitions
        ]
        out.table(("id", "name", "type", "timeout", "kind", "description"), rows, title="scripts")
        out.info(f"{len(definitions)} script(s)")

    out.emit(payload, render)


def scripts_create(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", "-n", help="Script name."),
    script_type: str = typer.Option("adb_shell", "--type", help="adb_shell, host_python, appium_python, appium_js."),
    content: str = typer.Option(None, "--content", help="Inline script body."),
    file: Path = typer.Option(None, "--file", help="Read the script body from this file ('-' reads stdin)."),
    description: str = typer.Option("", "--description", help="Free-form description."),
    timeout_s: int = typer.Option(120, "--timeout", min=1, help="Default per-instance timeout in seconds."),
    script_id: str = typer.Option(None, "--id", help="Stable script id (defaults to a slug of --name)."),
    json_output: bool = json_flag(),
) -> None:
    """Create or update a script definition."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    scripts = service(ctx, "scripts")
    if file is not None:
        if str(file) == "-":
            content = sys.stdin.read()
        else:
            try:
                content = Path(file).read_text(encoding="utf-8")
            except OSError as exc:
                raise CliUsageError(f"could not read {file}: {exc}") from exc
    if not content:
        raise CliUsageError(
            "a script needs --content or --file",
            hint="adb-shell scripts take shell commands; host/python/appium scripts take the file body",
        )
    try:
        definition = ScriptDefinition(
            id=script_id or slugify(name),
            name=name,
            type=ScriptType(script_type),
            content=content,
            description=description,
            timeout_s=timeout_s,
        )
    except ValueError as exc:
        raise CliUsageError(f"invalid script definition: {exc}") from exc
    saved = invoke_callable(scripts.save_script, definition)
    saved = saved if saved is not None else definition
    payload = jsonable(saved)

    def render() -> None:
        out.success(f"script saved: {getattr(saved, 'id', '')}")
        out.kv(
            (
                ("id", getattr(saved, "id", "")),
                ("name", getattr(saved, "name", "")),
                ("type", str(getattr(getattr(saved, "type", ""), "value", getattr(saved, "type", "")))),
                ("timeout", getattr(saved, "timeout_s", "")),
            ),
            title="script definition",
        )

    out.emit(payload, render)


def scripts_delete(
    ctx: typer.Context,
    script_id: str = typer.Argument(..., help="Script id to delete."),
    json_output: bool = json_flag(),
) -> None:
    """Delete a script definition."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    scripts = service(ctx, "scripts")
    invoke_callable(scripts.delete_script, script_id)
    payload = {"deleted": script_id, "ok": True}
    out.emit(payload, lambda: out.success(f"script {script_id!r} deleted"))


# ---------------------------------------------------------------------------
# Fan-out runs
# ---------------------------------------------------------------------------


def _resolve_instance_names(context: Any, instances: list[str] | None) -> list[str]:
    names: list[str] = []
    for item in instances or []:
        names.extend(part.strip() for part in str(item).split(",") if part.strip())
    if names:
        return names
    avds = optional_service(context, "avds")
    if avds is not None and callable(getattr(avds, "list_instances", None)):
        names = [
            str(getattr(info, "name", ""))
            for info in invoke_callable(avds.list_instances) or []
            if getattr(info, "name", "")
        ]
    if not names:
        raise CliUsageError(
            "no target instances",
            hint="pass --instances NAME (repeatable) or deploy instances first",
        )
    return names


def scripts_run(
    ctx: typer.Context,
    script_id: str = typer.Argument(..., help="Script id to run."),
    instances: list[str] = typer.Option(None, "--instances", help="Target instance (repeatable/comma separated)."),
    concurrency: int = typer.Option(5, "--concurrency", min=1, max=64, help="Maximum parallel instances."),
    timeout_s: int = typer.Option(None, "--timeout", min=1, help="Override the per-instance timeout."),
    detach: bool = typer.Option(False, "--detach", help="Start the run in the background and return immediately."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate targets without executing anything."),
    json_output: bool = json_flag(),
) -> None:
    """Run a script across instances (synchronous unless --detach)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    scripts = service(ctx, "scripts")
    names = _resolve_instance_names(context, instances)
    request = ScriptRunRequest(
        script_id=script_id,
        instances=names,
        concurrency=concurrency,
        timeout_s=timeout_s,
        dry_run=dry_run,
    )
    if detach and not dry_run:
        run = invoke_callable(scripts.submit, request)
        payload = run_payload(run)

        def render_detached() -> None:
            out.success(f"run {getattr(run, 'id', '')} queued for {len(names)} instance(s)")
            out.info(f"follow with: avdfleet scripts runs status {getattr(run, 'id', '')}")

        out.emit(payload, render_detached)
        return
    run = invoke_callable(scripts.run, request)
    _render_run(out, run, title=f"script run - {script_id}")
    failed = int(getattr(run, "failed", 0) or 0)
    if dry_run:
        return
    if failed or str(getattr(getattr(run, "state", ""), "value", "")) in {"failed", "timed_out", "cancelled"}:
        raise typer.Exit(1)


def runs_list(
    ctx: typer.Context,
    limit: int = typer.Option(50, "--limit", min=1, max=500, help="Maximum number of runs."),
    json_output: bool = json_flag(),
) -> None:
    """List recent script runs."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    scripts = service(ctx, "scripts")
    runs = list(invoke_callable(scripts.list_runs, limit=limit) or [])
    _render_runs(out, runs, title="script runs")


def runs_status(
    ctx: typer.Context,
    run_id: str = typer.Argument(..., help="Run id."),
    json_output: bool = json_flag(),
) -> None:
    """Show one script run."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    scripts = service(ctx, "scripts")
    run = invoke_callable(scripts.get_run, run_id)
    _render_run(out, run, title=f"script run {run_id}")


def runs_cancel(
    ctx: typer.Context,
    run_id: str = typer.Argument(..., help="Run id to cancel."),
    json_output: bool = json_flag(),
) -> None:
    """Cancel a running script run."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    scripts = service(ctx, "scripts")
    run = invoke_callable(scripts.cancel, run_id)
    payload = run_payload(run)
    state = getattr(run, "state", "")
    state_text = str(getattr(state, "value", state))

    def render() -> None:
        out.success(f"run {run_id}: {state_text}")
        _render_run(out, run, title=f"script run {run_id}")

    out.emit(payload, render)


def register(app: typer.Typer) -> None:
    """Register the scripts command group (including scripts runs)."""
    scripts_app.command("list")(command_errors("scripts list")(scripts_list))
    scripts_app.command("create")(command_errors("scripts create")(scripts_create))
    scripts_app.command("delete")(command_errors("scripts delete")(scripts_delete))
    scripts_app.command("run")(command_errors("scripts run")(scripts_run))
    runs_app.command("list")(command_errors("scripts runs list")(runs_list))
    runs_app.command("status")(command_errors("scripts runs status")(runs_status))
    runs_app.command("cancel")(command_errors("scripts runs cancel")(runs_cancel))
    scripts_app.add_typer(runs_app, name="runs")
    app.add_typer(scripts_app, name="scripts")


__all__ = ["register", "run_payload", "runs_app", "scripts_app"]
