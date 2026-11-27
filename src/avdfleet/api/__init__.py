"""FastAPI daemon for AVD Fleet: REST + WebSocket API, auth and static UI hosting."""

from __future__ import annotations

from .app import create_app
from .auth import TokenStore
from .container import ContainerAdapter, ServiceUnavailable, UnsupportedOperation, create_adapter

__all__ = [
    "ContainerAdapter",
    "ServiceUnavailable",
    "TokenStore",
    "UnsupportedOperation",
    "create_adapter",
    "create_app",
]
