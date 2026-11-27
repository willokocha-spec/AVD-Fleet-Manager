"""Executable fakes and pytest fixtures for AVD Fleet tests.

The implementation follows ``contracts/fakes-spec.md``.  Importing this package
is cheap (standard library only); the pytest fixtures live in
``tests.fakes.fixtures`` and are registered by ``tests/conftest.py``.
"""

from __future__ import annotations

from .runtime import TOOLS, FakeLoopbackListener, FakeState, FakeTools, FakeToolsError

__all__ = [
    "FakeLoopbackListener",
    "FakeState",
    "FakeTools",
    "FakeToolsError",
    "TOOLS",
]
