"""avdfleet doctor - environment checks with safe fixes.

The doctor service (platform/doctor.py) owns every check; this command only
renders the report, optionally applies the safe fixes and re-runs the checks.
Exit code is 0 when no check failed, 1 otherwise (an environment issue).
"""

from typing import Any

import typer

from avdfleet.core.models import CheckStatus

from ..context import get_context, invoke_callable, service
from ..errors import command_errors
from ..output import json_flag, jsonable

_STATUS_LABELS = {
    CheckStatus.OK: "ok",
    CheckStatus.WARN: "warn",
    CheckStatus.FAIL: "fail",
    CheckStatus.SKIP: "skip",
}


def _check_rows(report: Any) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for check in getattr(report, "checks", []) or []:
        status = getattr(check, "status", "")
        status = str(getattr(status, "value", status))
        detail = str(getattr(check, "detail", "") or "")
        hint = str(getattr(check, "hint", "") or "")
        if hint and status in {"fail", "warn"}:
            detail = f"{detail} - {hint}" if detail else hint
        rows.append(
            (
                status,
                str(getattr(check, "id", "")),
                str(getattr(check, "title", "")),
                detail,
            )
        )
    return rows


def _summary(report: Any) -> dict[str, int]:
    counts = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
    for check in getattr(report, "checks", []) or []:
        status = getattr(check, "status", "")
        status = str(getattr(status, "value", status))
        if status in counts:
            counts[status] += 1
    return counts


def _fix_results(ctx: Any, fixes: list[str], *, dry_run: bool) -> list[Any]:
    doctor = service(ctx, "doctor")
    results: list[Any] = []
    for check_id in fixes:
        for part in str(check_id).split(","):
            identifier = part.strip()
            if not identifier:
                continue
            results.append(invoke_callable(doctor.fix, identifier, dry_run=dry_run))
    return results


def doctor(
    ctx: typer.Context,
    slow: bool = typer.Option(False, "--slow", help="Include the slow checks (tool versions, ports, clock...)."),
    fix: list[str] = typer.Option(None, "--fix", help="Apply the safe fix for this check id (repeatable)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="With --fix: describe the change without applying it."),
    json_output: bool = json_flag(),
) -> None:
    """Run environment checks and optionally apply safe fixes."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    doctor_service = service(ctx, "doctor")
    report = invoke_callable(doctor_service.run, include_slow=slow)
    fix_results: list[Any] = []
    if fix:
        fix_results = _fix_results(ctx, fix, dry_run=dry_run)
        if not dry_run:
            report = invoke_callable(doctor_service.run, include_slow=slow)

    counts = _summary(report)
    ok = bool(getattr(report, "ok", False))
    payload: dict[str, Any] = {
        "ok": ok,
        "summary": counts,
        "report": jsonable(report),
    }
    if fix_results:
        payload["fixes"] = [jsonable(result) for result in fix_results]

    def render() -> None:
        host = getattr(report, "host", None)
        if host is not None:
            out.info(
                "host: "
                f"{getattr(host, 'os_name', '') or getattr(host, 'os', '')} "
                f"{getattr(host, 'arch', '')} | python {getattr(host, 'python_version', '')} | "
                f"ram {getattr(host, 'total_ram_gb', 0):.1f} GiB | "
                f"free disk {getattr(host, 'free_disk_gb', 0):.1f} GiB"
            )
        for result in fix_results:
            out.operation(result, title="fix")
        out.table(
            ("status", "id", "check", "detail"),
            _check_rows(report),
            title="doctor",
            status_columns=(0,),
        )
        out.info(
            "summary: "
            f"{counts['ok']} ok, {counts['warn']} warn, {counts['fail']} fail, {counts['skip']} skip"
        )
        if ok:
            out.success("environment is healthy")
        else:
            out.error("environment checks failed - see the hints above")

    out.emit(payload, render)
    if not ok:
        raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    app.command("doctor")(command_errors("doctor")(doctor))


__all__ = ["doctor", "register"]
