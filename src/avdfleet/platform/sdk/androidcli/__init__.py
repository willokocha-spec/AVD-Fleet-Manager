"""New Android CLI toolchain backend.

Layout::

    client.py   -- ``android`` command wrapper, JSON-first with table fallbacks
    parsing.py  -- JSON/table output adapters
    backend.py  -- :class:`AndroidCliBackend` implementing ``SdkBackend``
"""

from __future__ import annotations

from .backend import SUPPORTED_FEATURES, AndroidCliBackend
from .client import AndroidCliClient
from .parsing import parse_android_version, parse_sdk_list_output

__all__ = [
    "SUPPORTED_FEATURES",
    "AndroidCliBackend",
    "AndroidCliClient",
    "parse_android_version",
    "parse_sdk_list_output",
]
