"""avdfleet bundle and avdfleet portable - portable-edition tooling.

The portable package (portable/) owns bundle building, verification,
relocation and the preflight checks.  The commands prefer methods on the
portable service (container.portable()) and fall back to the package
functions when a build does not expose the optional helpers (the API uses the
same fallback strategy).
"""

import os
from pathlib import Path
from typing import Any

import typer

from ..context import get_context, invoke_callable, optional_service
from ..errors import CliServiceError, command_errors
from ..output import json_flag, jsonable

portable_app = typer.Typer(help="Portable-edition checks, verification and relocation.", no_args_is_help=True)


def _portable_service(context: Any) -> Any | None:
    return optional_service(context, "portable")


def _default_root(context: Any) -> Path:
    paths = context.paths
    if bool(getattr(paths, "portable", False)):
        return Path(paths.root)
    return Path(os.getcwd())


def _portable_checks(context: Any, root: Path) -> list[dict[str, Any]]:
    service = _portable_service(context)
    if service is not None:
        for name in ("checks", "run_checks", "preflight"):
            candidate = getattr(service, name, None)
            if not callable(candidate):
                continue
            try:
                raw = invoke_callable(candidate)
            except Exception:  # noqa: BLE001 - fall back to the package probe
                continue
            if isinstance(raw, (list, tuple)):
                return [jsonable(item) if not isinstance(item, dict) else jsonable(item) for item in raw]
            checks = getattr(raw, "checks", None)
            if checks is not None:
                return [jsonable(item) if not isinstance(item, dict) else jsonable(item) for item in checks]
    try:
        from avdfleet.portable.checks import run_preflight  # noqa: PLC0415 - optional fallback
    except ImportError as exc:  # pragma: no cover - core install always has it
        raise CliServiceError(
            "portable preflight checks are not available in this build",
            service="portable",
        ) from exc
    report = run_preflight(root, include_write_speed=False)
    return [jsonable(check) if not isinstance(check, dict) else jsonable(check) for check in report.checks]


def _check_rows(checks: list[dict[str, Any]]) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for check in checks:
        status = str(check.get("status", ""))
        status = getattr(status, "value", status)
        rows.append(
            (
                status,
                str(check.get("id", "")),
                str(check.get("title", "")),
                str(check.get("detail", "") or check.get("value", "") or ""),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# bundle
# ---------------------------------------------------------------------------


def bundle_build(
    ctx: typer.Context,
    offline: bool = typer.Option(False, "--offline", help="Include the offline cache (no network on first run)."),
    output: Path = typer.Option(
        None, "-o", "--output", help="Destination directory (default: ./avdfleet-portable)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the bundle manifest preview and change nothing."),
    force: bool = typer.Option(False, "--force", help="Replace an existing bundle directory."),
    json_output: bool = json_flag(),
) -> None:
    """Build a self-contained portable bundle."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    destination = Path(output) if output is not None else Path(os.getcwd()) / "avdfleet-portable"
    service = _portable_service(context)
    if service is None:
        raise CliServiceError(
            "the portable bundle builder is not available in this build",
            service="portable",
            hint="reinstall with the full feature set",
        )
    manifest = invoke_callable(service.build, destination, offline=offline, dry_run=dry_run, force=force)
    payload = jsonable(manifest)
    entries = list(getattr(manifest, "entries", []) or [])
    warnings = list(getattr(manifest, "warnings", []) or [])

    def render() -> None:
        if dry_run:
            out.info("bundle build (dry run): nothing was written")
        else:
            out.success(f"portable bundle built at {destination}")
        out.kv(
            (
                ("name", getattr(manifest, "name", "")),
                ("version", getattr(manifest, "version", "")),
                ("host", f"{getattr(manifest, 'host_os', '')}/{getattr(manifest, 'host_arch', '')}"),
                ("offline", "yes" if getattr(manifest, "offline", False) else "no"),
                ("entries", len(entries)),
                ("total bytes", getattr(manifest, "total_bytes", "") or ""),
            ),
            title="bundle manifest",
        )
        for warning in warnings:
            out.warning(str(warning))

    out.emit(payload, render)


# ---------------------------------------------------------------------------
# portable checks / verify / relocate / status
# ---------------------------------------------------------------------------


def portable_checks(
    ctx: typer.Context,
    root: Path = typer.Option(None, "--root", help="Bundle root (default: the portable root or the current directory)."),
    json_output: bool = json_flag(),
) -> None:
    """Run the portable-edition preflight checks (filesystem, noexec, space...)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    resolved = Path(root) if root is not None else _default_root(context)
    checks = _portable_checks(context, resolved)
    failed = [check for check in checks if str(check.get("status", "")).lower() == "fail"]
    payload = {"root": str(resolved), "ok": not failed, "checks": checks}

    def render() -> None:
        out.table(("status", "id", "check", "detail"), _check_rows(checks), title=f"portable checks - {resolved}", status_columns=(0,))
        out.info(f"{len(checks)} check(s), {len(failed)} failure(s)")

    out.emit(payload, render)
    if failed:
        raise typer.Exit(1)


def portable_verify(
    ctx: typer.Context,
    root: Path = typer.Argument(None, help="Bundle root (default: the portable root or the current directory)."),
    json_output: bool = json_flag(),
) -> None:
    """Verify a portable bundle against its checksum manifest."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    resolved = Path(root) if root is not None else _default_root(context)
    service = _portable_service(context)
    if service is None or not callable(getattr(service, "verify", None)):
        raise CliServiceError("portable bundle verification is not available", service="portable")
    manifest = invoke_callable(service.verify, resolved)
    payload = jsonable(manifest)
    entries = list(getattr(manifest, "entries", []) or [])

    def render() -> None:
        out.success(f"bundle verified: {resolved}")
        out.info(f"{len(entries)} file(s) match the manifest")

    out.emit(payload, render)


def portable_relocate(
    ctx: typer.Context,
    root: Path = typer.Argument(None, help="Bundle root (default: the portable root or the current directory)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the rewrites without changing anything."),
    json_output: bool = json_flag(),
) -> None:
    """Rewrite recorded absolute paths after the bundle moved or changed drive."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    resolved = Path(root) if root is not None else _default_root(context)
    service = _portable_service(context)
    relocate = getattr(service, "relocate", None) if service is not None else None
    if callable(relocate):
        report = invoke_callable(relocate, resolved, dry_run=dry_run)
    else:
        try:
            from avdfleet.portable.relocate import relocate_bundle  # noqa: PLC0415 - optional fallback
        except ImportError as exc:  # pragma: no cover
            raise CliServiceError("portable relocation is not available", service="portable") from exc
        report = relocate_bundle(resolved, dry_run=dry_run)
    payload = jsonable(report)
    changes = list(getattr(report, "changes", []) or [])
    warnings = list(getattr(report, "warnings", []) or [])

    def render() -> None:
        title = f"relocate {'(dry run) ' if dry_run else ''}- {resolved}"
        out.kv(
            (
                ("root", getattr(report, "root", resolved)),
                ("old roots", ", ".join(getattr(report, "old_roots", []) or []) or "(none recorded)"),
                ("changed rows", len(changes)),
                ("changed files", getattr(report, "changed_files", "") or ""),
                ("manifest updated", "yes" if getattr(report, "manifest_updated", False) else "no"),
                ("dry run", "yes" if getattr(report, "dry_run", dry_run) else "no"),
            ),
            title=title,
        )
        for warning in warnings:
            out.warning(str(warning))

    out.emit(payload, render)


def portable_status(
    ctx: typer.Context,
    json_output: bool = json_flag(),
) -> None:
    """Show portable-edition status (root, marker, manifest, leftovers)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    paths = context.paths
    service = _portable_service(context)
    root = _default_root(context)
    status_fn = getattr(service, "status", None) if service is not None else None
    if callable(status_fn):
        payload = jsonable(invoke_callable(status_fn))
    else:
        marker = Path(getattr(paths, "portable_marker", Path(root) / "avdfleet-portable.json"))
        payload = {
            "edition": str(getattr(context.config, "edition", "installed")),
            "root": str(root),
            "portable": bool(getattr(paths, "portable", False)),
            "relocatable": bool(getattr(paths, "portable", False)) and marker.is_file(),
            "marker": str(marker),
            "leftovers": [],
        }
        inspect = getattr(service, "inspect", None) if service is not None else None
        if callable(inspect):
            try:
                manifest = invoke_callable(inspect, root)
                payload["manifest"] = jsonable(manifest)
                payload["relocatable"] = True
            except Exception:  # noqa: BLE001 - a missing manifest is not an error for status
                payload["manifest"] = None

    def render() -> None:
        out.kv(
            [
                (str(key), jsonable(value))
                for key, value in payload.items()
                if not isinstance(jsonable(value), (dict, list))
            ],
            title="portable status",
        )
        manifest = payload.get("manifest")
        if isinstance(manifest, dict):
            out.kv(
                (
                    ("bundle version", manifest.get("version", "")),
                    ("offline", "yes" if manifest.get("offline") else "no"),
                    ("files", len(manifest.get("entries", []) or [])),
                ),
                title="bundle manifest",
            )

    out.emit(payload, render)


def register(app: typer.Typer) -> None:
    app.command("bundle")(command_errors("bundle")(bundle_build))
    portable_app.command("checks")(command_errors("portable checks")(portable_checks))
    portable_app.command("verify")(command_errors("portable verify")(portable_verify))
    portable_app.command("relocate")(command_errors("portable relocate")(portable_relocate))
    portable_app.command("status")(command_errors("portable status")(portable_status))
    app.add_typer(portable_app, name="portable")


__all__ = ["bundle_build", "portable_app", "register"]
