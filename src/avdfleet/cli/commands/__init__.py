"""Registration of every avdfleet command group.

Each module exposes register(app: typer.Typer) -> None and owns its own
sub-apps; this module is the single place where the command tree is assembled so
avdfleet --help lists every capability.
"""

from __future__ import annotations

import typer


def register_commands(app: typer.Typer) -> None:
    """Attach every command group to *app* (order defines help ordering)."""
    from . import (
        appium,
        avd,
        bundle,
        doctor,
        fleet,
        gui,
        images,
        install,
        instances,
        instrumentation,
        proxies,
        screen,
        scripts,
        selftest,
        serve,
    )

    install.register(app)
    doctor.register(app)
    serve.register(app)
    gui.register(app)
    images.register(app)
    avd.register(app)
    instances.register(app)
    proxies.register(app)
    scripts.register(app)
    appium.register(app)
    screen.register(app)
    instrumentation.register(app)
    fleet.register(app)
    bundle.register(app)
    selftest.register(app)


__all__ = ["register_commands"]
