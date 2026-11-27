"""Typer command-line interface for AVD Fleet.

Public entry point: :func:`avdfleet.cli.main.main` (avdfleet console script)
and python -m avdfleet.  The command tree mirrors the REST API capability by
capability; see :mod:`avdfleet.cli.main` for the global options and exit codes.
"""

from __future__ import annotations

__all__ = ["main"]


def __getattr__(name: str):  # pragma: no cover - lazy re-export
    if name == "main":
        from .main import main

        return main
    raise AttributeError(name)
