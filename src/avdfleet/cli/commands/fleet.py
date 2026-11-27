"""avdfleet fleet plan|apply|validate - declarative fleet reconciliation.

The fleet engine (fleetspec/) owns loading, validation, planning and the
idempotent apply; these commands only select the file, call the service and
render the diff/plan.

The supplied prompt (BRIEF section2) requires the top-level spellings
``avdfleet apply -f fleet.yaml`` and ``avdfleet plan -f fleet.yaml``; those are
registered as aliases of ``fleet apply``/``fleet plan`` and both spellings stay
available.
"""

from pathlib import Path
from typing import Any

import typer

from ..context import get_context, invoke_callable, service
from ..errors import CliUsageError, command_errors
from ..output import json_flag, jsonable

fleet_app = typer.Typer(help="Plan and apply fleet.yaml definitions.", no_args_is_help=True)


def _load(ctx: Any, path: Path) -> Any:
    fleet = service(ctx, "fleet")
    if not Path(path).is_file():
        raise CliUsageError(
            f"fleet spec not found: {path}",
            hint="pass -f/--file PATH to a fleet.yaml (see contracts/fleet.schema.json)",
        )
    return fleet, invoke_callable(fleet.load, path)


def _plan_payload(plan: Any) -> dict[str, Any]:
    return jsonable(plan)


def _exit_for_plan(plan: Any) -> None:
    errors = list(getattr(plan, "errors", []) or [])
    if errors:
        raise typer.Exit(1)
    if not bool(getattr(plan, "applyable", True)):
        raise typer.Exit(1)


def fleet_plan(
    ctx: typer.Context,
    file: Path = typer.Option(..., "-f", "--file", help="fleet.yaml document to plan."),
    prune: bool = typer.Option(False, "--prune", help="Include deletion of resources not in the spec."),
    json_output: bool = json_flag(),
) -> None:
    """Diff a fleet.yaml against the current fleet (read-only)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    fleet, spec = _load(ctx, file)
    plan = invoke_callable(fleet.plan, spec, prune=prune)
    out.plan(plan, title=f"fleet plan - {file}")
    _exit_for_plan(plan)


def fleet_apply(
    ctx: typer.Context,
    file: Path = typer.Option(..., "-f", "--file", help="fleet.yaml document to reconcile."),
    prune: bool = typer.Option(False, "--prune", help="Delete resources that are not in the spec."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan only; change nothing."),
    json_output: bool = json_flag(),
) -> None:
    """Reconcile the fleet to fleet.yaml (idempotent; --dry-run supported)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    fleet, spec = _load(ctx, file)
    plan = invoke_callable(fleet.apply, spec, prune=prune, dry_run=dry_run)
    title = f"fleet apply{' (dry run)' if dry_run else ''} - {file}"
    out.plan(plan, title=title)
    if dry_run:
        return
    _exit_for_plan(plan)
    errors = list(getattr(plan, "errors", []) or [])
    if errors:
        raise typer.Exit(1)


def fleet_validate(
    ctx: typer.Context,
    file: Path = typer.Option(..., "-f", "--file", help="fleet.yaml document to validate."),
    json_output: bool = json_flag(),
) -> None:
    """Validate a fleet.yaml against the schema and live image requirements."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    fleet, spec = _load(ctx, file)
    problems = [str(item) for item in (invoke_callable(fleet.validate, spec) or [])]
    payload = {"file": str(file), "ok": not problems, "errors": problems}

    def render() -> None:
        if problems:
            out.error(f"{file}: {len(problems)} problem(s)")
            for problem in problems:
                out.write(f"  - {problem}")
        else:
            out.success(f"{file}: valid fleet spec")
            templates = list(getattr(spec, "templates", []) or [])
            instances = list(getattr(spec, "instances", []) or [])
            out.info(f"{len(templates)} template(s), {len(instances)} instance(s)")

    out.emit(payload, render)
    if problems:
        raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    fleet_app.command("plan")(command_errors("fleet plan")(fleet_plan))
    fleet_app.command("apply")(command_errors("fleet apply")(fleet_apply))
    fleet_app.command("validate")(command_errors("fleet validate")(fleet_validate))
    # Top-level aliases explicitly required by the supplied prompt (BRIEF section2).
    app.command("plan")(command_errors("plan")(fleet_plan))
    app.command("apply")(command_errors("apply")(fleet_apply))
    app.add_typer(fleet_app, name="fleet")


__all__ = ["fleet_app", "register"]
