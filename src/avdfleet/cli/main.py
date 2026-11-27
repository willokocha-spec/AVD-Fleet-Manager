"""The avdfleet Typer application and entry point.

Global options
--------------
--config PATH
    Explicit config.toml (otherwise AVDFLEET_CONFIG_FILE, then
    ./avdfleet.toml, then the per-user config directory).
--json
    Machine-readable output.  Every command also accepts --json after the
    command name (avdfleet doctor --json).
--verbose / --quiet
    Diagnostics level; --quiet suppresses informational lines (results and
    errors are still printed).
--version
    Print avdfleet <version> and exit 0.

The CliContext is lazy: configuration, paths and the service container are
only created when a command needs them, so help/version and argument validation
never touch the SDK or the database.

Exit codes are documented in :mod:`avdfleet.cli.errors` (0 ok, 1 generic,
2 usage, 3 environment/blocked, 4 not found, 5 conflict, 6 reboot, 3010 reboot
required during install).
"""

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import typer

from avdfleet.core.config import APP_VERSION
from avdfleet.core.errors import AvdFleetError
from avdfleet.core.logging import redact_text

from .context import CliContext, configure_stdio, get_context
from .errors import EXIT_ERROR, EXIT_OK, build_error_payload

_HELP = """\
Install, isolate and automate fleets of Android Virtual Devices.

Every capability is available from the CLI and from the REST API served by
'avdfleet serve'. 'avdfleet gui' starts that daemon when needed and opens the
web GUI; 'avdfleet apply -f fleet.yaml' reconciles a declarative fleet (the
same as 'avdfleet fleet apply', with 'avdfleet plan -f fleet.yaml' for the
read-only diff). Use '--json' for machine-readable output; proxy passwords and
API tokens are never printed. Run 'avdfleet <command> --help' for details."""


def _version_callback(ctx: typer.Context, value: bool) -> None:
    if not value:
        return
    typer.echo(f"avdfleet {APP_VERSION}")
    raise typer.Exit(EXIT_OK)


def _configure_logging(context: CliContext) -> None:
    """Apply --verbose/--quiet to the root logger (redaction stays on)."""
    try:
        from avdfleet.core.logging import setup_logging  # noqa: PLC0415 - optional at help time
    except Exception:  # pragma: no cover - logging is part of core, always present
        return
    level = "DEBUG" if context.verbose else "ERROR"
    try:
        setup_logging(level=level, console=True, force=True)
    except Exception:  # pragma: no cover - logging setup must never break the CLI
        pass


def build_app() -> typer.Typer:
    """Build the Typer application (command tree + global options)."""
    app = typer.Typer(
        name="avdfleet",
        help=_HELP,
        no_args_is_help=False,
        add_completion=False,
        rich_markup_mode=None,
    )

    @app.callback(invoke_without_command=True)
    def _root(
        ctx: typer.Context,
        config: Path = typer.Option(
            None,
            "--config",
            help="Path to config.toml (default: AVDFLEET_CONFIG_FILE, ./avdfleet.toml, user config dir).",
        ),
        json_output: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
        verbose: bool = typer.Option(False, "--verbose", help="Verbose diagnostics (secrets are still redacted)."),
        quiet: bool = typer.Option(False, "--quiet", help="Only errors and essential result output."),
        version: bool = typer.Option(
            False,
            "--version",
            is_eager=True,
            callback=_version_callback,
            help="Show the version and exit.",
        ),
    ) -> None:
        cli_context = get_context(ctx)
        cli_context.configure(
            config_path=config,
            json_output=json_output,
            verbose=verbose,
            quiet=quiet,
        )
        ctx.obj = cli_context
        ctx.call_on_close(cli_context.close)
        if cli_context.verbose or cli_context.quiet:
            _configure_logging(cli_context)
        if ctx.invoked_subcommand is None:
            typer.echo(ctx.get_help())

    from .commands import register_commands  # noqa: PLC0415 - after the callback exists

    register_commands(app)
    return app


#: Module-level application object (also used by tests via CliRunner).
app = build_app()


def _report_top_level(error: BaseException, *, args: Sequence[str]) -> int:
    """Last-resort redacted reporting for errors raised outside a command."""
    payload = build_error_payload(error, command=" ".join(args[:1]) or None)
    if "--json" in args:
        import json  # noqa: PLC0415

        typer.echo(json.dumps(payload, indent=2, default=str))
    else:
        message = redact_text(str(getattr(error, "message", "") or error)) or type(error).__name__
        typer.echo(f"error: {message}", err=True)
        hint = getattr(error, "hint", None)
        if hint:
            typer.echo(f"hint: {redact_text(str(hint))}", err=True)
    return int(payload.get("exit_code", EXIT_ERROR))


def main(argv: Sequence[str] | None = None, *, container: Any | None = None) -> int:
    """Run the CLI and return the process exit code (never raises for user errors).

    container optionally injects a service container for tests/embedding;
    the equivalent environment route is AVDFLEET_CONTAINER=module:factory.
    """
    configure_stdio()
    args = list(sys.argv[1:] if argv is None else argv)
    command = typer.main.get_command(app)
    context: CliContext | None = CliContext(container=container) if container is not None else None
    try:
        result = command.main(
            args=args,
            prog_name="avdfleet",
            windows_expand_args=False,
            obj=context,
        )
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return EXIT_OK
        if isinstance(code, int):
            return code
        typer.echo(str(code), err=True)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - interactive interrupt
        typer.echo("interrupted", err=True)
        return 130
    except AvdFleetError as exc:
        return _report_top_level(exc, args=args)
    except Exception as exc:  # noqa: BLE001 - the CLI never leaks tracebacks
        if (os.environ.get("AVDFLEET_CLI_DEBUG") or "") == "1":  # pragma: no cover
            raise
        return _report_top_level(exc, args=args)
    finally:
        if context is not None:
            context.close()
    return result if isinstance(result, int) else EXIT_OK


__all__ = ["app", "build_app", "main"]


if __name__ == "__main__":  # pragma: no cover - module execution convenience
    raise SystemExit(main())
