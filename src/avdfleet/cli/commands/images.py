"""avdfleet images and avdfleet oobe - system images and OOBE matrix."""

from pathlib import Path
from typing import Any

import typer

from ..context import get_context, invoke_callable, optional_service, service
from ..errors import CliServiceError, command_errors
from ..output import json_flag, jsonable

images_app = typer.Typer(help="Catalogue, download and import Android system images.", no_args_is_help=True)
oobe_app = typer.Typer(help="Out-of-box-experience matrix, verification and reset.", no_args_is_help=True)


# ---------------------------------------------------------------------------
# Shared renderers
# ---------------------------------------------------------------------------


def _image_row(image: Any) -> tuple[str, str, str, str, str, str, str]:
    status = getattr(image, "oobe_status", "")
    return (
        str(getattr(image, "id", "")),
        str(getattr(image, "api_level", "") or ""),
        str(getattr(image, "abi", "") or ""),
        str(getattr(image, "tag", "") or getattr(image, "kind", "") or ""),
        "yes" if getattr(image, "installed", False) else "",
        str(getattr(status, "value", status)),
        "custom" if getattr(image, "custom", False) else str(getattr(image, "kind", "")),
    )


def _render_images(out: Any, images: list[Any], *, title: str) -> None:
    payload = jsonable(images)

    def render() -> None:
        out.table(
            ("id", "api", "abi", "tag", "installed", "oobe", "kind"),
            [_image_row(image) for image in images],
            title=title,
            status_columns=(5,),
        )
        out.info(f"{len(images)} image(s)")

    out.emit(payload, render)


def _render_image(out: Any, image: Any, *, title: str) -> None:
    payload = jsonable(image)

    def render() -> None:
        out.kv(
            (
                ("id", getattr(image, "id", "")),
                ("api level", getattr(image, "api_level", "")),
                ("abi", getattr(image, "abi", "")),
                ("tag", getattr(image, "tag", "")),
                ("kind", getattr(image, "kind", "")),
                ("installed", "yes" if getattr(image, "installed", False) else "no"),
                ("oobe", getattr(getattr(image, "oobe_status", ""), "value", getattr(image, "oobe_status", ""))),
                ("package", getattr(image, "package", "") or ""),
                ("path", getattr(image, "local_path", "") or ""),
                ("size", getattr(image, "size_bytes", "") or ""),
            ),
            title=title,
        )

    out.emit(payload, render)


# ---------------------------------------------------------------------------
# images list / download / import / refresh
# ---------------------------------------------------------------------------


def images_list(
    ctx: typer.Context,
    installed: bool = typer.Option(False, "-i", "--installed", help="Only images already installed."),
    remote: bool = typer.Option(True, "--remote/--no-remote", help="Include catalogue entries that are not installed."),
    api_level: int = typer.Option(None, "--api-level", min=1, help="Filter by API level."),
    abi: str = typer.Option(None, "--abi", help="Filter by ABI (e.g. x86_64, arm64-v8a)."),
    tag: str = typer.Option(None, "--tag", help="Filter by image tag (e.g. google_apis, playstore)."),
    json_output: bool = json_flag(),
) -> None:
    """List system images from the catalogue and the installed SDK."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    images = service(ctx, "images")
    items = list(invoke_callable(images.list_images, include_remote=bool(remote), installed_only=bool(installed)) or [])
    if api_level is not None:
        items = [image for image in items if int(getattr(image, "api_level", 0) or 0) == int(api_level)]
    if abi:
        items = [image for image in items if str(getattr(image, "abi", "")).lower() == abi.lower()]
    if tag:
        needle = tag.lower()
        items = [
            image
            for image in items
            if needle in str(getattr(image, "tag", "")).lower()
            or needle in str(getattr(image, "kind", "")).lower()
            or needle in str(getattr(image, "id", "")).lower()
        ]
    _render_images(out, items, title="system images")


def images_download(
    ctx: typer.Context,
    image_id: str = typer.Argument(..., help="Image id or unique id fragment from 'avdfleet images list'."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Resolve the image and stop before downloading."),
    json_output: bool = json_flag(),
) -> None:
    """Download and install a system image through the SDK package manager."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    images = service(ctx, "images")
    image = invoke_callable(images.download, image_id, dry_run=dry_run)
    _render_image(out, image, title="image download" + (" (dry run)" if dry_run else ""))


def images_import(
    ctx: typer.Context,
    path: Path = typer.Argument(..., help="Directory containing a custom system image."),
    name: str = typer.Option(None, "--name", help="Display name for the imported image."),
    image_id: str = typer.Option(None, "--id", help="Stable id to register the image under."),
    api_level: int = typer.Option(None, "--api-level", min=1, help="API level when it cannot be detected."),
    abi: str = typer.Option(None, "--abi", help="ABI when it cannot be detected."),
    force: bool = typer.Option(False, "--force", help="Replace an existing custom image with the same id."),
    json_output: bool = json_flag(),
) -> None:
    """Import a custom system image from a host directory."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    images = service(ctx, "images")
    image = invoke_callable(
        images.import_directory,
        path,
        name=name,
        image_id=image_id,
        api_level=api_level,
        abi=abi,
        force=force,
    )
    _render_image(out, image, title="image import")


def images_refresh(
    ctx: typer.Context,
    timeout_s: int = typer.Option(60, "--timeout", min=1, help="Timeout for the SDK catalogue refresh."),
    json_output: bool = json_flag(),
) -> None:
    """Refresh the remote image catalogue from the SDK."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    images = service(ctx, "images")
    items = list(invoke_callable(images.refresh, timeout_s=timeout_s) or [])
    _render_images(out, items, title="system images (refreshed)")


def images_get(
    ctx: typer.Context,
    image_id: str = typer.Argument(..., help="Image id to describe."),
    json_output: bool = json_flag(),
) -> None:
    """Show one system image."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    images = service(ctx, "images")
    image = invoke_callable(images.get_image, image_id)
    _render_image(out, image, title="system image")


# ---------------------------------------------------------------------------
# oobe
# ---------------------------------------------------------------------------


def _matrix(ctx: Any) -> Any:
    oobe = optional_service(ctx, "oobe")
    if oobe is not None:
        matrix_fn = getattr(oobe, "matrix", None)
        if callable(matrix_fn):
            return invoke_callable(matrix_fn)
    images = optional_service(ctx, "images")
    if images is not None:
        matrix_fn = getattr(images, "oobe_matrix", None)
        if callable(matrix_fn):
            return invoke_callable(matrix_fn)
    raise CliServiceError(
        "the OOBE matrix is not available in this build",
        service="oobe",
        hint="reinstall with the full feature set or check the oobe_matrix.json file",
    )


def _render_matrix(out: Any, matrix: Any, *, title: str) -> None:
    records = list(getattr(matrix, "records", []) or [])
    payload = jsonable(matrix)

    def render() -> None:
        rows = []
        for record in records:
            status = getattr(record, "status", "")
            rows.append(
                (
                    str(getattr(record, "image_id", "")),
                    str(getattr(record, "api_level", "") or ""),
                    str(getattr(record, "abi", "") or ""),
                    str(getattr(status, "value", status)),
                    str(getattr(record, "activity", "") or ""),
                    str(getattr(record, "boot_seconds", "") or ""),
                    str(getattr(record, "reset_seconds", "") or ""),
                )
            )
        out.table(
            ("image", "api", "abi", "oobe", "activity", "boot s", "reset s"),
            rows,
            title=title,
            status_columns=(3,),
        )
        out.info(f"{len(records)} record(s)")

    out.emit(payload, render)


def oobe_matrix(
    ctx: typer.Context,
    json_output: bool = json_flag(),
) -> None:
    """Print the OOBE verification matrix (checked-in + local results)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    _render_matrix(out, _matrix(ctx), title="OOBE matrix")


def oobe_verify(
    ctx: typer.Context,
    image_id: str = typer.Argument(..., help="Image id to verify (boots a temporary AVD)."),
    timeout_s: int = typer.Option(None, "--timeout", min=30, help="Boot/verification timeout in seconds."),
    keep_avd: bool = typer.Option(False, "--keep-avd", help="Keep the temporary AVD for inspection."),
    json_output: bool = json_flag(),
) -> None:
    """Verify Out-of-Box-Experience on an image (boots a temporary AVD)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    oobe = service(ctx, "oobe")
    record = invoke_callable(oobe.verify_image, image_id, timeout_s=timeout_s, keep_avd=keep_avd)
    payload = jsonable(record)

    def render() -> None:
        out.kv(
            (
                ("image", getattr(record, "image_id", "")),
                ("status", getattr(getattr(record, "status", ""), "value", getattr(record, "status", ""))),
                ("activity", getattr(record, "activity", "") or ""),
                ("boot seconds", getattr(record, "boot_seconds", "") or ""),
                ("checked at", getattr(record, "checked_at", "") or ""),
                ("notes", getattr(record, "notes", "") or ""),
            ),
            title="OOBE verification",
        )

    out.emit(payload, render)


def oobe_reset(
    ctx: typer.Context,
    avd_name: str = typer.Argument(..., help="Instance name to reset to the out-of-box experience."),
    strategy: str = typer.Option(None, "--strategy", help="Reset strategy: snapshot, wipe, package or reboot."),
    verify: bool = typer.Option(True, "--verify/--no-verify", help="Verify the Setup Wizard afterwards."),
    json_output: bool = json_flag(),
) -> None:
    """Reset an instance to the out-of-box experience."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    oobe = service(ctx, "oobe")
    result = invoke_callable(oobe.reset, avd_name, strategy=strategy, verify=verify)
    payload = jsonable(result)

    def render() -> None:
        ok = bool(getattr(result, "ok", False))
        (out.success if ok else out.error)(
            f"{avd_name}: OOBE reset "
            f"{'succeeded' if ok else 'failed'} "
            f"({getattr(getattr(result, 'strategy', ''), 'value', getattr(result, 'strategy', ''))}, "
            f"{getattr(result, 'seconds', '') or '?'}s)"
        )
        activity = getattr(result, "activity", "") or ""
        if activity:
            out.info(f"top activity: {activity}")
        message = getattr(result, "message", "") or ""
        if message:
            out.info(message)

    out.emit(payload, render)
    if not bool(getattr(result, "ok", False)):
        raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    """Register images and oobe command groups on the root app."""
    images_app.command("list")(command_errors("images list")(images_list))
    images_app.command("get")(command_errors("images get")(images_get))
    images_app.command("download")(command_errors("images download")(images_download))
    images_app.command("import")(command_errors("images import")(images_import))
    images_app.command("refresh")(command_errors("images refresh")(images_refresh))
    oobe_app.command("matrix")(command_errors("oobe matrix")(oobe_matrix))
    oobe_app.command("verify")(command_errors("oobe verify")(oobe_verify))
    oobe_app.command("reset")(command_errors("oobe reset")(oobe_reset))
    app.add_typer(images_app, name="images")
    app.add_typer(oobe_app, name="oobe")


__all__ = [
    "images_app",
    "images_download",
    "images_get",
    "images_import",
    "images_list",
    "images_refresh",
    "oobe_app",
    "oobe_matrix",
    "oobe_reset",
    "oobe_verify",
    "register",
]
