"""avdfleet install / uninstall / repair / status.

The installer engine (platform/installer) owns every step, checkpoint and
manifest; these commands only translate options and render results.  install
maps :class:`~avdfleet.core.errors.RebootRequiredError` to exit code **3010** so
the bootstrap script can reboot the host and call avdfleet install --resume.

Supplied-prompt flags (BRIEF section3.1)
----------------------------------------
``--no-studio``, ``--images``, ``--avd-home``, ``--proxies`` and
``--download-proxy`` are forwarded to :class:`InstallOptions`.  The CLI also
performs the post-install actions itself, defensively: every requested image is
downloaded through the images service and a proxies file seeds the proxy pool
through ``proxies.import_text``.  A missing service or a failed action prints a
warning, the remaining actions still run, and the command exits non-zero so the
bootstrap script notices.  ``--download-proxy`` is exported to installer child
processes (``HTTP_PROXY``/``HTTPS_PROXY``/``https_proxy``) only for the duration
of the run: the credential is never persisted and its warning is redacted.

Note: command modules deliberately avoid from __future__ import annotations
so Typer inspects real annotation objects (the @command_errors wrapper is
transparent via functools.wraps).
"""

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import typer

from avdfleet.core.logging import redact_text
from avdfleet.core.models import InstallOptions

from ..context import get_context, invoke_callable, service
from ..errors import command_errors
from ..output import json_flag, jsonable

#: Child-process proxy variables set while an install-time proxy is configured.
PROXY_ENV_KEYS: tuple[str, ...] = ("HTTP_PROXY", "HTTPS_PROXY", "https_proxy")


def _split_values(items: list[str] | None) -> list[str]:
    """Flatten repeatable/comma-separated option values, dropping blanks."""
    values: list[str] = []
    for item in items or []:
        values.extend(part.strip() for part in str(item).split(",") if part.strip())
    return values


def _mask_proxy_url(url: str) -> str:
    """Return *url* with any password replaced by ``***`` (never the secret)."""
    candidate = (url or "").strip()
    if not candidate:
        return ""
    try:
        parts = urlsplit(candidate)
        if not parts.password:
            return redact_text(candidate)
        hostname = parts.hostname or ""
        port_text = f":{parts.port}" if parts.port is not None else ""
        username = parts.username or ""
        userinfo = f"{username}:***" if username else "***"
        return urlunsplit(
            (parts.scheme, f"{userinfo}@{hostname}{port_text}", parts.path, parts.query, parts.fragment)
        )
    except ValueError:
        return redact_text(candidate)


@contextmanager
def _install_proxy_environment(proxy_url: str | None) -> Iterator[None]:
    """Export the install-time proxy to child processes, then restore it."""
    if not proxy_url:
        yield
        return
    previous = {key: os.environ.get(key) for key in PROXY_ENV_KEYS}
    for key in PROXY_ENV_KEYS:
        os.environ[key] = proxy_url
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _install_options(
    *,
    accept_licenses: bool = False,
    yes: bool = False,
    dry_run: bool = False,
    repair: bool = False,
    skip_appium: bool = False,
    skip_studio: bool = False,
    skip_scrcpy: bool = False,
    sdk_root: Path | None = None,
    only: list[str] | None = None,
    platform_provider: str | None = None,
    images: list[str] | None = None,
    avd_home: Path | None = None,
    proxies_file: Path | None = None,
    download_proxy: str | None = None,
    no_studio: bool = False,
) -> InstallOptions:
    return InstallOptions(
        accept_android_sdk_licenses=bool(accept_licenses),
        yes=bool(yes),
        dry_run=bool(dry_run),
        repair=bool(repair),
        skip_appium=bool(skip_appium),
        skip_studio=bool(skip_studio or no_studio),
        skip_scrcpy=bool(skip_scrcpy),
        sdk_root=str(sdk_root) if sdk_root is not None else None,
        only=_split_values(only),
        platform_provider=platform_provider,
        images=_split_values(images),
        avd_home=str(avd_home) if avd_home is not None else None,
        proxies_file=str(proxies_file) if proxies_file is not None else None,
        download_proxy=str(download_proxy) if download_proxy else None,
        no_studio=bool(no_studio),
    )


def _host_payload(ctx: Any) -> dict[str, Any]:
    context = get_context(ctx)
    host = None
    try:
        host = context.host_info()
    except Exception:  # noqa: BLE001 - status must work on a partially broken host
        host = None
    if host is None:
        return {"sdk_root": str(context.paths.sdk_root), "edition": context.config.edition}
    payload = jsonable(host)
    payload["sdk_root"] = str(context.paths.sdk_root)
    payload["edition"] = context.config.edition
    return payload


def _render_plan(out: Any, plan: Any, *, options: InstallOptions | None = None) -> None:
    payload = jsonable(plan)
    steps = list(getattr(plan, "steps", []) or [])

    def render() -> None:
        rows = []
        for step in steps:
            rows.append(
                (
                    str(getattr(step, "id", "")),
                    str(getattr(step, "title", "")),
                    "yes" if getattr(step, "requires_reboot", False) else "",
                    "critical" if getattr(step, "critical", True) else "optional",
                )
            )
        out.table(("step", "title", "reboot", "kind"), rows, title="install plan (dry run)")
        host = getattr(plan, "host", None)
        if host is not None:
            hypervisor = getattr(host, "hypervisor", None)
            kind = getattr(hypervisor, "kind", "none")
            kind = getattr(kind, "value", kind)
            out.info(
                "host: "
                f"{getattr(host, 'os_name', '') or getattr(host, 'os', '')} "
                f"{getattr(host, 'arch', '')} (acceleration: {kind})"
            )
        if getattr(plan, "already_installed", False):
            out.info("everything is already installed: a real run would be a no-op")
        if getattr(plan, "requires_reboot", False):
            out.warning("at least one step requires a host reboot")
        if options is not None:
            post_install = _planned_post_install(options)
            for line in post_install:
                out.info(line)
        out.info(f"{len(steps)} step(s) would run")

    out.emit(payload, render)


def _planned_post_install(options: InstallOptions) -> list[str]:
    """Describe the post-install actions a real run would perform."""
    lines: list[str] = []
    image_ids = list(options.images or [])
    if image_ids:
        lines.append(f"post-install: download {len(image_ids)} system image(s): {', '.join(image_ids)}")
    if options.proxies_file:
        lines.append(f"post-install: seed the proxy pool from {options.proxies_file}")
    return lines


def _manifest_summary(manifest: Any) -> tuple[dict[str, Any], bool]:
    payload = jsonable(manifest)
    steps = dict(getattr(manifest, "steps", {}) or {})
    failed: list[str] = []
    rows: list[tuple[str, str, str, str]] = []
    for step_id, result in steps.items():
        status = getattr(result, "status", "")
        status = str(getattr(status, "value", status))
        if status in {"failed", "reboot_required"}:
            failed.append(str(step_id))
        rows.append(
            (
                str(step_id),
                status,
                str(getattr(result, "duration_s", "") or ""),
                str(getattr(result, "message", "") or ""),
            )
        )
    completed = getattr(manifest, "completed_at", None) is not None
    ok = completed and not failed
    payload["ok"] = ok
    payload["failed_steps"] = failed
    return payload, ok


def _render_manifest(
    out: Any,
    manifest: Any,
    *,
    post_install: dict[str, Any] | None = None,
) -> bool:
    payload, ok = _manifest_summary(manifest)
    if post_install is not None:
        payload["post_install"] = post_install
    rows = [
        (
            str(step_id),
            str(getattr(getattr(result, "status", ""), "value", getattr(result, "status", ""))),
            str(getattr(result, "duration_s", "") or ""),
            str(getattr(result, "message", "") or ""),
        )
        for step_id, result in dict(getattr(manifest, "steps", {}) or {}).items()
    ]

    def render() -> None:
        out.table(("step", "status", "duration", "detail"), rows, title="install steps", status_columns=(1,))
        if ok:
            out.success("install completed")
        elif payload["failed_steps"]:
            out.error("install stopped: " + ", ".join(payload["failed_steps"]))
        else:
            out.warning("install did not complete")
        out.info(f"SDK root: {getattr(manifest, 'sdk_root', '') or ''}")
        version = getattr(manifest, "avdfleet_version", "") or ""
        if version:
            out.info(f"avdfleet: {version}")
        if post_install is not None:
            _render_post_install(out, post_install)

    out.emit(payload, render)
    return ok


def _render_post_install(out: Any, post_install: dict[str, Any]) -> None:
    for message in post_install.get("messages") or []:
        out.info(str(message))
    images = list(post_install.get("images") or [])
    if images:
        downloaded = sum(1 for image in images if image.get("ok"))
        out.info(f"post-install images: {downloaded}/{len(images)} downloaded")
    proxies = post_install.get("proxies")
    if isinstance(proxies, dict):
        out.info(
            "proxy pool: "
            f"{proxies.get('imported', 0)} imported, {proxies.get('skipped', 0)} duplicate(s) skipped"
        )
    if not bool(post_install.get("ok", True)):
        out.warning("one or more post-install actions failed")


def _seed_proxy_pool(
    context: Any,
    proxies_file: str,
    log: Callable[[str], None],
    fail: Callable[[str], None],
) -> dict[str, Any]:
    """Seed the proxy pool from *proxies_file* (report failures, never raise)."""
    summary: dict[str, Any] = {"file": proxies_file, "imported": 0, "skipped": 0, "errors": []}
    try:
        text = Path(proxies_file).read_text(encoding="utf-8")
    except OSError as exc:
        message = f"could not read the proxy file {proxies_file}: {exc}"
        summary["error"] = message
        fail(message)
        return summary
    pool = context.optional_service("proxies")
    if pool is None:
        message = "the proxies service is not available: the proxy pool was not seeded"
        summary["error"] = message
        fail(message)
        return summary
    try:
        result = invoke_callable(pool.import_text, text, source="install")
    except Exception as exc:  # noqa: BLE001 - a failed seed must not mask the install
        message = f"could not seed the proxy pool: {redact_text(str(exc)) or type(exc).__name__}"
        summary["error"] = message
        fail(message)
        return summary
    errors = [redact_text(str(error)) for error in (getattr(result, "errors", []) or [])]
    skipped = int(getattr(result, "skipped", 0) or 0)
    summary["imported"] = len(list(getattr(result, "added", []) or []))
    summary["skipped"] = skipped
    summary["errors"] = errors
    log(
        f"seeded {summary['imported']} proxy(ies) from {proxies_file}"
        + (f" ({skipped} duplicate(s) skipped)" if skipped else "")
    )
    for error in errors:
        fail(f"proxy import: {error}")
    return summary


def _post_install(context: Any, out: Any, options: InstallOptions) -> dict[str, Any]:
    """Run the post-install ``--images``/``--proxies`` actions defensively."""
    messages: list[str] = []
    report: dict[str, Any] = {"ok": True, "images": [], "proxies": None, "warnings": [], "messages": messages}

    def fail(message: str) -> None:
        report["ok"] = False
        report["warnings"].append(message)
        out.warning(message)

    image_ids = list(options.images or [])
    if image_ids:
        images = context.optional_service("images")
        if images is None:
            fail("the images service is not available: the requested system images were not downloaded")
        else:
            for image_id in image_ids:
                entry: dict[str, Any] = {"id": image_id, "ok": False}
                try:
                    image = invoke_callable(images.download, image_id)
                except Exception as exc:  # noqa: BLE001 - one image must not abort the others
                    message = redact_text(str(exc)) or type(exc).__name__
                    entry["error"] = message
                    fail(f"could not download image {image_id}: {message}")
                else:
                    entry["ok"] = True
                    entry["installed"] = bool(getattr(image, "installed", True))
                    messages.append(f"downloaded system image {image_id}")
                report["images"].append(entry)

    if options.proxies_file:
        report["proxies"] = _seed_proxy_pool(context, options.proxies_file, messages.append, fail)
    return report


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def install(
    ctx: typer.Context,
    accept_android_sdk_licenses: bool = typer.Option(
        False, "--accept-android-sdk-licenses", help="Accept the Android SDK licences (required for unattended installs)."
    ),
    yes: bool = typer.Option(False, "-y", "--yes", help="Assume yes for every confirmation."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the install plan and change nothing."),
    repair: bool = typer.Option(False, "--repair", help="Re-check and repair an existing installation."),
    resume: bool = typer.Option(False, "--resume", help="Resume after the reboot requested by the previous run."),
    no_appium: bool = typer.Option(False, "--no-appium", help="Skip Node/Appium/Inspector."),
    no_studio: bool = typer.Option(False, "--no-studio", help="Skip Android Studio installation/configuration."),
    no_scrcpy: bool = typer.Option(False, "--no-scrcpy", help="Skip scrcpy."),
    sdk_root: Path = typer.Option(None, "--sdk-root", help="Install/repair the SDK at this path."),
    avd_home: Path = typer.Option(None, "--avd-home", help="Shared AVD home directory (ANDROID_AVD_HOME)."),
    images: list[str] = typer.Option(
        None, "--images", help="System image ids to download after install (repeatable/comma separated)."
    ),
    proxies_file: Path = typer.Option(
        None, "--proxies", help="Proxy URL list (one per line) that seeds the proxy pool after install."
    ),
    download_proxy: str = typer.Option(
        None,
        "--download-proxy",
        help="Install-time HTTP(S) proxy for installer downloads (used for this run only; never persisted).",
    ),
    only: list[str] = typer.Option(None, "--only", help="Only run these step ids (repeatable/comma separated)."),
    platform_provider: str = typer.Option(
        None, "--platform-provider", help="Force a platform provider (windows/macos/debian/fedora/arch)."
    ),
    json_output: bool = json_flag(),
) -> None:
    """Install the complete Android toolchain (idempotent; dry-run supported)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    options = _install_options(
        accept_licenses=accept_android_sdk_licenses,
        yes=yes,
        dry_run=dry_run,
        repair=repair,
        skip_appium=no_appium,
        no_studio=no_studio,
        skip_studio=no_studio,
        skip_scrcpy=no_scrcpy,
        sdk_root=sdk_root,
        only=only,
        platform_provider=platform_provider,
        images=images,
        avd_home=avd_home,
        proxies_file=proxies_file,
        download_proxy=download_proxy,
    )
    if options.download_proxy:
        out.warning(
            f"using the install-time download proxy {_mask_proxy_url(options.download_proxy)} "
            "for installer child processes; the credential is exported for this run only and is not persisted"
        )
    engine = service(ctx, "installer")
    if dry_run:
        _render_plan(out, invoke_callable(engine.plan, options), options=options)
        return
    with _install_proxy_environment(options.download_proxy):
        if resume:
            manifest = invoke_callable(engine.resume, options)
        elif repair:
            manifest = invoke_callable(engine.repair, options)
        else:
            manifest = invoke_callable(engine.install, options)
        install_ok = _manifest_summary(manifest)[1]
        run_post_install = install_ok and bool(options.images or options.proxies_file)
        post_install = _post_install(context, out, options) if run_post_install else None
    if not _render_manifest(out, manifest, post_install=post_install):
        raise typer.Exit(1)
    if post_install is not None and not bool(post_install.get("ok", True)):
        raise typer.Exit(1)


def repair_command(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "-y", "--yes", help="Assume yes for every confirmation."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the repair plan and change nothing."),
    accept_android_sdk_licenses: bool = typer.Option(
        False, "--accept-android-sdk-licenses", help="Accept the Android SDK licences."
    ),
    sdk_root: Path = typer.Option(None, "--sdk-root", help="Repair the SDK at this path."),
    json_output: bool = json_flag(),
) -> None:
    """Re-check and repair an existing installation (install --repair)."""
    out = get_context(ctx).output(json_output=json_output)
    options = _install_options(
        accept_licenses=accept_android_sdk_licenses,
        yes=yes,
        dry_run=dry_run,
        repair=True,
        sdk_root=sdk_root,
    )
    engine = service(ctx, "installer")
    if dry_run:
        _render_plan(out, invoke_callable(engine.plan, options))
        return
    manifest = invoke_callable(engine.repair, options)
    if not _render_manifest(out, manifest):
        raise typer.Exit(1)


def uninstall(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run", help="Only report what would be removed."),
    keep_data: bool = typer.Option(False, "--keep-data", help="Keep the data directory (AVDs, database, logs)."),
    json_output: bool = json_flag(),
) -> None:
    """Remove installer-managed files and environment changes."""
    out = get_context(ctx).output(json_output=json_output)
    engine = service(ctx, "installer")
    result = invoke_callable(engine.uninstall, dry_run=dry_run, keep_data=keep_data)
    out.operation(result, title="uninstall")
    if not bool(getattr(result, "ok", True)):
        raise typer.Exit(1)


def status(
    ctx: typer.Context,
    json_output: bool = json_flag(),
) -> None:
    """Show installer status, host facts and SDK locations."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    engine = context.optional_service("installer")
    manifest = None
    if engine is not None:
        candidate = getattr(engine, "status", None)
        if callable(candidate):
            manifest = invoke_callable(candidate)
    host = _host_payload(ctx)
    installed = bool(manifest is not None and getattr(manifest, "completed_at", None) is not None)
    payload: dict[str, Any] = {
        "installed": installed,
        "edition": host.get("edition", context.config.edition),
        "version": str(getattr(manifest, "avdfleet_version", "") or ""),
        "sdk_root": host.get("sdk_root", str(context.paths.sdk_root)),
        "data_dir": str(context.paths.data_dir),
        "host": host,
        "manifest": jsonable(manifest) if manifest is not None else None,
    }

    def render() -> None:
        raw_hypervisor = host.get("hypervisor")
        hypervisor: dict[str, Any] = raw_hypervisor if isinstance(raw_hypervisor, dict) else {}
        out.kv(
            (
                ("installed", "yes" if installed else "no - run 'avdfleet install'"),
                ("edition", payload["edition"]),
                ("avdfleet", payload["version"] or "unknown"),
                ("sdk root", payload["sdk_root"]),
                ("data dir", payload["data_dir"]),
                (
                    "host",
                    f"{host.get('os_name') or host.get('os', '')} {host.get('arch', '')}".strip(),
                ),
                ("acceleration", str(hypervisor.get("kind") or "see 'avdfleet doctor'")),
            ),
            title="avdfleet status",
        )

    out.emit(payload, render)


def register(app: typer.Typer) -> None:
    """Register the installer commands on the root application."""
    app.command("install")(command_errors("install")(install))
    app.command("repair")(command_errors("repair")(repair_command))
    app.command("uninstall")(command_errors("uninstall")(uninstall))
    app.command("status")(command_errors("status")(status))


__all__ = ["install", "register", "repair_command", "status", "uninstall"]
