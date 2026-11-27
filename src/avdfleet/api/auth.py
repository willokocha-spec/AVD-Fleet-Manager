"""Bearer-token authentication for the HTTP and WebSocket API.

Design
------
* One opaque token per daemon installation, 32 random bytes rendered with
  :func:`secrets.token_urlsafe` (43 URL-safe characters).
* ``AVDFLEET_TOKEN`` (environment) overrides the token file.  This is the only
  way to run the daemon without writing a secret to disk; the environment token
  is never persisted.
* The token file lives at ``<data_dir>/token`` (see :class:`avdfleet.core.paths.Paths`).
  It is created atomically with owner-only permissions:

  - POSIX: ``os.open(..., 0o600)`` plus a defensive ``chmod``.
  - Windows: NTFS has no POSIX mode bits, so ``os.chmod`` is best-effort only.
    We additionally run ``icacls <path> /inheritance:r /grant:r <user>:F`` to
    remove inherited ACEs and grant only the current user.  This is best-effort
    (it can be blocked by policy, and local Administrators/SYSTEM retain the
    ability to read the file); the token is still never logged or returned.
    ``AVDFLEET_TOKEN`` is the strongest option on shared Windows hosts.

The token is never logged, never returned by an endpoint and never written into
an error body.  Comparison is constant time: both sides are SHA-256 hashed and
compared with :func:`hmac.compare_digest` so the comparison cannot leak the
token length or prefix through timing.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import stat
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from avdfleet.core.errors import AvdFleetError
from avdfleet.core.paths import Paths

logger = logging.getLogger("avdfleet.api.auth")

ENV_TOKEN = "AVDFLEET_TOKEN"
TOKEN_BYTES = 32
WWW_AUTHENTICATE = 'Bearer realm="avdfleet"'


class TokenStoreError(AvdFleetError):
    """The token store could not be read or created."""

    code = "token_store_error"
    http_status = 500


class BootstrapCodeStore:
    """Short-lived, single-use codes for browser auto-authentication.

    ``avdfleet gui`` opens ``/auth/bootstrap#code=<code>``.  The page posts the
    code to the unauthenticated ``POST /api/v1/auth/bootstrap`` endpoint, which
    exchanges it for the daemon token and stores it in browser local storage.
    Codes are high-entropy, expire quickly, are consumed on first use and are
    kept hashed in memory, so a stale URL or a copied link cannot be replayed.
    """

    def __init__(self, *, ttl_s: float = 120.0, clock: Any = time.monotonic) -> None:
        self._ttl_s = max(5.0, float(ttl_s))
        self._clock = clock
        self._lock = threading.RLock()
        self._codes: dict[str, float] = {}

    def issue(self) -> str:
        """Create and return a new one-time bootstrap code."""
        raw = secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        now = float(self._clock())
        with self._lock:
            # Opportunistically prune expired entries; the dict is tiny.
            expired = [key for key, deadline in self._codes.items() if deadline <= now]
            for key in expired:
                self._codes.pop(key, None)
            self._codes[digest] = now + self._ttl_s
        return raw

    def redeem(self, code: str | None) -> bool:
        """Consume *code* and return True only for a fresh, valid code."""
        if not code or not isinstance(code, str):
            return False
        digest = hashlib.sha256(code.strip().encode("utf-8")).hexdigest()
        now = float(self._clock())
        with self._lock:
            deadline = self._codes.pop(digest, None)
        return deadline is not None and deadline > now

    @property
    def ttl_s(self) -> float:
        return self._ttl_s

    def clear(self) -> None:
        with self._lock:
            self._codes.clear()


def hash_token(token: str) -> bytes:
    """Return a fixed-length comparison value for *token*."""
    return hashlib.sha256(token.encode("utf-8")).digest()


def tokens_equal(candidate: str | None, expected: str | None) -> bool:
    """Constant-time token comparison (works for unequal lengths too)."""
    if not candidate or not expected:
        return False
    return hmac.compare_digest(hash_token(candidate), hash_token(expected))


class TokenStore:
    """Loads (or creates) the daemon bearer token.

    Parameters
    ----------
    paths:
        Resolved filesystem layout; the token file is ``paths.data_dir / "token"``.
    environ:
        Environment mapping used for the ``AVDFLEET_TOKEN`` override.  Defaults
        to :data:`os.environ`; tests pass an explicit mapping for isolation.
    token_path:
        Optional explicit path (tests only).
    """

    def __init__(
        self,
        paths: Paths,
        *,
        environ: Mapping[str, str] | None = None,
        token_path: Path | None = None,
    ) -> None:
        env = os.environ if environ is None else environ
        raw_env = env.get(ENV_TOKEN)
        self._env_token = raw_env.strip() if raw_env else None
        self._paths = paths
        self._path = Path(token_path) if token_path is not None else paths.token_path
        self._cached: str | None = None
        self._permissions_checked = False

    # -- introspection ---------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    @property
    def source(self) -> str:
        return "environment" if self._env_token else "file"

    @property
    def token_from_environment(self) -> bool:
        return self._env_token is not None

    # -- public API ------------------------------------------------------
    def ensure(self) -> str:
        """Return the active token, creating the token file if required."""
        if self._env_token:
            return self._env_token
        if self._cached:
            return self._cached
        token = self._read_file()
        if not token:
            token = self._create_file()
        self._verify_permissions()
        self._cached = token
        return token

    def matches(self, candidate: str | None) -> bool:
        """Return True when *candidate* is the active daemon token."""
        try:
            expected = self.ensure()
        except TokenStoreError:
            raise
        except OSError as exc:
            raise TokenStoreError(
                "the API token could not be read; check permissions on the data directory",
                hint="set AVDFLEET_TOKEN or fix permissions on <data_dir>/token",
            ) from exc
        return tokens_equal(candidate, expected)

    def reset_cache(self) -> None:
        """Drop the cached token (used by tests after rotating the file)."""
        self._cached = None

    # -- internals -------------------------------------------------------
    def _read_file(self) -> str:
        try:
            value = self._path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""
        except OSError as exc:
            raise TokenStoreError(
                "the API token file could not be read",
                hint="check permissions on the data directory",
            ) from exc
        return value

    def _create_file(self) -> str:
        token = secrets.token_urlsafe(TOKEN_BYTES)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TokenStoreError(
                "the data directory for the API token could not be created",
                hint="check AVDFLEET_DATA_DIR and filesystem permissions",
            ) from exc
        try:
            fd = os.open(str(self._path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # Another process (or a previous run) won the race: use its token.
            existing = self._read_file()
            if existing:
                return existing
            try:
                fd = os.open(str(self._path), os.O_WRONLY | os.O_TRUNC, 0o600)
            except OSError as exc:  # pragma: no cover - exotic filesystem failure
                raise TokenStoreError("the API token file could not be written") from exc
        except OSError as exc:
            raise TokenStoreError(
                "the API token file could not be created",
                hint="check permissions on the data directory",
            ) from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(token + "\n")
        except OSError as exc:  # pragma: no cover - exotic filesystem failure
            raise TokenStoreError("the API token file could not be written") from exc
        self._restrict_permissions()
        logger.info("generated a new API token at %s (value never logged)", self._path)
        return token

    def _verify_permissions(self) -> None:
        """Warn (and try to repair) a token file readable by other users.

        On Windows the POSIX mode bits are meaningless (``os.stat`` reports
        0o666 for an NTFS file); permissions are enforced best-effort with
        ``icacls`` at creation time instead, so the check is POSIX-only.
        """
        if self._env_token or os.name == "nt" or self._permissions_checked:
            return
        self._permissions_checked = True
        try:
            mode = stat.S_IMODE(os.stat(self._path).st_mode)
        except OSError:  # pragma: no cover - file vanished between calls
            return
        if mode & 0o077:
            logger.warning("API token file permissions are too open (%o); tightening", mode)
            self._restrict_permissions()

    def _restrict_permissions(self) -> None:
        """Best-effort owner-only permissions for the token file."""
        if os.name == "nt":
            self._restrict_windows()
            return
        try:
            os.chmod(self._path, 0o600)
        except OSError:  # pragma: no cover - filesystem may not support chmod
            logger.warning("could not chmod the API token file to 0600")

    def _restrict_windows(self) -> None:
        """Best-effort NTFS ACL lockdown; documented limitation, never fatal."""
        username = os.environ.get("USERNAME") or ""
        if username:
            try:
                subprocess.run(
                    [
                        "icacls",
                        str(self._path),
                        "/inheritance:r",
                        "/grant:r",
                        f"{username}:F",
                    ],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                logger.debug("icacls unavailable; relying on chmod for the token file", exc_info=True)
        try:
            # Windows only maps the read-only bit; this is a weak fallback.
            os.chmod(self._path, stat.S_IREAD | stat.S_IWRITE)
        except OSError:  # pragma: no cover - network filesystems
            pass


def extract_bearer(headers: Mapping[str, Any]) -> str | None:
    """Extract ``Authorization: Bearer <token>`` from a header mapping."""
    value = None
    try:
        value = headers.get("authorization")
    except AttributeError:  # pragma: no cover - non-mapping input
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    parts = value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def token_for_websocket(websocket: Any) -> str | None:
    """Return the token supplied as ``?token=`` or in the Authorization header."""
    try:
        query_token = websocket.query_params.get("token")
    except AttributeError:  # pragma: no cover - defensive
        query_token = None
    if isinstance(query_token, str) and query_token.strip():
        return query_token.strip()
    headers = getattr(websocket, "headers", None)
    if headers is None:  # pragma: no cover - defensive
        return None
    return extract_bearer(headers)


def origin_allowed(origin: str | None, allow_origins: Sequence[str] | None) -> bool:
    """Validate a browser ``Origin`` header against the configured allowlist.

    Requests without an ``Origin`` header are accepted: those are non-browser
    clients (CLI, tests, native apps) which are not subject to cross-site
    WebSocket hijacking.  Browsers always send ``Origin``, so a malicious page
    is always checked.
    """
    if not origin:
        return True
    allowed = [candidate.rstrip("/") for candidate in (allow_origins or []) if candidate]
    if not allowed:
        return False
    normalized = origin.rstrip("/")
    return normalized in allowed


__all__ = [
    "BootstrapCodeStore",
    "ENV_TOKEN",
    "TOKEN_BYTES",
    "WWW_AUTHENTICATE",
    "TokenStore",
    "TokenStoreError",
    "extract_bearer",
    "hash_token",
    "origin_allowed",
    "token_for_websocket",
    "tokens_equal",
]
