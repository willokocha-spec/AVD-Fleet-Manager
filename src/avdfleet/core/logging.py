"""Logging configuration and secret redaction.

No credential may ever reach a log sink, an API error body or a persisted run
record.  `redact()` is the single choke point used by the API, CLI and services.
"""

from __future__ import annotations

import io
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

_SECRET_KEYS = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "authorization",
    "proxy_password",
    "private_key",
    "passphrase",
    "auth_token",
}

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # URL userinfo (http/https/socks5) -> keep scheme and host
    (
        re.compile(r"(?i)\b((?:https?|socks5h?)://)([^/\s@:]+):([^@\s/]+)@"),
        r"\1\2:***@",
    ),
    # Bearer / basic authorization headers
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)([^\s\"',;}]+)"),
        r"\1***",
    ),
    # JSON/YAML/TOML key-value pairs, quoted or bare: "password": "hunter2"
    (
        re.compile(
            r"(?i)([\"']?(?:password|passwd|pwd|secret|token|access[_-]?token|"
            r"refresh[_-]?token|api[_-]?key|proxy[_-]?password|passphrase)[\"']?"
            r"\s*[=:]\s*[\"']?)([^\s\"',;}\]]+)"
        ),
        r"\1***",
    ),
    # CLI flags: --password xxx / --token xxx / --proxy-password xxx
    (
        re.compile(r"(?i)(--(?:password|passwd|token|api-key|proxy-password|secret))(\s+)(\S+)"),
        r"\1\2***",
    ),
    # env-style assignments
    (
        re.compile(r"(?i)\b(AVDFLEET_TOKEN|ANDROID_EMULATOR_HTTP_PROXY|HTTPS?_PROXY)\b(\s*=\s*)(\S+)"),
        r"\1\2***",
    ),
    # Common provider token shapes
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "sk-***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "gh***"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"), "jwt.***"),
]

_SENSITIVE_WORDS = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|credential)"
)


def redact(value: Any) -> Any:
    """Return a copy of *value* with secrets masked (str, dict, list or scalar)."""
    if isinstance(value, str):
        result = value
        for pattern, replacement in _PATTERNS:
            result = pattern.sub(replacement, result)
        return result
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower().replace("-", "_") in _SECRET_KEYS:
                redacted[key] = "***" if item not in (None, "", False) else item
            else:
                redacted[key] = redact(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def redact_text(text: str) -> str:
    result = redact(text)
    return result if isinstance(result, str) else str(result)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_text(record.getMessage())
            record.args = None
        except Exception:  # pragma: no cover - logging must never raise
            record.msg = "<log message redaction failed>"
            record.args = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")


def setup_logging(
    *,
    level: str = "INFO",
    json_logs: bool = False,
    log_file: str | Path | None = None,
    console: bool = True,
    force: bool = True,
) -> logging.Logger:
    root = logging.getLogger()
    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
    root.setLevel(level.upper())
    formatter: logging.Formatter = JsonFormatter() if json_logs else TextFormatter()
    handlers: list[logging.Handler] = []
    if console:
        stream = sys.stderr
        try:
            stream.fileno()
        except (AttributeError, io.UnsupportedOperation):
            stream = sys.stderr
        console_handler = logging.StreamHandler(stream)
        console_handler.setFormatter(formatter)
        console_handler.addFilter(RedactingFilter())
        handlers.append(console_handler)
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(RedactingFilter())
        handlers.append(file_handler)
    for handler in handlers:
        root.addHandler(handler)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    return logging.getLogger("avdfleet")


def configure_from_config(config: Any) -> logging.Logger:
    log_file = getattr(config, "log_file", None)
    if not log_file:
        try:
            from .paths import Paths

            log_file = Paths.discover().logs_dir / "avdfleet.log"
        except Exception:
            log_file = None
    return setup_logging(
        level=getattr(config, "log_level", "INFO"),
        json_logs=bool(getattr(config, "log_json", False)),
        log_file=log_file,
        console=True,
    )


def get_logger(name: str = "avdfleet") -> logging.Logger:
    return logging.getLogger(name if name.startswith("avdfleet") else f"avdfleet.{name}")


def file_contains_secret(path: str | Path, needles: list[str]) -> list[str]:
    """Test helper: return secrets found verbatim in a file."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [needle for needle in needles if needle and needle in text]
