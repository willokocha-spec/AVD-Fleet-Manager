"""Proxy pool service: persistence, encrypted credentials and health checks.

The service is the single owner of proxy metadata and of the mapping between a
proxy record and its vaulted password:

* the database row (:class:`ProxyRecord`) stores only ``secret_ref``; the
  password itself is written **only** to the vault under ``proxy/<id>``;
* API-facing :class:`~avdfleet.core.models.ProxyConfig` instances never carry a
  password -- :meth:`ProxyPoolService.to_view` renders the contract ``ProxyView``
  shape including a truthful ``has_credentials`` flag;
* health checks use ``httpx`` through the configured proxy (with proxy auth) and
  persist the outcome via ``store.update_health``.

No store package is imported at module import time: any object exposing a
``.proxies`` repository with ``list/get/get_by_name/upsert/update_health/delete``
is accepted (see :class:`ProxyStore` / :class:`ProxyRepository`).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import SecretStr
from pydantic import ValidationError as PydanticValidationError

from ...core.config import Config
from ...core.errors import ConflictError, NotFoundError, ProxyError, ValidationError, VaultError
from ...core.logging import get_logger, redact_text
from ...core.models import (
    ProxyConfig,
    ProxyHealth,
    ProxyHealthStatus,
    ProxyImportResult,
    ProxyScheme,
    ProxyTestResult,
    utcnow,
)
from .parser import (
    ParsedProxy,
    make_proxy_id,
    normalise_host,
    parse_proxy_text,
    parse_proxy_url,
    proxy_fingerprint,
    proxy_url,
    validate_port,
)

SECRET_KEY_PREFIX = "proxy/"
DEFAULT_SOURCE = "manual"
DEFAULT_PORTS: dict[str, int] = {"http": 8080, "https": 8080, "socks5": 1080}
_IPV4_RE = re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3})\b")
_MAX_ERROR_LENGTH = 500


# ---------------------------------------------------------------------------
# Store interface expected from the D1 store package
# ---------------------------------------------------------------------------
@dataclass
class ProxyRecord:
    """Local mirror of the persisted proxy row (D1 store owns the schema).

    ``secret_ref`` points at the vault entry; ``password`` is never stored here.
    """

    id: str
    name: str = ""
    scheme: str = ProxyScheme.HTTP.value
    host: str = ""
    port: int = 0
    username: str | None = None
    secret_ref: str | None = None
    tags: list[str] = field(default_factory=list)
    enabled: bool = True
    source: str = DEFAULT_SOURCE
    health: ProxyHealth = field(default_factory=ProxyHealth)
    created_at: datetime | None = None


@runtime_checkable
class ProxyRepository(Protocol):
    """Repository contract used by :class:`ProxyPoolService`."""

    def list(self, include_disabled: bool = False) -> list[ProxyRecord]: ...

    def get(self, proxy_id: str) -> ProxyRecord | None: ...

    def get_by_name(self, name: str) -> ProxyRecord | None: ...

    def upsert(self, record: ProxyRecord) -> ProxyRecord | None: ...

    def update_health(self, proxy_id: str, health: ProxyHealth) -> None: ...

    def delete(self, proxy_id: str) -> None: ...


@runtime_checkable
class ProxyStore(Protocol):
    """Anything exposing a ``proxies`` repository (the D1 store does)."""

    proxies: ProxyRepository


ClientFactory = Callable[[str, float], httpx.Client]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class ProxyPoolService:
    """Implements :class:`avdfleet.core.interfaces.ProxyPool` on top of a store + vault."""

    def __init__(
        self,
        store: Any,
        vault: Any | None = None,
        config: Config | None = None,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._store = store
        self._repo: ProxyRepository = getattr(store, "proxies", store)
        self._vault = vault
        self._config = config or Config()
        self._client_factory: ClientFactory = client_factory or self._default_client
        self._log = get_logger("proxy.pool")

    # -- reads --------------------------------------------------------------
    def list_proxies(self, *, include_disabled: bool = False) -> list[ProxyConfig]:
        records = list(self._repo.list(include_disabled=include_disabled) or [])
        return [
            self._to_config(record)
            for record in records
            if include_disabled or bool(getattr(record, "enabled", True))
        ]

    def get(self, proxy_id: str) -> ProxyConfig:
        record = self._get_record(proxy_id)
        if record is None:
            raise NotFoundError(f"proxy not found: {proxy_id}")
        return self._to_config(record)

    def list_views(self, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        return [self.to_view(config) for config in self.list_proxies(include_disabled=include_disabled)]

    def view(self, proxy: ProxyConfig | str) -> dict[str, Any]:
        return self.to_view(proxy)

    def to_view(self, proxy: ProxyConfig | str) -> dict[str, Any]:
        """Render the API ``ProxyView`` dict: no password, truthful ``has_credentials``."""
        record: Any | None
        if isinstance(proxy, ProxyConfig):
            config = proxy
            record = self._get_record(config.id)
        else:
            record = self._get_record(proxy)
            if record is None:
                raise NotFoundError(f"proxy not found: {proxy}")
            config = self._to_config(record)
        return {
            "id": config.id,
            "name": config.name,
            "scheme": config.scheme.value,
            "host": config.host,
            "port": config.port,
            "username": config.username,
            "has_credentials": self._has_credentials(config, record),
            "tags": list(config.tags),
            "enabled": config.enabled,
            "health": config.health.model_dump(mode="json"),
            "created_at": config.created_at.isoformat() if config.created_at else None,
        }

    # -- writes -------------------------------------------------------------
    def add(self, proxy: ProxyConfig | Mapping[str, Any] | None = None, **fields: Any) -> ProxyConfig:
        """Create a proxy from a ``ProxyConfig``, a mapping or keyword fields.

        Accepts ``url`` in addition to explicit fields.  The password (if any) is
        written to the vault under ``proxy/<id>`` before the row is persisted.
        """
        config, password = self._build_config(proxy, fields)
        self._ensure_unique(config, exclude_id=None)
        return self._persist_new(config, password, source=config.source or DEFAULT_SOURCE)

    def import_text(self, text: str, *, source: str = "paste") -> ProxyImportResult:
        """Import one-proxy-per-line bulk text; duplicates are skipped, never fatal."""
        parsed = parse_proxy_text(text or "")
        added: list[ProxyConfig] = []
        errors = [error.message for error in parsed.errors]
        skipped = len(parsed.duplicates)
        known = {_fingerprint_record(record) for record in self._repo.list(include_disabled=True) or []}
        for entry in parsed.entries:
            if entry.fingerprint in known:
                skipped += 1
                continue
            try:
                config = self._persist_parsed(entry, source=source)
            except (ConflictError, ProxyError, ValidationError) as exc:
                errors.append(redact_text(exc.message))
                continue
            except VaultError as exc:
                errors.append(redact_text(exc.message))
                continue
            added.append(config)
            known.add(entry.fingerprint)
        return ProxyImportResult(added=added, skipped=skipped, errors=_dedupe(errors))

    def update(self, proxy_id: str, values: Mapping[str, Any] | None = None, **fields: Any) -> ProxyConfig:
        """Merge PATCH-style ``values`` into a proxy.

        Password semantics: absent -> keep; ``None`` -> delete the vaulted secret;
        empty string -> keep; any other string -> replace.
        """
        record = self._get_record(proxy_id)
        if record is None:
            raise NotFoundError(f"proxy not found: {proxy_id}")
        current = self._to_config(record)
        payload: dict[str, Any] = dict(values or {})
        payload.update(fields)

        merged: dict[str, Any] = {
            "id": current.id,
            "name": current.name,
            "scheme": current.scheme.value,
            "host": current.host,
            "port": current.port,
            "username": current.username,
            "tags": list(current.tags),
            "enabled": current.enabled,
        }
        extra_password: Any = _UNSET
        if payload.get("url"):
            try:
                parsed = parse_proxy_url(str(payload["url"]))
            except ProxyError as exc:
                raise ValidationError(exc.message) from None
            merged.update(
                scheme=parsed.scheme.value,
                host=parsed.host,
                port=parsed.port,
                username=parsed.username,
            )
            if "password" not in payload and parsed.password is not None:
                extra_password = parsed.password
        for key in ("name", "scheme", "host", "port", "username", "tags", "enabled"):
            if key in payload:
                merged[key] = payload[key]
        merged["scheme"] = _canonical_scheme(str(getattr(merged["scheme"], "value", merged["scheme"])))
        try:
            merged["host"] = normalise_host(str(merged["host"]))
        except ProxyError as exc:
            raise ValidationError(exc.message) from None
        try:
            merged["port"] = int(merged["port"])
        except (TypeError, ValueError):
            raise ValidationError(f"invalid proxy port: {merged.get('port')!r}") from None
        try:
            validate_port(merged["port"])
        except ProxyError as exc:
            raise ValidationError(exc.message) from None
        config = self._validate_config(merged)
        self._ensure_unique(config, exclude_id=config.id)

        password_update: Any = _UNSET
        if "password" in payload:
            password_update = payload["password"]
        elif extra_password is not _UNSET:
            password_update = extra_password
        secret_ref = getattr(record, "secret_ref", None)
        if password_update is not _UNSET:
            if password_update is None:
                # Explicit JSON null deletes the vaulted password.
                if secret_ref:
                    self._delete_secret_quietly(secret_ref)
                secret_ref = None
            else:
                password_text = _password_text(password_update)
                if password_text:
                    if secret_ref is None:
                        secret_ref = self._secret_ref(config.id)
                    self._set_secret(secret_ref, password_text)
                # an empty string keeps the existing secret
        updated = ProxyRecord(
            id=config.id,
            name=config.name,
            scheme=config.scheme.value,
            host=config.host,
            port=config.port,
            username=config.username,
            secret_ref=secret_ref,
            tags=list(config.tags),
            enabled=config.enabled,
            source=str(getattr(record, "source", "") or DEFAULT_SOURCE),
            health=_coerce_health(getattr(record, "health", None)),
            created_at=_coerce_datetime(getattr(record, "created_at", None)) or utcnow(),
        )
        self._upsert(updated)
        return self._to_config(updated)

    def delete(self, proxy_id: str) -> None:
        record = self._get_record(proxy_id)
        if record is None:
            raise NotFoundError(f"proxy not found: {proxy_id}")
        self._repo.delete(proxy_id)
        secret_ref = getattr(record, "secret_ref", None)
        if secret_ref:
            self._delete_secret_quietly(secret_ref)
        self._log.info("proxy deleted: id=%s", proxy_id)

    # -- health -------------------------------------------------------------
    def check(self, proxy_id: str, *, timeout_s: float | None = None) -> ProxyTestResult:
        """Probe the proxy with an HTTP GET and persist the resulting health."""
        record = self._get_record(proxy_id)
        if record is None:
            raise NotFoundError(f"proxy not found: {proxy_id}")
        config = self._to_config(record)
        timeout = float(timeout_s) if timeout_s is not None else float(self._config.proxy_verify_timeout_s)
        verify_url = self._config.proxy_verify_url
        password = ""
        started = time.perf_counter()
        try:
            password = self._password_for(record) or ""
            upstream = proxy_url(config.scheme, config.host, config.port, config.username, password)
            with self._client_factory(upstream, timeout) as client:
                response = client.get(verify_url)
            latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
            status_code = int(response.status_code)
            ok = 200 <= status_code < 400
            result = ProxyTestResult(
                proxy_id=config.id,
                ok=ok,
                latency_ms=latency_ms,
                egress_ip=_extract_egress_ip(response),
                status_code=status_code,
                error="" if ok else f"proxy returned HTTP {status_code}",
            )
        except VaultError as exc:
            result = ProxyTestResult(
                proxy_id=config.id,
                ok=False,
                latency_ms=round((time.perf_counter() - started) * 1000.0, 2),
                error=redact_text(exc.message)[:_MAX_ERROR_LENGTH],
            )
        except Exception as exc:  # any transport failure is a failed check
            result = ProxyTestResult(
                proxy_id=config.id,
                ok=False,
                latency_ms=round((time.perf_counter() - started) * 1000.0, 2),
                error=_describe_error(exc, password),
            )
        self._repo.update_health(config.id, result.as_health())
        return result

    def check_all(self) -> list[ProxyTestResult]:
        return [self.check(config.id) for config in self.list_proxies(include_disabled=False)]

    # -- credential helpers used by the bridge / verifier -------------------
    def get_with_credentials(self, proxy_id: str) -> ProxyConfig:
        """Return a config with the vaulted password populated.

        **Internal use only** (bridge upstream dialling, guest verifier).  Never
        return this object from the API: use :meth:`to_view` / :meth:`list_views`.
        ``repr``/``str`` of the returned config still mask the password.
        """
        record = self._get_record(proxy_id)
        if record is None:
            raise NotFoundError(f"proxy not found: {proxy_id}")
        config = self._to_config(record)
        password = self._password_for(record)
        if not password:
            return config
        return config.model_copy(update={"password": SecretStr(password)})

    def upstream_url(self, proxy_id: str) -> str:
        """Return the **sensitive** upstream URL including credentials.

        Internal use only (bridge/verifier wiring); never log or return this from
        the API.  Use :meth:`to_view` for API-facing data.
        """
        record = self._get_record(proxy_id)
        if record is None:
            raise NotFoundError(f"proxy not found: {proxy_id}")
        config = self._to_config(record)
        return proxy_url(
            config.scheme,
            config.host,
            config.port,
            config.username,
            self._password_for(record),
        )

    # -- internals ----------------------------------------------------------
    def _get_record(self, proxy_id: str) -> Any | None:
        if not proxy_id:
            raise NotFoundError("proxy not found: <empty id>")
        try:
            return self._repo.get(proxy_id)
        except NotFoundError:
            return None

    def _persist_new(self, config: ProxyConfig, password: str | None, *, source: str) -> ProxyConfig:
        record = ProxyRecord(
            id=config.id,
            name=config.name,
            scheme=config.scheme.value,
            host=config.host,
            port=config.port,
            username=config.username,
            secret_ref=self._secret_ref(config.id) if password is not None else None,
            tags=list(config.tags),
            enabled=config.enabled,
            source=source or DEFAULT_SOURCE,
            health=ProxyHealth(),
            created_at=utcnow(),
        )
        if password is not None:
            self._set_secret(self._secret_ref(config.id), password)
        try:
            self._upsert(record)
        except Exception:
            if record.secret_ref:
                self._delete_secret_quietly(record.secret_ref)
            raise
        self._log.info(
            "proxy added: id=%s scheme=%s host=%s port=%s credentials=%s",
            config.id,
            config.scheme.value,
            config.host,
            config.port,
            password is not None,
        )
        return self._to_config(record)

    def _import_name(self, base: str) -> str:
        """Return a free display name for a bulk-imported proxy.

        Rotating proxy providers hand out many credentials behind a single
        gateway (``host:port``), so the plain ``host:port`` name collides for
        every entry after the first and the rest were rejected as name conflicts.
        Imported proxies therefore become ``host:port``, ``host:port #2``, ... .
        """
        candidate = base
        index = 1
        while self._repo.get_by_name(candidate) is not None:
            index += 1
            candidate = f"{base} #{index}"
        return candidate

    def _persist_parsed(self, entry: ParsedProxy, *, source: str) -> ProxyConfig:
        config, password = self._build_config(
            {
                "id": entry.id,
                "name": self._import_name(entry.name),
                "scheme": entry.scheme.value,
                "host": entry.host,
                "port": entry.port,
                "username": entry.username,
                "source": source,
            },
            {"password": entry.password} if entry.password is not None else {},
        )
        self._ensure_unique(config, exclude_id=None)
        return self._persist_new(config, password, source=source)

    def _to_config(self, record: Any) -> ProxyConfig:
        scheme = _coerce_scheme(getattr(record, "scheme", ProxyScheme.HTTP))
        host = str(getattr(record, "host", "") or "")
        port = int(getattr(record, "port", 0) or 0)
        try:
            return ProxyConfig(
                id=str(getattr(record, "id", "")),
                name=str(getattr(record, "name", "") or f"{host}:{port}"),
                scheme=scheme,
                host=host,
                port=port,
                username=getattr(record, "username", None) or None,
                password=None,
                tags=list(getattr(record, "tags", None) or []),
                enabled=bool(getattr(record, "enabled", True)),
                source=str(getattr(record, "source", "") or ""),
                health=_coerce_health(getattr(record, "health", None)),
                created_at=_coerce_datetime(getattr(record, "created_at", None)),
            )
        except PydanticValidationError as exc:
            raise ProxyError(f"stored proxy record is invalid: {redact_text(str(exc))}") from None

    def _build_config(
        self, proxy: ProxyConfig | Mapping[str, Any] | None, fields: Mapping[str, Any]
    ) -> tuple[ProxyConfig, str | None]:
        data: dict[str, Any] = {}
        if isinstance(proxy, ProxyConfig):
            data.update(
                id=proxy.id,
                name=proxy.name,
                scheme=proxy.scheme.value,
                host=proxy.host,
                port=proxy.port,
                username=proxy.username,
                tags=list(proxy.tags),
                enabled=proxy.enabled,
                source=proxy.source,
            )
            if proxy.password is not None:
                data["password"] = proxy.password
        elif proxy is not None:
            data.update(dict(proxy))
        data.update(dict(fields))

        password: str | None = None
        if "password" in data:
            password = _password_value(data.pop("password"))
        if data.get("url"):
            try:
                parsed = parse_proxy_url(str(data.pop("url")))
            except ProxyError as exc:
                raise ValidationError(exc.message) from None
            data.setdefault("scheme", parsed.scheme.value)
            data.setdefault("host", parsed.host)
            data.setdefault("port", parsed.port)
            data.setdefault("username", parsed.username)
            if password is None and parsed.password is not None:
                password = parsed.password
        data.pop("url", None)

        scheme_name = _canonical_scheme(
            str(getattr(data.get("scheme", "http"), "value", data.get("scheme", "http")))
        )
        if scheme_name not in DEFAULT_PORTS:
            raise ValidationError(f"unsupported proxy scheme: {scheme_name!r}")
        try:
            host = normalise_host(str(data.get("host", "")))
        except ProxyError as exc:
            raise ValidationError(exc.message) from None
        raw_port = data.get("port", DEFAULT_PORTS[scheme_name])
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            raise ValidationError(f"invalid proxy port: {raw_port!r}") from None
        try:
            validate_port(port)
        except ProxyError as exc:
            raise ValidationError(exc.message) from None
        username = str(data["username"]) if data.get("username") else None
        if password is not None and username is None:
            raise ValidationError("proxy password requires a username")
        proxy_id = str(data.get("id") or "").strip() or make_proxy_id(
            ProxyScheme(scheme_name), host, port, username
        )
        name = str(data.get("name") or "").strip() or f"{host}:{port}"
        try:
            config = ProxyConfig(
                id=proxy_id,
                name=name,
                scheme=ProxyScheme(scheme_name),
                host=host,
                port=port,
                username=username,
                password=None,
                tags=_string_list(data.get("tags")),
                enabled=bool(data.get("enabled", True)),
                source=str(data.get("source") or DEFAULT_SOURCE),
            )
        except PydanticValidationError as exc:
            raise ValidationError(f"invalid proxy: {redact_text(str(exc))}") from None
        return config, password

    def _validate_config(self, merged: Mapping[str, Any]) -> ProxyConfig:
        try:
            return ProxyConfig.model_validate(dict(merged))
        except PydanticValidationError as exc:
            raise ValidationError(f"invalid proxy: {redact_text(str(exc))}") from None

    def _ensure_unique(self, config: ProxyConfig, *, exclude_id: str | None) -> None:
        other = self._repo.get_by_name(config.name)
        other_id = str(getattr(other, "id", "")) if other is not None else ""
        if other is not None and other_id != (exclude_id or ""):
            raise ConflictError(f"proxy name already in use: {config.name}")
        fingerprint = proxy_fingerprint(config.scheme, config.host, config.port, config.username)
        for record in self._repo.list(include_disabled=True) or []:
            record_id = str(getattr(record, "id", ""))
            if exclude_id and record_id == exclude_id:
                continue
            if record_id == config.id:
                raise ConflictError(f"proxy already exists: {config.name}")
            if _fingerprint_record(record) == fingerprint:
                raise ConflictError(
                    f"proxy already exists for {config.scheme.value}://{config.host}:{config.port}"
                )

    def _secret_ref(self, proxy_id: str) -> str:
        return f"{SECRET_KEY_PREFIX}{proxy_id}"

    def _upsert(self, record: ProxyRecord) -> None:
        """Persist through the D1 repository, converting to its record type when present."""
        self._repo.upsert(_to_store_record(record))

    def _password_for(self, record: Any) -> str | None:
        secret_ref = getattr(record, "secret_ref", None)
        if not secret_ref or self._vault is None:
            return None
        return self._vault.get(secret_ref)

    def _has_credentials(self, config: ProxyConfig, record: Any | None) -> bool:
        if config.username:
            return True
        secret_ref = getattr(record, "secret_ref", None) if record is not None else None
        if not secret_ref:
            return False
        if self._vault is None:
            return True
        try:
            return self._vault.get(secret_ref) is not None
        except VaultError:
            return True

    def _set_secret(self, secret_ref: str, password: str) -> None:
        if self._vault is None:
            raise VaultError(
                "proxy credentials require an available secret vault",
                hint="create a vault via avdfleet.proxy.vault.create_vault() and pass it to ProxyPoolService",
            )
        self._vault.set(secret_ref, password)

    def _delete_secret_quietly(self, secret_ref: str) -> None:
        if self._vault is None:
            return
        try:
            self._vault.delete(secret_ref)
        except Exception as exc:  # a broken vault must not hide the primary operation
            self._log.warning(
                "could not delete vaulted proxy secret %s: %s", secret_ref, redact_text(str(exc))
            )

    @staticmethod
    def _default_client(upstream: str, timeout: float) -> httpx.Client:
        return httpx.Client(proxy=upstream, timeout=timeout, trust_env=False, follow_redirects=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_UNSET = object()


def _to_store_record(record: ProxyRecord) -> Any:
    """Convert the service record to the D1 store record when that package exists.

    The pool deliberately does not import ``avdfleet.store`` at module import time
    so it stays usable on its own (unit tests, portable bootstrap).  When the real
    store is installed the repository only accepts ``avdfleet.store.models.ProxyRecord``
    or a ``ProxyConfig``; the latter cannot carry ``secret_ref``, so use the former.
    """
    try:
        from ...store.models import ProxyRecord as StoreProxyRecord
    except Exception:
        return record
    config = ProxyConfig(
        id=record.id,
        name=record.name,
        scheme=_coerce_scheme(record.scheme),
        host=record.host,
        port=record.port,
        username=record.username,
        password=None,
        tags=list(record.tags),
        enabled=record.enabled,
        source=record.source,
        health=_coerce_health(record.health),
        created_at=record.created_at,
    )
    return StoreProxyRecord.from_config(config, secret_ref=record.secret_ref)


def proxy_id_for(scheme: ProxyScheme | str, host: str, port: int, username: str | None = None) -> str:
    """Public helper mirroring the deterministic parser ids."""
    scheme_value = scheme.value if isinstance(scheme, ProxyScheme) else _canonical_scheme(str(scheme))
    return make_proxy_id(ProxyScheme(scheme_value), host, port, username or None)


def _password_text(value: Any) -> str:
    """Normalise a password field to text (empty string stays empty)."""
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if value is None:
        return ""
    return str(value)


def _password_value(value: Any) -> str | None:
    """Normalise an *add* password field: empty values mean "no password"."""
    text = _password_text(value)
    return text if text else None


def _canonical_scheme(name: str) -> str:
    lowered = (name or "").lower()
    return "socks5" if lowered == "socks5h" else lowered


def _coerce_scheme(value: Any) -> ProxyScheme:
    if isinstance(value, ProxyScheme):
        return value
    try:
        return ProxyScheme(_canonical_scheme(str(value)))
    except ValueError:
        raise ProxyError(f"stored proxy record has an unsupported scheme: {value!r}") from None


def _fingerprint_record(record: Any) -> str:
    return proxy_fingerprint(
        _coerce_scheme(getattr(record, "scheme", ProxyScheme.HTTP)),
        str(getattr(record, "host", "") or ""),
        int(getattr(record, "port", 0) or 0),
        getattr(record, "username", None),
    )


def _coerce_health(value: Any) -> ProxyHealth:
    if isinstance(value, ProxyHealth):
        return value
    if isinstance(value, Mapping):
        try:
            return ProxyHealth.model_validate(dict(value))
        except PydanticValidationError:
            return ProxyHealth(status=ProxyHealthStatus.UNKNOWN)
    return ProxyHealth()


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _describe_error(exc: BaseException, password: str) -> str:
    message = f"proxy check failed: {type(exc).__name__}: {exc}"
    message = redact_text(message)
    if password:
        message = message.replace(password, "***")
    return message[:_MAX_ERROR_LENGTH]


def _extract_egress_ip(response: httpx.Response) -> str | None:
    """Best-effort egress IP extraction from JSON bodies like ``{"origin": "1.2.3.4"}``."""
    try:
        payload = response.json()
    except Exception:  # non-JSON bodies simply carry no egress IP
        return None
    candidates: list[str] = []
    if isinstance(payload, dict):
        for key in ("origin", "ip", "egress_ip", "query"):
            value = payload.get(key)
            if isinstance(value, str):
                candidates.append(value)
    elif isinstance(payload, str):
        candidates.append(payload)
    for candidate in candidates:
        match = _IPV4_RE.search(candidate)
        if match and _is_ipv4(match.group(1)):
            return match.group(1)
    return None


def _is_ipv4(value: str) -> bool:
    octets = value.split(".")
    return len(octets) == 4 and all(octet.isdigit() and int(octet) <= 255 for octet in octets)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
