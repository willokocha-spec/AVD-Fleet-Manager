"""avdfleet proxies - pool, assignments and egress verification.

Security invariant (BRIEF section3.4): credentials are write-only through the CLI.
Listings and command output go through the service's redacted view (or an
equivalent local view) and never include a password; :func:`exit_code_for`
reporting is redacted too.
"""

import sys
from pathlib import Path
from typing import Any

import typer

from ..context import get_context, invoke_callable, optional_service, service
from ..errors import CliServiceError, CliUsageError, command_errors
from ..output import json_flag, jsonable

proxies_app = typer.Typer(help="Manage authenticated proxies, assignments and bridges.", no_args_is_help=True)


# ---------------------------------------------------------------------------
# Masked views
# ---------------------------------------------------------------------------


def proxy_view(pool: Any, proxy: Any) -> dict[str, Any]:
    """Return the redacted view of a proxy (never the password)."""
    if pool is not None:
        to_view = getattr(pool, "to_view", None)
        if callable(to_view):
            try:
                view = invoke_callable(to_view, proxy)
            except Exception:  # noqa: BLE001 - fall back to the local view below
                view = None
            if isinstance(view, dict):
                view.pop("password", None)
                return jsonable(view)
    scheme = getattr(proxy, "scheme", "")
    scheme = getattr(scheme, "value", scheme)
    username = getattr(proxy, "username", None)
    safe_url = getattr(proxy, "safe_url", None)
    if not isinstance(safe_url, str) or not safe_url:
        host = str(getattr(proxy, "host", ""))
        port = getattr(proxy, "port", "")
        userinfo = f"{username}:***@" if username else ""
        safe_url = f"{scheme}://{userinfo}{host}:{port}"
    health = getattr(proxy, "health", None)
    view = {
        "id": str(getattr(proxy, "id", "")),
        "name": str(getattr(proxy, "name", "") or ""),
        "scheme": str(scheme),
        "host": str(getattr(proxy, "host", "")),
        "port": int(getattr(proxy, "port", 0) or 0),
        "username": username,
        "has_credentials": bool(getattr(proxy, "has_credentials", False)),
        "url": safe_url,
        "tags": list(getattr(proxy, "tags", []) or []),
        "enabled": bool(getattr(proxy, "enabled", True)),
        "health": jsonable(health),
        "created_at": jsonable(getattr(proxy, "created_at", None)),
    }
    return view


def _proxy_rows(views: list[dict[str, Any]]) -> list[tuple[str, str, str, str, str, str, str]]:
    rows = []
    for view in views:
        raw_health = view.get("health")
        health: dict[str, Any] = raw_health if isinstance(raw_health, dict) else {}
        rows.append(
            (
                str(view.get("id", "")),
                str(view.get("name", "")),
                str(view.get("url", "")),
                "yes" if view.get("has_credentials") else "no",
                str(health.get("status", "") or ""),
                str(health.get("latency_ms", "") or ""),
                "yes" if view.get("enabled", True) else "no",
            )
        )
    return rows


def _render_views(out: Any, views: list[dict[str, Any]], *, title: str) -> None:
    def render() -> None:
        out.table(
            ("id", "name", "url (masked)", "credentials", "health", "latency ms", "enabled"),
            _proxy_rows(views),
            title=title,
            status_columns=(4,),
        )
        out.info(f"{len(views)} proxy(ies)")

    out.emit(views, render)


def _test_result_payload(result: Any) -> dict[str, Any]:
    return jsonable(result)


def _render_test_results(out: Any, results: list[Any], *, title: str) -> None:
    payload = [_test_result_payload(result) for result in results]

    def render() -> None:
        rows = []
        for result in results:
            ok = bool(getattr(result, "ok", False))
            rows.append(
                (
                    str(getattr(result, "proxy_id", "")),
                    "ok" if ok else "failed",
                    str(getattr(result, "latency_ms", "") or ""),
                    str(getattr(result, "egress_ip", "") or ""),
                    str(getattr(result, "error", "") or ""),
                )
            )
        out.table(
            ("proxy", "status", "latency ms", "egress ip", "error"),
            rows,
            title=title,
            status_columns=(1,),
        )
        failed = sum(1 for result in results if not bool(getattr(result, "ok", False)))
        if failed:
            out.warning(f"{failed}/{len(results)} proxy check(s) failed")

    out.emit(payload, render)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def proxies_list(
    ctx: typer.Context,
    include_disabled: bool = typer.Option(False, "--all", help="Include disabled proxies."),
    json_output: bool = json_flag(),
) -> None:
    """List proxies with masked credentials."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    pool = service(ctx, "proxies")
    proxies = list(invoke_callable(pool.list_proxies, include_disabled=include_disabled) or [])
    _render_views(out, [proxy_view(pool, proxy) for proxy in proxies], title="proxies")


def proxies_add(
    ctx: typer.Context,
    url: str = typer.Argument(
        ..., help="Proxy URL: http://user:pass@host:port or socks5://user:pass@host:port."
    ),
    name: str = typer.Option(None, "--name", help="Display name (defaults to the masked URL)."),
    tag: list[str] = typer.Option(None, "--tag", help="Tag (repeatable)."),
    json_output: bool = json_flag(),
) -> None:
    """Add one authenticated proxy (the password goes straight into the vault)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    pool = service(ctx, "proxies")
    created = invoke_callable(pool.add, url=url, name=name, tags=[str(item) for item in (tag or [])] or None)
    view = proxy_view(pool, created)

    def render() -> None:
        out.success(f"proxy added: {view['id']}")
        out.kv(
            (
                ("name", view["name"]),
                ("url", view["url"]),
                ("credentials", "yes (stored in the encrypted vault)" if view["has_credentials"] else "none"),
            ),
            title="proxy",
        )

    out.emit(view, render)


def proxies_import(
    ctx: typer.Context,
    file: Path = typer.Argument(..., help="Text file with one proxy URL per line ('-' reads stdin)."),
    source: str = typer.Option("import", "--source", help="Source label stored with the proxies."),
    json_output: bool = json_flag(),
) -> None:
    """Import proxy URLs, one per line (duplicates are skipped)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    pool = service(ctx, "proxies")
    if str(file) == "-":
        text = sys.stdin.read()
    else:
        try:
            text = Path(file).read_text(encoding="utf-8")
        except OSError as exc:
            raise CliUsageError(f"could not read {file}: {exc}") from exc
    result = invoke_callable(pool.import_text, text, source=source)
    added = list(getattr(result, "added", []) or [])
    views = [proxy_view(pool, proxy) for proxy in added]
    payload: dict[str, Any] = {
        "added": views,
        "skipped": int(getattr(result, "skipped", 0) or 0),
        "errors": [str(error) for error in (getattr(result, "errors", []) or [])],
    }

    def render() -> None:
        _render_views(out, views, title="imported proxies")
        if payload["skipped"]:
            out.info(f"skipped {payload['skipped']} duplicate(s)")
        for error in payload["errors"]:
            out.warning(str(error))

    out.emit(payload, render)


def proxies_check(
    ctx: typer.Context,
    proxy_id: str = typer.Argument(..., help="Proxy id."),
    timeout_s: float = typer.Option(None, "--timeout", min=0.1, help="Health-check timeout in seconds."),
    json_output: bool = json_flag(),
) -> None:
    """Health-check one proxy from the host."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    pool = service(ctx, "proxies")
    result = invoke_callable(pool.check, proxy_id, timeout_s=timeout_s)
    _render_test_results(out, [result], title="proxy check")
    if not bool(getattr(result, "ok", False)):
        raise typer.Exit(1)


def proxies_check_all(
    ctx: typer.Context,
    json_output: bool = json_flag(),
) -> None:
    """Health-check every proxy from the host."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    pool = service(ctx, "proxies")
    results = list(invoke_callable(pool.check_all) or [])
    _render_test_results(out, results, title="proxy checks")
    if any(not bool(getattr(result, "ok", False)) for result in results):
        raise typer.Exit(1)


def proxies_delete(
    ctx: typer.Context,
    proxy_id: str = typer.Argument(..., help="Proxy id to delete."),
    json_output: bool = json_flag(),
) -> None:
    """Delete a proxy and its vaulted secret."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    pool = service(ctx, "proxies")
    invoke_callable(pool.delete, proxy_id)
    payload = {"deleted": proxy_id, "ok": True}
    out.emit(payload, lambda: out.success(f"proxy {proxy_id!r} deleted"))


def proxies_verify(
    ctx: typer.Context,
    proxy_id: str = typer.Argument(..., help="Proxy id."),
    guest: str = typer.Option(None, "--guest", "-g", help="Verify egress from inside this instance."),
    token_url: str = typer.Option(None, "--token-url", help="URL whose body must contain the instance token."),
    timeout_s: float = typer.Option(None, "--timeout", min=0.1, help="Verification timeout in seconds."),
    json_output: bool = json_flag(),
) -> None:
    """Verify a proxy from the host, or from inside a guest with --guest."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    pool = service(ctx, "proxies")
    proxy = invoke_callable(pool.get, proxy_id)
    verifier = optional_service(ctx, "verifier")
    if guest:
        if verifier is not None and callable(getattr(verifier, "verify_guest", None)):
            result = invoke_callable(
                verifier.verify_guest,
                guest,
                proxy,
                token_url=token_url,
                timeout_s=timeout_s,
            )
        elif callable(getattr(pool, "verify_guest", None)):
            result = invoke_callable(
                pool.verify_guest,
                guest,
                proxy,
                token_url=token_url,
                timeout_s=timeout_s,
            )
        else:
            raise CliServiceError(
                "guest egress verification is not available in this build",
                service="verifier",
                hint="enable the proxy verifier or verify from the host instead",
            )
    else:
        if verifier is not None and callable(getattr(verifier, "verify_proxy", None)):
            result = invoke_callable(verifier.verify_proxy, proxy, timeout_s=timeout_s)
        else:
            result = invoke_callable(pool.check, proxy_id, timeout_s=timeout_s)
    _render_test_results(out, [result], title=f"proxy verification ({'guest' if guest else 'host'})")
    if not bool(getattr(result, "ok", False)):
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------


def proxies_assign(
    ctx: typer.Context,
    avd_name: str = typer.Argument(..., help="Instance name."),
    proxy_id: str = typer.Argument(..., help="Proxy id to assign."),
    json_output: bool = json_flag(),
) -> None:
    """Assign exactly one proxy to an instance."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    assignments = service(ctx, "assignments")
    assignment = invoke_callable(assignments.assign, avd_name, proxy_id)
    payload = jsonable(assignment)

    def render() -> None:
        out.success(f"{avd_name} -> {proxy_id}")
        out.kv(
            (
                ("instance", getattr(assignment, "avd_name", avd_name)),
                ("proxy", getattr(assignment, "proxy_id", proxy_id)),
            ),
            title="assignment",
        )

    out.emit(payload, render)


def proxies_unassign(
    ctx: typer.Context,
    avd_name: str = typer.Argument(..., help="Instance name."),
    json_output: bool = json_flag(),
) -> None:
    """Remove an instance's proxy assignment."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    assignments = service(ctx, "assignments")
    invoke_callable(assignments.unassign, avd_name)
    payload = {"instance": avd_name, "assigned": False, "ok": True}
    out.emit(payload, lambda: out.success(f"proxy unassigned from {avd_name}"))


def _instance_names(context: Any) -> list[str]:
    avds = optional_service(context, "avds")
    if avds is None:
        return []
    lister = getattr(avds, "list_instances", None)
    if not callable(lister):
        return []
    return [str(getattr(info, "name", "")) for info in invoke_callable(lister) or [] if getattr(info, "name", "")]


def proxies_auto_assign(
    ctx: typer.Context,
    instances: list[str] = typer.Option(None, "--instances", help="Instance name (repeatable/comma separated)."),
    strategy: str = typer.Option("round_robin", "--strategy", help="round_robin, random or healthiest."),
    json_output: bool = json_flag(),
) -> None:
    """Auto-assign proxies to instances (default: every deployed instance)."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    assignments = service(ctx, "assignments")
    names: list[str] = []
    for item in instances or []:
        names.extend(part.strip() for part in str(item).split(",") if part.strip())
    if not names:
        names = _instance_names(context)
    if not names:
        raise CliUsageError(
            "no instances to assign",
            hint="deploy an instance first or pass --instances NAME",
        )
    created = list(invoke_callable(assignments.auto_assign, names, strategy=strategy) or [])
    payload = jsonable(created)

    def render() -> None:
        rows = [
            (
                str(getattr(assignment, "avd_name", "")),
                str(getattr(assignment, "proxy_id", "")),
            )
            for assignment in created
        ]
        out.table(("instance", "proxy"), rows, title=f"auto-assigned ({strategy})")
        out.info(f"{len(created)} assignment(s)")

    out.emit(payload, render)


def proxies_bridges(
    ctx: typer.Context,
    json_output: bool = json_flag(),
) -> None:
    """List active local proxy bridges."""
    context = get_context(ctx)
    out = context.output(json_output=json_output)
    bridge = service(ctx, "bridge")
    active = list(invoke_callable(bridge.list_active) or [])
    payload = jsonable(active)

    def render() -> None:
        rows = [
            (
                str(getattr(item, "instance", "")),
                f"{getattr(item, 'listen_host', '')}:{getattr(item, 'listen_port', '')}",
                str(getattr(item, "mode", "")),
                "yes" if getattr(item, "active", True) else "no",
            )
            for item in active
        ]
        out.table(("instance", "listen", "mode", "active"), rows, title="proxy bridges")
        out.info(f"{len(active)} bridge(s)")

    out.emit(payload, render)


def register(app: typer.Typer) -> None:
    """Register the proxies command group."""
    proxies_app.command("list")(command_errors("proxies list")(proxies_list))
    proxies_app.command("add")(command_errors("proxies add")(proxies_add))
    proxies_app.command("import")(command_errors("proxies import")(proxies_import))
    proxies_app.command("check")(command_errors("proxies check")(proxies_check))
    proxies_app.command("check-all")(command_errors("proxies check-all")(proxies_check_all))
    proxies_app.command("delete")(command_errors("proxies delete")(proxies_delete))
    proxies_app.command("verify")(command_errors("proxies verify")(proxies_verify))
    proxies_app.command("assign")(command_errors("proxies assign")(proxies_assign))
    proxies_app.command("unassign")(command_errors("proxies unassign")(proxies_unassign))
    proxies_app.command("auto-assign")(command_errors("proxies auto-assign")(proxies_auto_assign))
    proxies_app.command("bridges")(command_errors("proxies bridges")(proxies_bridges))
    app.add_typer(proxies_app, name="proxies")


__all__ = ["proxy_view", "proxies_app", "register"]
