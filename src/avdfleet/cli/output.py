"""Output layer for the avdfleet CLI.

Three rendering modes share one API:

* **rich tables/status** when stdout is an interactive terminal,
* **plain aligned text** for pipes, files and non-TTY terminals (so
  avdfleet images list | grep works),
* **JSON** when --json is passed (global or per command) - a single machine
  readable document on stdout, nothing else.

Everything that leaves this module is redacted with
:func:`avdfleet.core.logging.redact` (the same choke point as the API and the
logs), so proxy passwords and tokens can never be printed by accident.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

from pydantic import BaseModel
from rich import box
from rich.console import Console
from rich.table import Table

from avdfleet.core.logging import redact

_STATUS_STYLES = {
    "ok": "green",
    "pass": "green",
    "passed": "green",
    "succeeded": "green",
    "verified": "green",
    "warn": "yellow",
    "warning": "yellow",
    "skip": "cyan",
    "skipped": "cyan",
    "unverified": "yellow",
    "fail": "red",
    "failed": "red",
    "error": "red",
    "running": "green",
    "booting": "yellow",
    "starting": "yellow",
    "queued": "cyan",
    "cancelled": "yellow",
    "timed_out": "red",
    "create": "green",
    "update": "yellow",
    "delete": "red",
    "noop": "dim",
}


def jsonable(value: Any) -> Any:
    """Convert models/dataclasses/enums/paths/datetimes into JSON-safe values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, BaseModel):
        return jsonable(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return str(value)


def is_tty(stream: TextIO | None = None) -> bool:
    """Return True when *stream* is an interactive terminal."""
    target = stream if stream is not None else sys.stdout
    try:
        return bool(target.isatty())
    except Exception:  # pragma: no cover - exotic stream
        return False


def strips_ansi(text: str) -> str:
    """Remove ANSI colour codes (used by tests and for piped JSON safety)."""
    import re

    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def json_flag() -> Any:
    """Return a fresh --json Typer option (per-command machine output)."""
    import typer

    return typer.Option(False, "--json", help="Machine-readable JSON output (no tables/colours).")


class Output:
    """Console abstraction with rich/plain/JSON modes and redaction."""

    def __init__(
        self,
        *,
        json_output: bool = False,
        quiet: bool = False,
        verbose: bool = False,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self.json_output = bool(json_output)
        self.quiet = bool(quiet)
        self.verbose = bool(verbose)
        self._stdout: TextIO = stdout if stdout is not None else sys.stdout
        self._stderr: TextIO = stderr if stderr is not None else sys.stderr
        self._tty = is_tty(self._stdout) and not os.environ.get("NO_COLOR")
        self._console = Console(
            file=self._stdout,
            highlight=False,
            markup=False,
            no_color=not self._tty,
            soft_wrap=True,
        )
        self._err_console = Console(
            file=self._stderr,
            highlight=False,
            markup=False,
            no_color=not is_tty(self._stderr) or not self._tty,
            soft_wrap=True,
        )

    # -- mode ------------------------------------------------------------
    @property
    def is_tty(self) -> bool:
        return self._tty

    def set_json(self, enabled: bool) -> None:
        self.json_output = bool(enabled) or self.json_output

    # -- primitives ------------------------------------------------------
    def write(self, text: str = "", *, stream: TextIO | None = None) -> None:
        """Write plain text (never wraps or interprets markup).

        Legacy Windows consoles decode with the ANSI code page; a non-ASCII
        character in user data (device name, error text) must degrade to a
        replacement character instead of raising UnicodeEncodeError.
        """
        target = self._stdout if stream is None else stream
        payload = text + "\n"
        try:
            target.write(payload)
        except UnicodeEncodeError:  # pragma: no cover - only on legacy code pages
            encoding = getattr(target, "encoding", None) or "ascii"
            safe = payload.encode(encoding, errors="replace").decode(encoding, errors="replace")
            target.write(safe)
        try:
            target.flush()
        except Exception:  # pragma: no cover - some streams cannot flush
            pass

    def write_error(self, text: str = "") -> None:
        self.write(text, stream=self._stderr)

    def emit_json(self, payload: Any) -> None:
        """Emit one redacted JSON document on stdout."""
        document = json.dumps(jsonable(redact(payload)), indent=2, sort_keys=False, ensure_ascii=False)
        self.write(document)

    def emit(self, payload: Any, render: Any = None) -> None:
        """Emit *payload* as JSON, or call render() for human output."""
        if self.json_output:
            self.emit_json(payload)
            return
        if render is not None:
            render()

    # -- messages --------------------------------------------------------
    def info(self, text: str) -> None:
        if self.quiet:
            return
        self.write(text)

    def success(self, text: str) -> None:
        if self._tty:
            self._console.print(f"[green]{text}[/green]", markup=True)
        else:
            self.write(text)

    def warning(self, text: str, *, prefix: bool = True) -> None:
        message = f"warning: {text}" if prefix else text
        if self._tty:
            self._err_console.print(f"[yellow]{message}[/yellow]", markup=True)
        else:
            self.write_error(message)

    def error(self, text: str, *, prefix: bool = True) -> None:
        message = f"error: {text}" if prefix else text
        if self._tty:
            self._err_console.print(f"[red]{message}[/red]", markup=True)
        else:
            self.write_error(message)

    def detail(self, text: str) -> None:
        if self.quiet:
            return
        self.write(text)

    # -- tables ----------------------------------------------------------
    def table(
        self,
        columns: Sequence[str],
        rows: Iterable[Sequence[Any]],
        *,
        title: str | None = None,
        caption: str | None = None,
        status_columns: Sequence[int] = (),
    ) -> None:
        """Render a table: rich when interactive, aligned plain text otherwise."""
        materialized = [["" if cell is None else str(cell) for cell in row] for row in rows]
        if not materialized:
            self.info("(no rows)")
            if caption:
                self.info(caption)
            return
        if self._tty:
            self._rich_table(columns, materialized, title=title, caption=caption, status_columns=status_columns)
            return
        self._plain_table(columns, materialized, title=title, caption=caption)

    def _rich_table(
        self,
        columns: Sequence[str],
        rows: list[list[str]],
        *,
        title: str | None,
        caption: str | None,
        status_columns: Sequence[int],
    ) -> None:
        table = Table(
            title=title,
            caption=caption,
            box=box.SIMPLE_HEAD,
            header_style="bold",
            show_lines=False,
            pad_edge=False,
        )
        for label in columns:
            table.add_column(label, overflow="fold")
        status_set = set(status_columns)
        for row in rows:
            table.add_row(*[f"[{_STATUS_STYLES.get(cell.lower(), '')}]{cell}[/]" if index in status_set else cell for index, cell in enumerate(row)])
        self._console.print(table)

    def _plain_table(
        self,
        columns: Sequence[str],
        rows: list[list[str]],
        *,
        title: str | None,
        caption: str | None,
    ) -> None:
        widths = [len(label) for label in columns]
        for row in rows:
            for index, cell in enumerate(row):
                if index < len(widths):
                    widths[index] = max(widths[index], len(cell))
        if title:
            self.write(title)
        header = "  ".join(label.ljust(widths[index]) for index, label in enumerate(columns))
        self.write(header)
        self.write("  ".join("-" * width for width in widths))
        for row in rows:
            self.write("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))
        if caption:
            self.write(caption)

    # -- convenience -----------------------------------------------------
    def kv(self, pairs: Iterable[tuple[str, Any]], *, title: str | None = None) -> None:
        """Render key/value details as a two-column table."""
        rows = [(key, "" if value is None else str(value)) for key, value in pairs]
        self.table(("field", "value"), rows, title=title)

    def operation(self, result: Any, *, title: str | None = None) -> None:
        """Render an OperationResult (or duck-typed equivalent)."""
        payload = jsonable(result)
        if self.json_output:
            self.emit_json(payload)
            return
        ok = bool(getattr(result, "ok", True))
        message = str(getattr(result, "message", "") or "")
        if message:
            if ok:
                self.success(message)
            else:
                self.warning(message)
        data = getattr(result, "data", None)
        if isinstance(data, Mapping) and data:
            self.kv([(str(key), jsonable(value)) for key, value in data.items()], title=title)
        if not message and not (isinstance(data, Mapping) and data):
            self.info("done" if ok else "failed")

    def plan(self, plan: Any, *, title: str = "fleet plan") -> None:
        """Render a FleetPlan-like object as a change table + summary."""
        payload = jsonable(plan)
        if self.json_output:
            self.emit_json(payload)
            return
        changes = list(getattr(plan, "changes", []) or [])
        rows = []
        for change in changes:
            action = getattr(change, "action", "")
            action = getattr(action, "value", action)
            detail = getattr(change, "detail", "") or ""
            rows.append(
                (
                    str(action),
                    str(getattr(change, "kind", "")),
                    str(getattr(change, "name", "")),
                    detail,
                )
            )
        self.table(("action", "kind", "name", "detail"), rows, title=title, status_columns=(0,))
        summary = getattr(plan, "summary", None)
        if isinstance(summary, Mapping) and summary:
            self.info("summary: " + ", ".join(f"{key}={value}" for key, value in summary.items()))
        errors = list(getattr(plan, "errors", []) or [])
        for error in errors:
            self.error(str(error), prefix=False)
        applyable = getattr(plan, "applyable", True)
        if not applyable:
            self.warning("plan is not applyable")


def get_output(ctx: Any, *, json_output: bool = False) -> Output:
    """Return the :class:`Output` bound to a Typer context (lazily created)."""
    from .context import get_context

    return get_context(ctx).output(json_output=json_output)


__all__ = [
    "Output",
    "get_output",
    "is_tty",
    "json_flag",
    "jsonable",
    "strips_ansi",
]
